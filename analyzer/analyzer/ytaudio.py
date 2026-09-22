"""Fetch a recording from YouTube once, and cache it as a local mono WAV.

Alignment and playback both need random access to decoded samples, so the only
useful product here is a file: `cache/audio/<slug>.wav` plus a JSON sidecar of
the facts only the network knows. After the first fetch the app works offline,
and a second call for the same piece must not touch YouTube at all.

yt-dlp is driven through its Python API, never a subprocess, because every
failure here has to become a short message the GUI can show -- which means
catching exceptions, not parsing stderr. Conversion to mono/16-bit PCM at
`target_sample_rate` is an explicit ffmpeg call rather than a yt-dlp
postprocessor, so the format is ours to guarantee and the step is reportable.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .config import AnalyzerConfig
from .schema import AudioSource

try:
  # Optional at import time: the data types and the UI must load without yt-dlp,
  # and _require_ytdlp turns its absence into a message a user can act on.
  import yt_dlp
except ImportError:
  yt_dlp = None                                         # type: ignore[assignment]

PathLike = Union[str, pathlib.Path]
Progress = Callable[[float, str], None]

MIN_YTDLP = (2023, 1, 1)      # noplaylist + flat-search behaviour we rely on
WAV_HEADER_BYTES = 44         # a bare header is not audio, so it is not a cache
DOWNLOAD_PERCENT = 90.0       # download owns 0-90, ffmpeg 92, done 100
CONVERT_PERCENT = 92.0


class DownloadError(RuntimeError):
  """Raised for any failure whose message is safe to show the user."""


def probe_duration(path: PathLike) -> float:
  """Length of an audio file in seconds, measured by ffprobe.

  Args:
    path: File to measure.
  Returns:
    Duration in seconds, always greater than zero.
  Raises:
    DownloadError: If ffprobe is missing or the file is not readable audio.
  """
  out = _ffprobe(['-show_entries', 'format=duration',
                  '-of', 'default=noprint_wrappers=1:nokey=1'], path)
  try:
    seconds = float(out.splitlines()[0])
  except (IndexError, ValueError) as exc:
    raise DownloadError('ffprobe found no duration in %s, so it is not readable '
                        'audio' % pathlib.Path(path).name) from exc
  if not seconds > 0:
    raise DownloadError('%s holds no audio (zero length)' % pathlib.Path(path).name)
  return seconds


def probe_stream(path: PathLike) -> Tuple[int, int]:
  """Sample rate and channel count of a file's first audio stream.

  Args:
    path: File to inspect.
  Returns:
    A `(sample_rate, channels)` pair, both greater than zero.
  Raises:
    DownloadError: If ffprobe is missing or there is no usable audio stream.
  """
  out = _ffprobe(['-select_streams', 'a:0', '-show_entries',
                  'stream=sample_rate,channels', '-of', 'json'], path)
  try:
    stream = (json.loads(out).get('streams') or [{}])[0]
    rate, channels = int(stream.get('sample_rate') or 0), int(stream.get('channels') or 0)
  except (ValueError, TypeError, AttributeError, IndexError) as exc:
    raise DownloadError('%s has no audio stream ffprobe can describe'
                        % pathlib.Path(path).name) from exc
  if rate <= 0 or channels <= 0:
    raise DownloadError('%s has no usable audio stream' % pathlib.Path(path).name)
  return rate, channels


def _ffprobe(args: List[str], path: PathLike) -> str:
  """Run ffprobe on `path` and return its stdout, or raise DownloadError."""
  exe = shutil.which('ffprobe')
  if exe is None:
    raise DownloadError('ffprobe is not installed or not on PATH, so the audio '
                        'cannot be inspected (install ffmpeg)')
  try:
    proc = subprocess.run([exe, '-v', 'error'] + args + [str(path)],
                          capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)
  except (OSError, subprocess.SubprocessError) as exc:
    raise DownloadError('ffprobe could not be started: %s' % _short(exc)) from exc
  if proc.returncode != 0:
    raise DownloadError('ffprobe could not read %s: %s'
                        % (pathlib.Path(path).name,
                           _short(proc.stderr) or 'unknown ffprobe error'))
  return proc.stdout.strip()


def is_cached(cfg: AnalyzerConfig, slug: str) -> bool:
  """Report whether `slug` already has both a usable WAV and its metadata.

  A file no bigger than a WAV header does not count: it means an earlier run died
  mid-conversion, and fetching again is the honest answer.

  Args:
    cfg: Layout describing where the cache lives.
    slug: Piece the recording belongs to.
  Returns:
    True when the cached WAV and its JSON sidecar are both present.
  """
  return bool(_big_enough(cfg.audio_path(slug))
              and pathlib.Path(cfg.audio_meta_path(slug)).is_file())


def load_cached(cfg: AnalyzerConfig, slug: str) -> Optional[AudioSource]:
  """Read the cached recording, or None when there is nothing usable.

  The WAV is the truth about the audio and the JSON only a cache of the
  network-only facts, so a missing, truncated or hand-edited sidecar costs the
  title and video id but not the recording: duration, sample rate and channel
  count get re-probed from the file itself.

  Args:
    cfg: Layout describing where the cache lives.
    slug: Piece the recording belongs to.
  Returns:
    The AudioSource for the cached WAV, or None when the WAV is missing or empty.
  """
  wav = pathlib.Path(cfg.audio_path(slug))
  if not _big_enough(wav):
    return None
  try:
    loaded = json.loads(pathlib.Path(cfg.audio_meta_path(slug)).read_text(encoding='utf-8'))
  except (OSError, ValueError):
    loaded = {}
  # The defaults supply the two required fields, so a sidecar holding only part
  # of the record still contributes the parts it does have.
  source = AudioSource.from_dict(
      dict({'slug': slug, 'path': str(wav)}, **(loaded if isinstance(loaded, dict) else {})))
  duration = _num(source.duration_s)
  rate, channels = int(_num(source.sample_rate)), int(_num(source.channels))
  if rate <= 0 or channels <= 0:
    try:
      probed = probe_stream(wav)
      rate, channels = rate or probed[0], channels or probed[1]
    except DownloadError:
      pass                    # an odd file is still worth returning
  if duration <= 0:
    try:
      duration = probe_duration(wav)
    except DownloadError:
      duration = 0.0
  return AudioSource(
      slug=slug, path=str(wav), url=str(source.url or ''),
      source_id=str(source.source_id or ''), title=str(source.title or ''),
      duration_s=duration, sample_rate=rate, channels=channels,
      fetched_at=str(source.fetched_at or ''))


def delete_cached(cfg: AnalyzerConfig, slug: str) -> bool:
  """Remove a cached recording, its metadata and any leftover partial download.

  Args:
    cfg: Layout describing where the cache lives.
    slug: Piece to forget.
  Returns:
    True when at least one file was deleted, False when there was nothing to do.
  """
  victims = [pathlib.Path(cfg.audio_path(slug)), pathlib.Path(cfg.audio_meta_path(slug))]
  victims += _scratch_files(pathlib.Path(cfg.cache_dir) / 'downloads', slug)
  removed = False
  for path in victims:
    if path.exists():
      _remove(path)
      removed = True
  return removed


def download_audio(cfg: AnalyzerConfig, slug: str, url: str,
                   progress: Optional[Progress] = None,
                   force: bool = False) -> AudioSource:
  """Download a recording once and cache it as a mono WAV plus metadata.

  Args:
    cfg: Layout to write into; `ensure_dirs()` is called first.
    slug: Piece the recording belongs to, and the cache key.
    url: The YouTube link.
    progress: Optional `progress(percent, message)` callback for the UI.
    force: Re-download even when a cached copy exists.
  Returns:
    The AudioSource for the cached WAV.
  Raises:
    DownloadError: For any failure, with a message fit to show the user.
  """
  cfg.ensure_dirs()
  if not force and is_cached(cfg, slug):
    cached = load_cached(cfg, slug)
    if cached is not None:
      return cached
  _require_ytdlp()
  report = _reporter(progress)
  link = (url or '').strip()
  if not link:
    raise DownloadError('no YouTube link was given for %s' % slug)
  scratch = pathlib.Path(cfg.cache_dir) / 'downloads'
  scratch.mkdir(parents=True, exist_ok=True)
  report(0.0, 'contacting YouTube')
  info = _extract(link, scratch, slug, report)
  try:
    raw = _raw_of(info, scratch, slug)
    if _size(raw) <= 0:
      raise DownloadError('the download produced an empty file (0 bytes), so '
                          'there is no audio to use')
    report(CONVERT_PERCENT, 'converting')
    wav = pathlib.Path(cfg.audio_path(slug))
    _to_wav(raw, wav, cfg.target_sample_rate)
    size = _size(wav)
    if size <= WAV_HEADER_BYTES:
      _remove(wav)
      raise DownloadError('the converted WAV holds no audio (%d bytes), so the '
                          'download is unusable' % size)
    rate, channels = probe_stream(wav)
    # fetched_at rides along in to_dict(), so the sidecar is the AudioSource
    # verbatim plus the timestamp the app may want to show.
    source = AudioSource(
        slug=slug, path=str(wav), url=link, source_id=str(info.get('id') or ''),
        title=str(info.get('title') or ''), duration_s=probe_duration(wav),
        sample_rate=rate, channels=channels, fetched_at=_now())
    pathlib.Path(cfg.audio_meta_path(slug)).write_text(
        json.dumps(source.to_dict(), indent=2) + '\n', encoding='utf-8')
    report(100.0, 'ready')
    return source
  finally:
    _cleanup(scratch, slug)   # the raw download is an intermediate: drop it


def fetch(cfg: AnalyzerConfig, slug: str, url: str,
          progress: Optional[Progress] = None,
          force: bool = False) -> AudioSource:
  """Alias for `download_audio`, the name the GUI calls.

  Args:
    cfg: Layout to write into.
    slug: Piece the recording belongs to.
    url: The YouTube link.
    progress: Optional `progress(percent, message)` callback for the UI.
    force: Re-download even when a cached copy exists.
  Returns:
    The AudioSource for the cached WAV.
  Raises:
    DownloadError: For any failure, with a message fit to show the user.
  """
  return download_audio(cfg, slug, url, progress=progress, force=force)


def search_youtube(query: str, limit: int = 5) -> List[Dict[str, Any]]:
  """Search YouTube for candidate recordings, without downloading anything.

  Uses `ytsearchN:` with `extract_flat`, so yt-dlp returns a listing instead of
  resolving -- let alone fetching -- each video.

  Args:
    query: Free text, e.g. "chopin op 34 no 2".
    limit: Maximum number of rows to return.
  Returns:
    Rows of `{'id', 'title', 'duration_s', 'uploader'}`, best match first; an
    empty list when the search matched nothing.
  Raises:
    DownloadError: When the search itself fails (network, rejected query).
  """
  text, count = (query or '').strip(), int(limit or 0)
  if not text or count <= 0:
    return []
  _require_ytdlp()
  opts = {'quiet': True, 'no_warnings': True, 'skip_download': True,
          'extract_flat': True, 'noplaylist': True}
  try:
    with yt_dlp.YoutubeDL(opts) as ydl:                 # type: ignore[union-attr]
      info = ydl.extract_info('ytsearch%d:%s' % (count, text), download=False)
    # Materialise inside the try: a flat listing is lazy and can fail late.
    entries = info.get('entries') if isinstance(info, dict) else None
    found = [e for e in list(entries or []) if isinstance(e, dict)]
  except Exception as exc:          # yt-dlp raises assorted types, all unusable
    raise DownloadError('could not search YouTube for %r: %s'
                        % (text, _why(exc))) from exc
  return [{'id': str(e.get('id') or ''), 'title': str(e.get('title') or ''),
           'duration_s': round(_num(e.get('duration')), 3),
           'uploader': str(e.get('uploader') or e.get('channel') or '')}
          for e in found[:count]]


def _extract(link: str, scratch: pathlib.Path, slug: str,
             report: Progress) -> Dict[str, Any]:
  """Run yt-dlp once for `link` and return its info dict."""
  opts = {
      'format': 'bestaudio/best',
      'noplaylist': True,
      'quiet': True,
      'no_warnings': True,
      # `quiet` alone still leaves yt-dlp's progress bar on stdout, which a Qt
      # app has no console for; the hooks below are what report progress.
      'noprogress': True,
      'overwrites': True,
      'retries': 3,
      'socket_timeout': 30,
      'outtmpl': str(scratch / (slug + '.%(ext)s')),
      'progress_hooks': [_hook(report)],
  }
  try:
    with yt_dlp.YoutubeDL(opts) as ydl:                 # type: ignore[union-attr]
      info = ydl.extract_info(link, download=True)
  except DownloadError:
    raise
  except Exception as exc:
    raise DownloadError('could not download %s: %s' % (link, _why(exc))) from exc
  if not isinstance(info, dict):
    raise DownloadError('yt-dlp returned nothing for %s; the link may point at a '
                        'playlist rather than one video' % link)
  return info


def _raw_of(info: Dict[str, Any], scratch: pathlib.Path, slug: str) -> pathlib.Path:
  """Find the file yt-dlp actually wrote for this download."""
  for entry in list(info.get('requested_downloads') or []) + [info]:
    if isinstance(entry, dict):
      for key in ('filepath', '_filename', 'filename'):
        if entry.get(key) and _size(entry[key]) > 0:
          return pathlib.Path(entry[key])
  # No usable path in the info dict (older yt-dlp, or a hook-driven download):
  # fall back to the newest complete file this run left in the scratch dir.
  complete = [p for p in _scratch_files(scratch, slug) if not p.name.endswith('.part')]
  if complete:
    return max(complete, key=lambda p: p.stat().st_mtime)
  raise DownloadError('yt-dlp reported success but wrote no file, so there is '
                      'nothing to convert')


def _to_wav(src: PathLike, dest: pathlib.Path, sample_rate: int) -> None:
  """Transcode `src` to mono 16-bit PCM WAV at `dest`, atomically."""
  exe = shutil.which('ffmpeg')
  if exe is None:
    raise DownloadError('ffmpeg is not installed or not on PATH, so the download '
                        'cannot be converted to WAV (install ffmpeg)')
  temp = dest.with_name(dest.name + '.part')   # same directory: os.replace is atomic
  _remove(temp)
  cmd = [exe, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(src),
         '-vn', '-ac', '1', '-ar', str(int(sample_rate)),
         '-c:a', 'pcm_s16le', '-f', 'wav', str(temp)]
  try:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                          stdin=subprocess.DEVNULL)
  except (OSError, subprocess.SubprocessError) as exc:
    _remove(temp)
    raise DownloadError('ffmpeg could not be started: %s' % _short(exc)) from exc
  if proc.returncode != 0 or _size(temp) <= 0:
    _remove(temp)
    raise DownloadError('ffmpeg could not convert the download to WAV: %s'
                        % (_short(proc.stderr) or 'unknown ffmpeg error'))
  os.replace(temp, dest)


def _reporter(progress: Optional[Progress]) -> Progress:
  """Wrap a UI callback so a broken one can never lose a finished download."""
  def report(percent: float, message: str) -> None:
    if progress is None:
      return
    try:
      progress(max(0.0, min(100.0, float(percent))), str(message))
    except Exception:      # the download is worth more than the progress bar
      pass
  return report


def _hook(report: Progress):
  """Map a yt-dlp progress dict onto the UI's (percent, message) contract.

  The download owns 0-90%: yt-dlp cannot know how long ffmpeg will take, and
  claiming more would make the bar jump backwards at the conversion step.
  """
  def on_progress(status: Dict[str, Any]) -> None:
    state = status.get('status')
    if state == 'downloading':
      total = status.get('total_bytes') or status.get('total_bytes_estimate') or 0
      done = status.get('downloaded_bytes') or 0
      percent = (float(done) / float(total) * DOWNLOAD_PERCENT) if total else 0.0
      report(min(percent, DOWNLOAD_PERCENT), 'downloading')
    elif state in ('finished', 'error'):
      report(DOWNLOAD_PERCENT if state == 'finished' else 0.0,
             'download complete' if state == 'finished' else 'download failed')
  return on_progress


def _require_ytdlp():
  """Return the yt_dlp module, or raise DownloadError saying why it is unusable."""
  if yt_dlp is None:
    raise DownloadError('yt-dlp is not installed, so YouTube links cannot be '
                        'fetched (try: pip install yt-dlp)')
  version = getattr(getattr(yt_dlp, 'version', None), '__version__', '')
  if not version:
    raise DownloadError('this yt-dlp build reports no version and is too old to '
                        'use; upgrade it with: pip install -U yt-dlp')
  parts = (tuple(int(p) for p in re.findall(r'\d+', version)) + (0, 0, 0))[:3]
  if parts < MIN_YTDLP:
    raise DownloadError('yt-dlp %s is too old for this app (need %s or newer); '
                        'upgrade it with: pip install -U yt-dlp'
                        % (version, '.'.join(str(p) for p in MIN_YTDLP)))
  return yt_dlp


# Common yt-dlp complaints and the sentence to lead with. The first row whose
# keywords appear in the message wins.
_CAUSES = (
    (('ffmpeg', 'ffprobe'), 'ffmpeg could not handle the audio'),
    (('private', 'sign in', 'log in', 'members-only', 'age-restricted',
      'confirm your age', 'premium'),
     'this video is private or needs a signed-in account'),
    (('unavailable', 'removed', 'deleted', 'no longer', 'not available', 'blocked'),
     'this video is unavailable: removed, blocked, or never public'),
    (('unsupported url', 'not a valid url', 'invalid url', 'no video'),
     'that is not a YouTube video link yt-dlp can open'),
    (('timed out', 'timeout', 'network', 'connection', 'unreachable',
      'temporary failure', 'getaddrinfo', 'resolve', 'ssl', 'http error 5'),
     'the network failed while talking to YouTube'),
)


def _why(exc: BaseException) -> str:
  """Say in one line what went wrong, in terms the user can act on.

  yt-dlp's wording is verbose but specific, so a recognised cause gets a plain
  sentence in front and the original text in brackets for a bug report.
  """
  text = _short(exc) or exc.__class__.__name__
  low = text.lower()
  for needles, message in _CAUSES:
    if any(needle in low for needle in needles):
      return '%s (%s)' % (message, text)
  return text


def _short(text: Any, limit: int = 200) -> str:
  """Collapse tool output to one trimmed line that fits in a dialog."""
  clean = ' '.join(str(text or '').split())
  clean = clean[6:].strip() if clean.upper().startswith('ERROR:') else clean
  return clean[:limit] + ('...' if len(clean) > limit else '')


def _scratch_files(scratch: PathLike, slug: str) -> List[pathlib.Path]:
  """Files in the scratch dir belonging to `slug`.

  Scanned, not globbed: a slug is user-derived text and may contain `[` or `*`.
  """
  try:
    entries = list(os.scandir(scratch))
  except OSError:
    return []
  return [pathlib.Path(e.path) for e in entries
          if e.is_file() and e.name.startswith(slug + '.')]


def _cleanup(scratch: PathLike, slug: str) -> None:
  """Delete the raw download and its partials, keeping only the WAV and JSON."""
  for path in _scratch_files(scratch, slug):
    _remove(path)


def _remove(path: PathLike) -> None:
  try:
    pathlib.Path(path).unlink()
  except OSError:
    pass


def _size(path: PathLike) -> int:
  try:
    return pathlib.Path(path).stat().st_size
  except OSError:
    return 0


def _big_enough(path: PathLike) -> bool:
  """True when `path` is a file holding more than a WAV header."""
  return pathlib.Path(path).is_file() and _size(path) > WAV_HEADER_BYTES


def _num(value: Any, default: float = 0.0) -> float:
  """Coerce a JSON or scraped value into a finite number.

  `float()` happily accepts 'nan' and 'inf' -- and JSON allows them -- which
  `int()` then refuses, so a corrupt sidecar could otherwise raise out of
  `load_cached` instead of falling back to the file on disk.
  """
  try:
    number = float(value)
  except (TypeError, ValueError):
    return default
  return number if math.isfinite(number) else default


def _now() -> str:
  """Current UTC time, second precision, for `AudioSource.fetched_at`."""
  return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
