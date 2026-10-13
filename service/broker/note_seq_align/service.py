"""The note-seq broker plugin: one job, a few artifacts, the analysis of `app.pipeline`.

Written against the contract in `worker_plugins/base.py` of the reference stack:

* `run(params, workdir, outdir, progress, inputs)` -- `inputs` maps each `format: files`
  field to the files the worker staged for it;
* return `[{'kind': ..., 'path': ...}]`, one per artifact;
* raise to fail the job, with a message the submitter can act on.

Nothing here reimplements the analysis. It calls `service.app.pipeline.analyse`, which
is the same code path this service's own jobs use, so a map produced through the broker
is the same map as one produced in the browser.

**Why the service is called `note_seq_align` and not `note_seq`.** The loader imports a
plugin *by its directory name*, and this repository already contains a package called
`note_seq` (the melody library `memorization/` builds on). A service named `note_seq`
would therefore be shadowed -- or shadow -- depending on the order directories landed on
`sys.path`, and the failure would read as "no plugin found", which says nothing about
the real cause. The extra word costs nothing and cannot collide.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parents[3]                       # the note-seq checkout
# Appended rather than inserted: the worker's own imports are already in hand by the time
# a plugin is loaded, and a plugin that jumps its repository to the front of `sys.path`
# is a plugin that can shadow somebody else's module.
for _path in (str(REPO), str(REPO / 'analyzer')):
  if _path not in sys.path:
    sys.path.append(_path)

from service.app.config import Settings                   # noqa: E402
from service.app.pipeline import (Progress, analyse, audio_path,        # noqa: E402
                                  publish_piece)

# The passage a range is cut with, in seconds, either side: the same run-in and run-out
# the desktop app uses, because starting exactly on the downbeat clips the piano's
# attack and stopping exactly on the last beat cuts the pedal.
PRE_ROLL_S = 0.12
POST_ROLL_S = 0.25

# Where the highlight sits on a page. A translucent wash over the bars being asked
# about, drawn *into* the SVG before it is rasterised rather than over the pixels
# afterwards: the boxes are exact, so the honest thing is to use them as geometry
# instead of guessing where the ink is.
HIGHLIGHT = '#2f7ed8'
HIGHLIGHT_OPACITY = 0.18

# Where this service's own page is, for the link a job hands back. The default is the
# port `service/run.sh` serves on; the deployment that runs a broker worker may well be
# somewhere else, so it is configurable.
VIEWER_URL = os.environ.get('NOTE_SEQ_VIEWER_URL', 'http://localhost:8100').rstrip('/')

KIND_MAP = 'measure_map'
KIND_AUDIO = 'range_audio'
KIND_PAGE = 'score_page'
KIND_REPORT = 'report'

# Every kind this service can produce. `score_page_2` and up are page numbers: the
# broker keys an artifact by (job, kind), so two pages of one job need two kinds -- and
# "score_page_2 · 0.4 MB" reads perfectly well as a download button.
ARTIFACT_KINDS = (KIND_MAP, KIND_AUDIO, KIND_PAGE, KIND_REPORT)


class NoteSeqError(RuntimeError):
  """The submission cannot be analysed. The message becomes the job's error."""


