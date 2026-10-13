"""The analysis: an upload in, a piece on disk out.

The steps are the desktop app's, in the same order, calling the same code:

1. **the recording** -- an uploaded file, or a YouTube URL fetched with `yt-dlp` and
   converted with `ffmpeg` to 22050 Hz mono PCM, which is the format everything else
   is timed in;
2. **the reference MIDI** -- `memorization/analyze.py --midi-only` writes the steady
   tempo performance of the score, which is what the alignment compares against. It
   runs as a subprocess with the checkout's own interpreter, because that is exactly
   what the README tells a person to run and because it keeps the service from
   depending on the question-bank machinery;
3. **the performance order** -- repeats, endings and jumps expanded, so bar 40 means
   the same thing here as it does in the desktop app;
4. **the alignment** -- `align.align_to_audio`, the same DTW fit the map has always
   been built with, reporting progress as it goes;
5. **the engraving** -- `scoreview.render_svg_pages`: Verovio to SVG per page, with
   every measure's box in the SVG's own units.

Everything that can fail, fails with a message a person can act on: no audio track in
that video, a score with no measures, an upload that is not music at all. Those messages
end up on the job, which is where the browser shows them.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, Optional

from .config import REPO_ROOT, Settings, analyzer_importable
from .pieces import PieceStore, fingerprint, slugify

# Where each step sits in the progress bar. The alignment is the slow part by a wide
# margin -- tens of seconds against a couple for everything else -- so it owns most of
# the bar, and the steps after it are given room to report rather than jumping to 100.
STEPS = {
    'audio': (2.0, 20.0),
    'reference': (20.0, 30.0),
    'order': (30.0, 32.0),
    'align': (32.0, 88.0),
    'engrave': (88.0, 98.0),
    'store': (98.0, 100.0),
}

Report = Callable[..., None]


class PipelineError(RuntimeError):
  """Something in the analysis failed, with a message worth showing."""


class Progress:
  """Maps one step's own 0-100 onto its slice of the job's progress bar.

  The analyzer reports percentages of *its* work -- `align_to_audio` says 70 when it
  starts the DTW, not 70% of the job -- so each step is given a span here and the
  numbers are rescaled. Without this the bar jumps backwards whenever a step that
  counts to 100 follows one that did not.
  """

  def __init__(self, report: Report, step: str):
    self.report = report
    self.low, self.high = STEPS[step]

  def __call__(self, percent: float, message: str = '') -> None:
    try:
      fraction = max(0.0, min(1.0, float(percent) / 100.0))
    except (TypeError, ValueError):
      fraction = 0.0
    self.report(self.low + (self.high - self.low) * fraction, message)


def analyze(settings: Settings, job_dir: pathlib.Path, params: Dict[str, Any],
            report: Report, log: Callable[[str], None]) -> Dict[str, Any]:
  """Run the whole analysis. Returns what `pieces.PieceStore.write` needs, plus meta.

  Args:
    settings: paths and tool locations.
    job_dir: this job's directory; uploads are already in it.
    params: `{'score': path, 'audio': path|None, 'youtube_url': str|None, 'title': str}`.
    report: `(percent, message)` for the job's progress.
    log: `(line)` for the job's log -- what was run, and what it said.

  Returns:
    `{'slug': ..., 'meta': {...}}` for the piece it stored.

  The work itself is in `analyse`, below: this function is the front door that fetches
  the recording and stores the result, and it is the only part that knows about jobs.
  """
  if not analyzer_importable():
    raise PipelineError('the analyzer package could not be imported; run the service '
                        'from the note-seq checkout (see service/README.md)')

  # The job directory is this run's working space. The API creates it before the upload
  # lands, but a worker started against a job made another way (a script, a retry after
  # somebody cleaned up) must not fail on a missing directory halfway through.
  job_dir.mkdir(parents=True, exist_ok=True)

  store = PieceStore(settings)
  score = pathlib.Path(params['score'])
  if not score.is_file():
    raise PipelineError('the uploaded score is missing from the job directory')
  audio_source = params.get('audio')
  url = (params.get('youtube_url') or '').strip()
  if not audio_source and not url:
    raise PipelineError('give either an audio file or a YouTube URL')

  # 1. the recording ---------------------------------------------------------
  audio = audio_path(settings, job_dir, audio_source, url, Progress(report, 'audio'),
                     log)

  # 2-5. the analysis --------------------------------------------------------
  analysis = analyse(settings, job_dir, score, audio, report, log)

  # 6. store -----------------------------------------------------------------
  step = Progress(report, 'store')
  step(30.0, 'saving the piece')
  result, pages = analysis.result, analysis.pages
  title = (params.get('title') or '').strip() or analysis.score_meta.get('title') or \
      score.stem.replace('_', ' ')
  audio_meta = read_audio_meta(audio) if audio.is_file() else {}
  slug = store.claim_slug(slugify(title), fingerprint(score))
  map_dict = result.to_dict()
  geometry = {'slug': slug,
              'pages': [page.to_dict() for page in pages],
              'warnings': list(analysis.page_warnings)}
  meta = {
      'slug': slug,
      'title': title,
      'composer': analysis.score_meta.get('composer') or '',
      'bars': len({m.measure for m in result.measures}),
      'duration_s': round(result.audio_duration_s, 3),
      'method': result.method,
      'seconds_per_measure': round(result.seconds_per_measure, 4),
      'pages': len(pages),
      'created_at': time.time(),
      'job_id': params.get('job_id') or '',
      'score': dict(analysis.score_meta, name=score.name, sha256=fingerprint(score)),
      'audio': dict(audio_meta, name=audio.name,
                    source='youtube' if url else 'upload',
                    url=url, uploaded=pathlib.Path(audio_source).name if audio_source else ''),
      'quality': result.quality,
      'warnings': list(result.warnings) + list(analysis.page_warnings),
  }
  store.write(slug, score=score, reference_midi=analysis.reference_midi, audio=audio,
              map_dict=map_dict, geometry=geometry, pages=pages, meta=meta)
  log('stored as %s: %d page(s), %d bars, %.1f s of audio'
      % (slug, len(pages), meta['bars'], meta['duration_s']))
  step(100.0, 'done')
  return {'slug': slug, 'meta': meta}


def analysis_config(settings: Settings):
  """The analyzer's config, pointed at this service's data directory.

  The alignment and the engraving cache their intermediate work; none of it should land
  in the desktop app's cache, and none of it should need a desktop app to exist.
  """
  from analyzer.config import AnalyzerConfig
  return AnalyzerConfig(cache_dir=settings.data_dir / 'cache',
                        output_dir=settings.data_dir / 'output')


class Analysis:
  """Everything the analysis produced, before any of it is stored anywhere.

  Split out from `analyze` because there are two front doors to the same work: this
  service's own job, which stores a piece and serves a page, and the broker plugin in
  `service/broker/note_seq`, which hands files back to another system. Both want the
  same alignment and the same engraving, and neither wants a second copy of the code.
  """

  def __init__(self, *, slug: str, score_meta: Dict[str, Any], played: List[int],
               expansion: str, result, pages: List[Any], page_warnings: List[str],
               reference_midi: pathlib.Path, audio: pathlib.Path):
    self.slug = slug
    self.score_meta = score_meta
    self.played = played
    self.expansion = expansion
    self.result = result                 # analyzer.schema.SyncResult
    self.pages = pages                   # analyzer.scoreview.SvgPage
    self.page_warnings = page_warnings
    self.reference_midi = reference_midi
    self.audio = audio

  @property
  def warnings(self) -> List[str]:
    return list(self.result.warnings) + list(self.page_warnings)

  def page_for_bar(self, bar: int):
    """The engraved page a bar is on, or None."""
    for page in self.pages:
      if any(box.number == bar for box in page.measures):
        return page
    return None

  def pages_for_bars(self, first: int, last: int) -> List[Any]:
    """The pages a range of bars touches, in order, without duplicates."""
    out = []
    for bar in range(min(first, last), max(first, last) + 1):
      page = self.page_for_bar(bar)
      if page is not None and (not out or out[-1].number != page.number):
        out.append(page)
    seen = set()
    unique = []
    for page in out:
      if page.number not in seen:
        seen.add(page.number)
        unique.append(page)
    return unique


def analyse(settings: Settings, job_dir: pathlib.Path, score: pathlib.Path,
            audio: pathlib.Path, report: Report,
            log: Callable[[str], None]) -> Analysis:
  """Everything after the recording is in hand: reference MIDI, alignment, engraving.

  Steps 2 to 5 of the pipeline, usable on its own -- which is what the broker plugin
  does, since a file staged by another system's worker is just as good a recording as
  one this service uploaded.
  """
  from analyzer import measures, scoreview

  score = pathlib.Path(score)
  audio = pathlib.Path(audio)
  if not score.is_file():
    raise PipelineError('no score to analyse: %s' % score)
  if not audio.is_file():
    raise PipelineError('no recording to analyse: %s' % audio)
  job_dir.mkdir(parents=True, exist_ok=True)
  cfg = analysis_config(settings)

  score_meta = read_score_meta(score)
  log('score: %s (%d bytes)' % (score.name, score.stat().st_size))
  log('score says: %s' % json.dumps(score_meta, sort_keys=True))

  # 2. the reference MIDI ----------------------------------------------------
  reference = build_reference_midi(settings, score, job_dir,
                                   Progress(report, 'reference'), log)

  # 3. the performance order -------------------------------------------------
  step = Progress(report, 'order')
  step(40.0, 'working out the order the bars are played in')
  try:
    expansion = measures.expand_measures(score)
  except Exception as e:
    raise PipelineError('could not read the bar structure of the score: %s' % e)
  played = list(expansion.played_numbers)      # a property, not a call
  log('bars: %d played (printed 1..%d), %s'
      % (len(played), max(played) if played else 0, expansion.summary()))
  if not played:
    raise PipelineError('the score has no measures to align')

  # 4. the alignment ---------------------------------------------------------
  step = Progress(report, 'align')
  step(1.0, 'aligning the recording to the score')
  started = time.monotonic()
  try:
    result = align_result = _align(played, audio, reference, cfg, step)
  except PipelineError:
    raise
  except Exception as e:
    raise PipelineError('the alignment failed: %s' % e)
  log('aligned in %.1f s: method=%s, %.3f s a bar, %d measures, %d warning(s)'
      % (time.monotonic() - started, result.method, result.seconds_per_measure,
         len(result.measures), len(result.warnings)))
  for warning in result.warnings:
    log('warning: %s' % warning)

  # 5. the engraving ---------------------------------------------------------
  step = Progress(report, 'engrave')
  try:
    pages, page_warnings = scoreview.render_svg_pages(cfg, score, progress=step)
  except Exception as e:
    raise PipelineError('the score could not be engraved: %s' % e)
  log('engraved %d page(s) to SVG' % len(pages))

  return Analysis(slug=slugify(score.stem), score_meta=score_meta, played=played,
                  expansion=expansion.summary(), result=align_result, pages=pages,
                  page_warnings=page_warnings, reference_midi=reference, audio=audio)


def _align(played, audio, reference, cfg, progress):
  """`align.align_to_audio`, imported here so this module lists without the stack."""
  from analyzer import align
  return align.align_to_audio(played, str(audio), str(reference), cfg=cfg,
                              progress=progress)


# ---------------------------------------------------------------------------
# 1. the recording
# ---------------------------------------------------------------------------

def audio_path(settings: Settings, job_dir: pathlib.Path, uploaded: Optional[str],
               url: str, report: Report, log: Callable[[str], None]) -> pathlib.Path:
  """The recording, as 22050 Hz mono PCM in the job directory."""
  dest = job_dir / 'audio.wav'
  if url and not uploaded:
    source = fetch_youtube(settings, job_dir, url, report, log)
  elif uploaded:
    source = pathlib.Path(uploaded)
    if not source.is_file():
      raise PipelineError('the uploaded audio is missing from the job directory')
  else:
    raise PipelineError('give either an audio file or a YouTube URL')
  convert_audio(settings, source, dest, report, log)
  return dest


def fetch_youtube(settings: Settings, job_dir: pathlib.Path, url: str,
                  report: Report, log: Callable[[str], None]) -> pathlib.Path:
  """Download the best audio track of a video, without post-processing it."""
  import yt_dlp
  report(5.0, 'looking up the video')
  log('fetching %s' % url)

  def hook(status: Dict[str, Any]) -> None:
    if status.get('status') == 'downloading':
      total = status.get('total_bytes') or status.get('total_bytes_estimate') or 0
      done = status.get('downloaded_bytes') or 0
      if total:
        report(5.0 + 55.0 * min(1.0, done / float(total)),
               'downloading the recording (%.0f%%)' % (100.0 * done / float(total)))
    elif status.get('status') == 'finished':
      report(65.0, 'downloaded; converting')

  options = {
      'format': 'bestaudio/best',
      'outtmpl': str(job_dir / 'download.%(ext)s'),
      'quiet': True,
      'no_warnings': True,
      'noprogress': True,
      'progress_hooks': [hook],
      'noplaylist': True,
  }
  try:
    with yt_dlp.YoutubeDL(options) as ydl:
      info = ydl.extract_info(url, download=True)
  except Exception as e:
    raise PipelineError('could not fetch that YouTube URL: %s' % _short(e))
  found = sorted(job_dir.glob('download.*'))
  if not found:
    raise PipelineError('the download produced no file')
  # A search URL hands back the video's own metadata; a plain URL may hand back a
  # wrapper with `entries`, in which case the duration lives on the entry.
  entry = info or {}
  if not entry.get('title') and entry.get('entries'):
    entry = (entry['entries'] or [{}])[0] or {}
  title = entry.get('title') or ''
  duration = entry.get('duration')
  log('downloaded %s%s as %s'
      % (title or '(no title)',
         ' (%.0f s)' % duration if duration else '',
         found[0].name))
  (job_dir / 'source.txt').write_text(
      json.dumps({'title': title, 'duration': duration, 'url': url}), encoding='utf-8')
  return found[0]


def convert_audio(settings: Settings, source: pathlib.Path, dest: pathlib.Path,
                  report: Report, log: Callable[[str], None]) -> None:
  """ffmpeg to the one format everything downstream assumes.

  22050 Hz mono is what the analyzer aligns at, so converting here rather than reading
  the upload at its own rate keeps one clock for the whole pipeline -- and it means an
  m4a from a phone and a 96 kHz flac behave identically.
  """
  if settings.ffmpeg is None:
    raise PipelineError('ffmpeg is not installed; it is needed to read the recording')
  report(70.0, 'converting the recording to 22050 Hz mono')
  dest.parent.mkdir(parents=True, exist_ok=True)
  command = [settings.ffmpeg, '-nostdin', '-y', '-v', 'error', '-i', str(source),
             '-ac', '1', '-ar', '22050', '-c:a', 'pcm_s16le', str(dest)]
  result = subprocess.run(command, capture_output=True, text=True)
  if result.returncode != 0 or not dest.is_file():
    raise PipelineError('could not read that audio file: %s'
                        % _short(result.stderr or 'ffmpeg failed'))
  log('converted %s -> %s (%.1f MB)' % (source.name, dest.name,
                                        dest.stat().st_size / 1e6))
  report(100.0, 'recording ready')


def read_audio_meta(path: pathlib.Path) -> Dict[str, Any]:
  """Duration, rate and channels of a file, with ffprobe when soundfile will not."""
  import soundfile as sf
  try:
    info = sf.info(str(path))
    return {'duration_s': round(info.frames / float(info.samplerate), 3),
            'sample_rate': info.samplerate, 'channels': info.channels,
            'format': info.format}
  except Exception:
    return {}


# ---------------------------------------------------------------------------
# 2. the reference MIDI
# ---------------------------------------------------------------------------

ANALYZE = REPO_ROOT / 'memorization' / 'analyze.py'


def build_reference_midi(settings: Settings, score: pathlib.Path, job_dir: pathlib.Path,
                         report: Report, log: Callable[[str], None]) -> pathlib.Path:
  """The steady-tempo performance of the score, used as the alignment's other side.

  `memorization/analyze.py` is where the reference MIDI has always come from; the
  service runs it with `--midi-only` so it writes the two MIDI files and stops, rather
  than building the whole question bank the desktop app uses.
  """
  report(5.0, 'building the reference MIDI from the score')
  if not ANALYZE.is_file():
    raise PipelineError('memorization/analyze.py is missing from the checkout')
  base = job_dir / 'reference.json'          # the base name; the MIDI is base+'.mid'
  command = [settings.python, str(ANALYZE), str(score), '--name', 'service',
             '--midi-only', '--out', str(base)]
  result = subprocess.run(command, capture_output=True, text=True, cwd=str(REPO_ROOT))
  if result.returncode != 0:
    raise PipelineError('the reference MIDI could not be built: %s'
                        % _short(result.stderr or result.stdout or 'analyze.py failed'))
  steady = pathlib.Path(str(base)[:-len('.json')] + '_steady.mid')
  if not steady.is_file():
    candidates = sorted(job_dir.glob('reference*_steady.mid'))
    if not candidates:
      raise PipelineError('no steady reference MIDI was written; analyze.py said: %s'
                          % _short(result.stdout))
    steady = candidates[0]
  for line in (result.stdout or '').splitlines():
    if 'reference midi' in line or 'could not' in line:
      log(line.strip())
  report(100.0, 'reference MIDI ready')
  return steady


# ---------------------------------------------------------------------------
# score metadata
# ---------------------------------------------------------------------------

def read_score_meta(score: pathlib.Path) -> Dict[str, Any]:
  """Title, composer and movement from the score itself, when it says.

  Best effort by design: a MusicXML without a `<work>` or `<identification>` block is
  perfectly legal, and the filename is a good enough title when it happens.
  """
  try:
    root = _score_root(score)
  except Exception:
    return {}
  if root is None:
    return {}
  out: Dict[str, Any] = {}
  work = _first(root, 'work')
  title = _text(_first(work, 'work-title')) if work is not None else ''
  movement = _text(_first(root, 'movement-title'))
  identification = _first(root, 'identification')
  creators = identification.findall('creator') if identification is not None else []
  for creator in creators:
    kind = (creator.get('type') or '').lower()
    if kind == 'composer' and not out.get('composer'):
      out['composer'] = _text(creator)
    elif kind == 'lyricist' and not out.get('lyricist'):
      out['lyricist'] = _text(creator)
  if title:
    out['title'] = title
  elif movement:
    out['title'] = movement
  if movement and title:
    out['movement'] = movement
  return out


def _score_root(score: pathlib.Path) -> Optional[ET.Element]:
  """The `<score-partwise>` element, unzipping an `.mxl` if that is what it is."""
  import zipfile
  if score.suffix.lower() == '.mxl':
    with zipfile.ZipFile(str(score)) as archive:
      names = [n for n in archive.namelist() if n.lower().endswith(('.xml', '.musicxml'))]
      if not names:
        return None
      container = [n for n in names if 'container' not in n.lower()]
      data = archive.read(sorted(container or names)[0])
    return ET.fromstring(data)
  root = ET.parse(str(score)).getroot()
  if _local(root.tag) != 'score-partwise':
    found = [e for e in root.iter() if _local(e.tag) == 'score-partwise']
    return found[0] if found else None
  return root


def _local(tag: str) -> str:
  return tag.rsplit('}', 1)[-1]


def _first(element: Optional[ET.Element], name: str) -> Optional[ET.Element]:
  if element is None:
    return None
  for child in element:
    if _local(child.tag) == name:
      return child
  return None


def _text(element: Optional[ET.Element]) -> str:
  if element is None or element.text is None:
    return ''
  return element.text.strip()


def _short(value: Any, limit: int = 400) -> str:
  """A tool's output, trimmed to something that fits on a job."""
  text = re.sub(r'\s+', ' ', str(value or '')).strip()
  return text if len(text) <= limit else text[:limit] + '...'
