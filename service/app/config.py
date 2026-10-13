"""Where the service keeps things, and what it is allowed to do.

The service is deliberately self-contained: it owns `service/data/` and never writes
into the analyzer's cache or output directories, so running it cannot disturb the
desktop app and deleting `data/` resets it completely. The *code* it runs is the
analyzer's -- the same alignment, the same engraving, the same measure map -- which is
the whole point of putting it in this repository rather than beside it.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys

SERVICE_DIR = pathlib.Path(__file__).resolve().parents[1]     # service/
REPO_ROOT = SERVICE_DIR.parent                                # the note-seq checkout
ANALYZER_DIR = REPO_ROOT / 'analyzer'

# The analyzer is imported from the checkout this service lives in, not vendored: the
# package is `analyzer/analyzer/`, so the directory *holding* it has to be on the path.
# Doing it here, at import of this module, is deliberate -- every other module in this
# package ends up importing the analyzer at some point, and a path that is only correct
# after the analysis has run once is a bug that appears the first time the server is
# restarted against a piece that is already on disk.
if str(ANALYZER_DIR) not in sys.path:
  sys.path.insert(0, str(ANALYZER_DIR))


def _path(env: str, default: pathlib.Path) -> pathlib.Path:
  raw = os.environ.get(env)
  return pathlib.Path(raw).expanduser() if raw else default


class Settings:
  """Paths, limits and the two external tools the pipeline shells out to.

  Everything is overridable by environment variable so tests can point at a temporary
  tree -- the same convention `AnalyzerConfig` uses in the analyzer.
  """

  def __init__(self, data_dir: pathlib.Path | None = None,
               database: pathlib.Path | None = None):
    self.data_dir = pathlib.Path(data_dir) if data_dir else _path(
        'SERVICE_DATA', SERVICE_DIR / 'data')
    self.database = pathlib.Path(database) if database else _path(
        'SERVICE_DB', self.data_dir / 'service.db')
    self.frontend_dist = _path('SERVICE_FRONTEND', SERVICE_DIR / 'frontend' / 'dist')

    # The interpreter that has note_seq and the analyzer's dependencies. The pipeline
    # builds the reference MIDI with `memorization/analyze.py`, which needs this one --
    # not whatever python happens to be running the server.
    self.python = os.environ.get('SERVICE_PYTHON') or str(REPO_ROOT / '.venv' / 'bin' / 'python')

    # Uploads. A ten-minute piece of music is a few megabytes of MusicXML and tens of
    # megabytes of audio; anything past this is a mistake worth reporting early rather
    # than after a minute of work.
    self.max_score_mb = float(os.environ.get('SERVICE_MAX_SCORE_MB', 32))
    self.max_audio_mb = float(os.environ.get('SERVICE_MAX_AUDIO_MB', 200))

    # How long a worker's claim on a job lasts before another worker may take it. A
    # long piece takes a couple of minutes to align; this is well past that, and a
    # worker that dies is picked up again rather than lost.
    self.lease_seconds = float(os.environ.get('SERVICE_LEASE_SECONDS', 600))
    self.job_attempts = int(os.environ.get('SERVICE_JOB_ATTEMPTS', 3))

    # Run the worker inside the API process (the default: one command to start) or in
    # its own process (`python -m service.app.worker`).
    self.embedded_worker = _flag('SERVICE_EMBEDDED_WORKER', True)
    self.poll_seconds = float(os.environ.get('SERVICE_POLL_SECONDS', 1.0))

  # ------------------------------------------------------------------ paths

  @property
  def jobs_dir(self) -> pathlib.Path:
    return self.data_dir / 'jobs'

  @property
  def pieces_dir(self) -> pathlib.Path:
    return self.data_dir / 'pieces'

  def job_dir(self, job_id: str) -> pathlib.Path:
    return self.jobs_dir / job_id

  def piece_dir(self, slug: str) -> pathlib.Path:
    return self.pieces_dir / slug

  def ensure_dirs(self) -> None:
    for path in (self.data_dir, self.jobs_dir, self.pieces_dir):
      path.mkdir(parents=True, exist_ok=True)

  # ------------------------------------------------------------------ tools

  @property
  def ffmpeg(self) -> str | None:
    return shutil.which('ffmpeg')

  @property
  def ffprobe(self) -> str | None:
    return shutil.which('ffprobe')

  def capabilities(self) -> dict:
    """What this installation can actually do, so the UI can say so up front.

    YouTube needs `yt-dlp`; converting an upload needs `ffmpeg`; the reference MIDI
    needs the checkout's own `memorization/analyze.py` and the interpreter that can
    import `note_seq`. Each is reported rather than assumed: a form that offers a
    YouTube box on a machine that cannot fetch YouTube is a trap.
    """
    import importlib.util
    return {
        'python': pathlib.Path(self.python).is_file(),
        'ffmpeg': bool(self.ffmpeg),
        'yt_dlp': importlib.util.find_spec('yt_dlp') is not None,
        'analyze': (REPO_ROOT / 'memorization' / 'analyze.py').is_file(),
        'analyzer': (ANALYZER_DIR / 'analyzer' / 'align.py').is_file(),
    }

  def describe(self) -> dict:
    return {'data_dir': str(self.data_dir), 'database': str(self.database),
            'frontend': str(self.frontend_dist),
            'frontend_built': (self.frontend_dist / 'index.html').is_file(),
            'capabilities': self.capabilities()}


def _flag(env: str, default: bool = True) -> bool:
  raw = os.environ.get(env)
  if raw is None:
    return default
  return raw.strip().lower() not in ('0', 'no', 'off', 'false')


def analyzer_importable() -> bool:
  """Can the analyzer actually be imported? (The path is set above; this checks it.)

  Saying so once, at the start of a job, keeps the error message honest instead of a
  bare ImportError three frames down inside the alignment.
  """
  try:
    import analyzer.align  # noqa: F401
  except Exception:
    return False
  return True