class NoteSeqAlign:
  """Align one recording to one score, and cut the passage that was asked for."""

  name = 'note_seq_align'
  title = 'note-seq: measure map, passage audio, and the engraved page'
  description = ('Aligns a recording to a MusicXML score and returns the measure map, '
                 'the audio of a chosen range of bars, and the engraved page with those '
                 'bars highlighted.')
  artifact_kinds = ARTIFACT_KINDS

  # Only the keywords the broker's validator enforces (see its `param_schema.py`), and
  # deliberately no more: a schema it cannot fully check is refused at registration.
  # `format`/`accept` are rendering hints the submission form understands.
  param_schema = {
      'type': 'object',
      'properties': {
          'score': {
              'type': 'array',
              'format': 'files',
              'accept': '.musicxml,.xml,.mxl',
              'minItems': 1,
              'maxItems': 1,
              'title': 'Score (MusicXML)',
              'description': 'The music the recording plays: .musicxml, .xml or .mxl.',
          },
          'recording': {
              'type': 'array',
              'format': 'files',
              'accept': '.mp3,.wav,.flac,.m4a,.aac,.ogg,.opus,.wma,.webm',
              'maxItems': 1,
              'title': 'Recording',
              'description': 'The performance to align. Give this or a YouTube URL.',
          },
          'youtube_url': {
              'type': 'string',
              'maxLength': 500,
              'title': 'YouTube URL',
              'description': 'Or a YouTube URL: the best audio track is downloaded. '
                             'Give this or a recording.',
          },
          'first_bar': {
              'type': 'integer',
              'minimum': 1,
              'maximum': 10000,
              'title': 'First bar',
              'description': 'Start of the passage to cut and highlight. Leave empty '
                             'for no clip.',
          },
          'last_bar': {
              'type': 'integer',
              'minimum': 1,
              'maximum': 10000,
              'title': 'Last bar',
              'description': 'End of the passage, inclusive.',
          },
          'title': {
              'type': 'string',
              'maxLength': 200,
              'title': 'Title',
              'description': 'What to call this piece; defaults to the score\'s own '
                             'title.',
          },
      },
      'required': ['score'],
      # Exactly one recording: the broker's validator can say "one of these forms" but
      # `anyOf` over object branches is enforced here in `run`, where the message can
      # name the two fields instead of saying "does not match any of the allowed forms".
      'additionalProperties': False,
  }

  def __init__(self, settings: Optional[Settings] = None, viewer: Optional[bool] = None,
               viewer_url: Optional[str] = None, **kwargs):
    # Configuration a deployment may want to pass through `plugin(**kwargs)`; ignored
    # keys are accepted so the worker can pass its own flags to every plugin.
    self.settings = settings
    self.options = kwargs
    # Publishing the piece into the standalone service is on by default -- it is the
    # difference between a folder of downloads and something you can listen in -- and
    # switchable, because a machine that only runs a broker worker should not have a
    # data directory quietly filling up. NOTE_SEQ_VIEWER=0 turns it off.
    self.viewer_enabled = _flag('NOTE_SEQ_VIEWER', True) if viewer is None else bool(viewer)
    self.viewer_url = (viewer_url or VIEWER_URL)

  # ------------------------------------------------------------------ running

  def run(self, params: Dict[str, Any], workdir: pathlib.Path, outdir: pathlib.Path,
          progress, inputs: Optional[Dict[str, List[pathlib.Path]]] = None) -> List[dict]:
    """Analyse one submission. See the package docstring for what comes back."""
    inputs = inputs or {}
    workdir = pathlib.Path(workdir)
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    score = self._staged(inputs, 'score', workdir, required=True)
    recording = self._staged(inputs, 'recording', workdir, required=False)
    url = str(params.get('youtube_url') or '').strip()
    if recording is not None and url:
      raise NoteSeqError('give either a recording or a YouTube URL, not both')
    if recording is None and not url:
      raise NoteSeqError('no recording: upload an audio file or give a YouTube URL')
    first, last = self._range(params)

    # The pipeline reports 0..100 of its *own* work; the worker's bar is 5..95 (it owns
    # the ends, and a plugin that claims 100 is claiming steps it does not control).
    # Every step inside goes through `Progress`, which puts it in its own slice of that
    # 0..100 -- without which the recording, whose own progress runs to 100, would fill
    # the whole bar and the steps after it would jump backwards.
    def report(percent: float, message: str = '') -> None:
      bounded = max(0.0, min(100.0, float(percent)))
      progress(int(round(5 + 90 * bounded / 100.0)), message or 'working')

    settings = self.settings or Settings()
    settings.ensure_dirs()

    # The recording, converted to the one format the alignment is timed in. A staged
    # file goes through exactly the same `audio_path` a browser upload does.
    report(0.0, 'preparing the recording')
    audio = audio_path(settings, workdir, str(recording) if recording else None, url,
                       Progress(report, 'audio'), lambda line: None)

    analysis = analyse(settings, workdir, score, audio, report, lambda line: None)
    map_dict = analysis.result.to_dict()
    report(99.0, 'writing the artifacts')

    produced: List[dict] = []

    # 1. the map: what every bar sounds at.
    map_path = outdir / 'measure_map.json'
    map_path.write_text(json.dumps({
        **map_dict,
        'title': str(params.get('title') or '').strip() or analysis.score_meta.get('title') or score.stem,
        'composer': analysis.score_meta.get('composer') or '',
        'score': {'name': score.name},
        'audio': {'name': audio.name, 'source': 'youtube' if url else 'upload', 'url': url},
        # The pages come with their measure boxes, in the engraving's own units: a
        # consumer that has the map and the page image can then put a bar's box on the
        # page without engraving anything itself. It is what this service's own page does
        # with `geometry.json`, and it is why the two artifacts pair up.
        'pages': [page.to_dict() for page in analysis.pages],
        'warnings': analysis.warnings,
    }, indent=2, sort_keys=True), encoding='utf-8')
    produced.append({'kind': KIND_MAP, 'path': map_path})

    # 2. the passage, as audio, when a range was asked for.
    if first is not None and last is not None:
      span = range_for_bars(map_dict, first, last)
      if span is None:
        raise NoteSeqError('the map has nothing for bars %d-%d' % (first, last))
      start, end = span
      clip, problem = _slice(analysis.audio, max(0.0, start - PRE_ROLL_S), end + POST_ROLL_S)
      if clip is None:
        raise NoteSeqError(problem or 'the passage could not be cut')
      clip_path = outdir / ('bars_%d-%d.wav' % (first, last))
      clip_path.write_bytes(clip)
      produced.append({'kind': KIND_AUDIO, 'path': clip_path})

    # 3. the engraved page(s), with the passage highlighted.
    pages = analysis.pages_for_bars(*(first, last)) if first is not None and last is not None \
        else analysis.pages[:1]
    if not pages:
      pages = analysis.pages[:1]
    for index, page in enumerate(pages):
      kind = KIND_PAGE if index == 0 else '%s_%d' % (KIND_PAGE, index + 1)
      png = _page_png(page, first, last)
      if png is None:
        continue
      image_path = outdir / ('score_page_%d.png' % page.number)
      image_path.write_bytes(png)
      produced.append({'kind': kind, 'path': image_path})

    # 4. the piece, in the standalone service's store, so the artifacts come with a page
    #    you can actually listen in. Failing to publish must not fail the job: the
    #    artifacts are the contract, this is a bonus.
    viewer = self.publish(analysis, score=score, params=params, url=url)

    # 5. a short report: what was asked for, what came out, and what to distrust.
    report_path = outdir / 'report.json'
    report_path.write_text(json.dumps(_report(analysis, first, last, url, produced, viewer),
                                      indent=2, sort_keys=True), encoding='utf-8')
    produced.append({'kind': KIND_REPORT, 'path': report_path})

    if viewer.get('published'):
      report(100.0, '%d bars at %.2f s a bar; %d artifact(s); the page is at %s'
             % (len(map_dict['measures']), analysis.result.seconds_per_measure,
                len(produced), viewer['url']))
    else:
      report(100.0, '%d bars at %.2f s a bar; %d artifact(s)'
             % (len(map_dict['measures']), analysis.result.seconds_per_measure,
                len(produced)))
    return produced

  def publish(self, analysis, *, score: pathlib.Path, params: Dict[str, Any],
              url: str) -> Dict[str, Any]:
    """Store the piece where this service's own page can serve it.

    The artifacts are files a person downloads; the page is where you click bar 40, click
    bar 83 and hear it. Same analysis, two front doors -- so a job that ran through
    another system's worker still leaves something interactive behind.

    Returns a small dict for the report:
      `{'published': True, 'url': ..., 'slug': ...}` or
      `{'published': False, 'reason': ...}` -- never raises, because a job whose
    artifacts are complete should not fail over a link.
    """
    if not self.viewer_enabled:
      return {'published': False, 'reason': 'switched off (NOTE_SEQ_VIEWER)'}
    try:
      settings = self.settings or Settings()
      settings.ensure_dirs()
      slug, meta = publish_piece(
          settings, analysis, score=score,
          title=str(params.get('title') or '').strip(),
          audio_source_name='', url=url, job_id=str(params.get('job_id') or ''))
    except Exception as e:                     # noqa: BLE001 - see the docstring
      return {'published': False, 'reason': '%s: %s' % (type(e).__name__, e)}
    return {'published': True, 'slug': slug, 'url': '%s/pieces/%s' % (self.viewer_url, slug),
            'title': meta.get('title') or slug, 'bars': meta.get('bars')}

  def fake_run(self, params: Dict[str, Any], workdir: pathlib.Path, outdir: pathlib.Path,
               progress, inputs=None) -> List[dict]:
    """Stub for the worker's `--fake-pipeline`, so the queue can be tested with no music.

    It writes the same kinds of file with the same names, small enough to be harmless:
    the point is to exercise submission, dispatch and artifact upload, not the analysis.
    """
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    progress(30, 'fake note-seq run')
    first = int(params.get('first_bar') or 1)
    last = int(params.get('last_bar') or 4)
    measures = [{'measure': bar, 'play_index': bar - 1, 'start_s': round(1.5 * (bar - 1), 3),
                 'end_s': round(1.5 * bar, 3), 'confidence': 1.0}
                for bar in range(1, 9)]
    map_path = outdir / 'measure_map.json'
    map_path.write_text(json.dumps({'method': 'fake', 'seconds_per_measure': 1.5,
                                    'audio_duration_s': 12.0, 'measures': measures,
                                    'warnings': ['this is a fake run']}, indent=2))
    clip = outdir / ('bars_%d-%d.wav' % (first, last))
    clip.write_bytes(_silent_wav(1.0))
    page = outdir / 'score_page_1.png'
    page.write_bytes(_tiny_png())
    report = outdir / 'report.json'
    report.write_text(json.dumps({'fake': True, 'first_bar': first, 'last_bar': last},
                                 indent=2))
    progress(95, 'fake artifacts written')
    return [{'kind': KIND_MAP, 'path': map_path},
            {'kind': KIND_AUDIO, 'path': clip},
            {'kind': KIND_PAGE, 'path': page},
            {'kind': KIND_REPORT, 'path': report}]

  # ------------------------------------------------------------------ inputs

  def _staged(self, inputs: Dict[str, List[pathlib.Path]], field: str,
              workdir: pathlib.Path, required: bool) -> Optional[pathlib.Path]:
    """The file the worker staged for a `format: files` field.

    The worker stages each field's files under their original names in `workdir` and
    hands the mapping over; the field name is what says which is which, so this is a
    lookup rather than a guess about filenames.
    """
    files = [pathlib.Path(p) for p in (inputs.get(field) or [])]
    if not files:
      # The worker stages each field's files under their original names and passes the
      # mapping over; guessing which file is which from its name is exactly what the
      # contract warns against, so an absent field is an absent field.
      if required:
        raise NoteSeqError('no %s file was staged for this job' % field)
      return None
    if len(files) > 1:
      raise NoteSeqError('give one %s file, not %d' % (field, len(files)))
    if not files[0].is_file():
      raise NoteSeqError('the staged %s file is missing: %s' % (field, files[0]))
    return files[0]

  def _range(self, params: Dict[str, Any]):
    """The passage to cut, or (None, None) for the whole piece."""
    first = params.get('first_bar')
    last = params.get('last_bar')
    if first in (None, '') and last in (None, ''):
      return None, None
    if first in (None, '') or last in (None, ''):
      raise NoteSeqError('give both first_bar and last_bar, or neither')
    try:
      first, last = int(first), int(last)
    except (TypeError, ValueError):
      raise NoteSeqError('first_bar and last_bar must be whole numbers of bars')
    if first < 1 or last < 1:
      raise NoteSeqError('bar numbers start at 1')
    if last < first:
      first, last = last, first
    return first, last


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------

def range_for_bars(map_dict: Dict[str, Any], first: int, last: int):
  """(start_s, end_s) for bars `first`..`last`, by the analyzer's own rule.

  Built through `SyncResult.range_for_measures` rather than reimplemented, so a passage
  means here exactly what it means in the desktop app and in this service's own page.
  """
  from analyzer.schema import MeasureTime, SyncResult
  measures = [MeasureTime(measure=int(m['measure']), play_index=int(m.get('play_index') or 0),
                          start_s=float(m['start_s']), end_s=float(m['end_s']),
                          confidence=m.get('confidence'))
              for m in (map_dict.get('measures') or [])]
  if not measures:
    return None
  result = SyncResult(method=map_dict.get('method') or 'dtw', measures=measures,
                      seconds_per_measure=float(map_dict.get('seconds_per_measure') or 0.0),
                      audio_duration_s=float(map_dict.get('audio_duration_s') or 0.0),
                      offset_s=float(map_dict.get('offset_s') or 0.0))
  return result.range_for_measures(int(first), int(last))


def _slice(path: pathlib.Path, start_s: float, end_s: float):
  """`[start_s, end_s)` of a WAV, or (None, why not)."""
  from analyzer.server import slice_file
  return slice_file(str(path), start_s, end_s)


def _page_png(page, first: Optional[int], last: Optional[int]) -> Optional[bytes]:
  """One engraved page as a PNG, with the requested bars washed in colour.

  The highlight is a `<rect>` per bar, placed at the box the engraving itself reported
  (the same boxes the browser puts its click targets on) and inserted into the SVG
  before rasterising. Drawing on the finished pixels instead would mean finding the ink
  again, which is the mistake this repository has already made once.
  """
  import cairosvg
  svg = page.svg
  if first is not None and last is not None:
    rects = []
    for box in page.measures:
      if min(first, last) <= box.number <= max(first, last):
        rects.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                     'fill="%s" fill-opacity="%.2f"/>'
                     % (box.x0, box.y0, max(1.0, box.x1 - box.x0),
                        max(1.0, box.y1 - box.y0), HIGHLIGHT, HIGHLIGHT_OPACITY))
    if rects:
      svg = _inject(svg, ''.join(rects))
  try:
    # 1.5 is the analyzer's own raster scale: about 1260 px across for a page of this
    # size, which is what the desktop app draws and what a screen can show.
    return cairosvg.svg2png(bytestring=svg.encode('utf-8'), scale=1.5)
  except Exception:
    return None


def _inject(svg: str, markup: str) -> str:
  """Put `markup` on top of the music, inside the drawing that carries the viewBox.

  Verovio emits two nested SVGs: an outer one with a fixed pixel size and no viewBox,
  and an inner `definition-scale` one whose viewBox is the engraving's own units
  (21000 x 28440 for a page here). The measure boxes are in those units, so the
  rectangles have to go inside the *inner* document -- placed before the last `</svg>`
  they would be read as pixels and land 25 times too far out. Painting them last also
  puts the wash over the notes rather than under them.
  """
  end = _drawing_close(svg)
  if end < 0:
    return svg
  return svg[:end] + '<g class="note-seq-highlight">' + markup + '</g>' + svg[end:]


def _drawing_close(svg: str) -> int:
  """Where the inner drawing's `</svg>` starts, or -1 if the SVG is not that shape."""
  inner = svg.find('class="definition-scale"')
  start = svg.rfind('<svg', 0, inner) if inner >= 0 else -1
  if start < 0:
    return svg.rfind('</svg>')
  depth = 0
  position = start
  while True:
    opening = svg.find('<svg', position)
    closing = svg.find('</svg>', position)
    if closing < 0:
      return -1
    if 0 <= opening < closing:
      depth += 1
      position = opening + 4
      continue
    depth -= 1
    position = closing + len('</svg>')
    if depth == 0:
      return closing


def _report(analysis, first, last, url, produced, viewer=None) -> Dict[str, Any]:
  """What a person should know before trusting the numbers.

  `artifacts` lists what the job produced *besides this file*: the report is written
  last, so claiming itself in its own contents would be a promise made before the file
  exists.
  """
  quality = dict(analysis.result.quality or {})
  warnings = list(analysis.warnings)
  clamped = int(quality.get('bars_clamped') or 0)
  return {
      'title': analysis.score_meta.get('title') or '',
      'composer': analysis.score_meta.get('composer') or '',
      'bars': len({m.measure for m in analysis.result.measures}),
      'duration_s': round(analysis.result.audio_duration_s, 3),
      'seconds_per_measure': round(analysis.result.seconds_per_measure, 4),
      'method': analysis.result.method,
      'passage': ({'first_bar': first, 'last_bar': last} if first is not None else None),
      'recording': {'source': 'youtube' if url else 'upload', 'url': url},
      'quality': quality,
      'warnings': warnings,
      'reading': _reading(clamped),
      'artifacts': [{'kind': a['kind'], 'name': pathlib.Path(a['path']).name}
                    for a in produced],
      # Where the interactive page is, if it was published: click a bar, hear it. The
      # artifacts above are the same analysis as files.
      'viewer': dict(viewer or {'published': False, 'reason': 'not attempted'}),
  }


def _reading(clamped: int) -> str:
  if clamped:
    return ('%d bars had to be clamped: the fit was erratic there, so their times are a '
            'plausible guess rather than a measurement.' % clamped)
  return 'No bar had to be clamped: every bar boundary was found by the fit.'


def _silent_wav(seconds: float) -> bytes:
  """A second of silence, for the fake run: a real RIFF file, not a stub by extension."""
  import io

  import numpy as np
  import soundfile as sf
  buffer = io.BytesIO()
  sf.write(buffer, np.zeros(int(22050 * seconds), dtype='float32'), 22050, format='WAV')
  return buffer.getvalue()


def _tiny_png() -> bytes:
  """A 2x2 PNG, so the fake run has an artifact of the right kind and mime."""
  import base64
  return base64.b64decode(
      b'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAF0lEQVQI12P8//8/AzbAxIAH'
      b'HDUKAGCgAwGvRQpbAAAAAElFTkSuQmCC')


def _flag(env: str, default: bool = True) -> bool:
  """A boolean from the environment: anything but a clear no means yes."""
  raw = os.environ.get(env)
  if raw is None:
    return default
  return raw.strip().lower() not in ('0', 'no', 'off', 'false')


def plugin(**kwargs) -> NoteSeqAlign:
  """Factory form, which is how the loader instantiates a configurable plugin."""
  return NoteSeqAlign(**kwargs)
