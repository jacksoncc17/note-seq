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

# Where `service/run.sh` serves the page, and what `NOTE_SEQ_VIEWER_URL` means by default.
# Named here because "the address is the same as the link unless it says otherwise" needs
# to know when the link is only loopback.
DEFAULT_VIEWER_URL = 'http://localhost:8100'
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

    # Serving under the broker's own origin (a path prefix, same host) is what makes the
    # pages need no password: the browser already holds the broker's token, and this
    # service checks the signature on it instead of running a login of its own.
    # `SERVICE_TRUST_JWT` is the broker's SECRET_KEY; empty means no gate at all, which
    # is the right default for a service on loopback.
    self.trust_jwt = os.environ.get('SERVICE_TRUST_JWT', '').strip()
    self.allowed_subjects = os.environ.get('SERVICE_ALLOWED_SUBJECTS', '').strip()
    self.upload_token = os.environ.get('SERVICE_UPLOAD_TOKEN', '').strip()
    self.broker_login = os.environ.get('SERVICE_BROKER_LOGIN', '/login').strip()

    # Whose pieces this machine produces, when the analysis did not come from the broker.
    # A job submitted on the broker carries its owner through the worker; a job submitted to
    # *this* service (the LAN page, `curl`) has nobody to ask, so a single-user installation
    # sets this once and everything it analyses belongs to them. Empty means the pieces are
    # unowned, which only an operator can see -- correct, and easy to fix with
    # `python -m service.app.manage --adopt-unowned --owner <sub>`.
    self.owner = os.environ.get('SERVICE_OWNER', '').strip()

    # A deployment that only serves finished pieces should not offer a form that queues
    # an analysis nobody will ever run: no worker is attached there. The broker's own
    # page is where jobs are submitted.
    self.read_only = _flag('SERVICE_READ_ONLY', False)

    # Publishing a piece to the page that serves it: where to send it, and what to send
    # with. This is the *outbox* side, so it belongs to the machine that analyses -- a
    # serving deployment leaves `SERVICE_PUBLISH_URL` empty and never pushes anything.
    # When it is empty nothing is queued, so the default is exactly the old behaviour.
    # An unset address follows `NOTE_SEQ_VIEWER_URL`, because the page a link points at is
    # the page the piece belongs on -- one variable, so the two cannot drift into the state
    # where the link names a deployment and the piece stays on this machine. The exception
    # is the loopback default: there the store *is* this service, and sending would be a
    # copy of a piece to itself.
    viewer = os.environ.get('NOTE_SEQ_VIEWER_URL', '').strip().rstrip('/')
    self.publish_url = (os.environ.get('SERVICE_PUBLISH_URL', '').strip().rstrip('/')
                        or (viewer if viewer and viewer != DEFAULT_VIEWER_URL else ''))
    self.publish_token = (os.environ.get('SERVICE_PUBLISH_TOKEN', '').strip()
                          or self.upload_token)
    self.publish_timeout_s = float(os.environ.get('SERVICE_PUBLISH_TIMEOUT_S', 120))

    # The other direction: does this service accept pieces from a worker? On for a
    # deployment whose whole job is to serve them, and the switch is here for a mirror
    # that should stay exactly as it is.
    self.accept_pieces = _flag('SERVICE_ACCEPT_PIECES', True)
    self.max_piece_mb = float(os.environ.get('SERVICE_MAX_PIECE_MB', 64))

    # The archive: a copy of every finished piece in an S3-compatible bucket, so the machine
    # that serves them is disposable -- wipe its volume, run one command, everything is back.
    # Deliberately not on the request path: the page never reads from here, so a home
    # connection that is down costs nothing except a stale archive. Empty URL means off.
    self.archive_url = os.environ.get('SERVICE_ARCHIVE_URL', '').strip()
    self.archive_bucket = os.environ.get('SERVICE_ARCHIVE_BUCKET', '').strip()
    self.archive_key = os.environ.get('SERVICE_ARCHIVE_KEY', '').strip()
    self.archive_secret = os.environ.get('SERVICE_ARCHIVE_SECRET', '').strip()
    # Must match the store's configured region, or Garage answers AuthorizationHeaderMalformed
    # -- which reads like a credentials problem and is not one.
    self.archive_region = os.environ.get('SERVICE_ARCHIVE_REGION', 'garage').strip()
    self.archive_timeout_s = float(os.environ.get('SERVICE_ARCHIVE_TIMEOUT_S', 120))

    # Behind a path-stripping reverse proxy (Traefik `stripPrefix`), this is the public
    # prefix, so the API's own docs and redirects name the right URLs. Empty when the
    # service is served at the root.
    self.root_path = os.environ.get('SERVICE_ROOT_PATH', '').strip()

  # ------------------------------------------------------------------ paths

  @property
  def jobs_dir(self) -> pathlib.Path:
    return self.data_dir / 'jobs'

  @property
  def pieces_dir(self) -> pathlib.Path:
    return self.data_dir / 'pieces'

  @property
  def archive_enabled(self) -> bool:
    return bool(self.archive_url and self.archive_bucket
                and self.archive_key and self.archive_secret)

  def archive_client(self):
    """A client for the archive bucket, built from these settings.

    A method rather than an import at module scope: `s3.py` is only needed by a deployment
    that archives, and the serving half should not carry even a module it never runs.
    """
    from .s3 import Client
    return Client(self.archive_url, self.archive_bucket, self.archive_key,
                  self.archive_secret, region=self.archive_region,
                  timeout=self.archive_timeout_s)

  @property
  def outbox_dir(self) -> pathlib.Path:
    """Pieces analysed here that their page has not accepted yet. See `outbox.py`."""
    return self.data_dir / 'outbox'

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

  def gate(self):
    """The front door, built from these settings. See `auth.py`."""
    from .auth import Gate
    return Gate(trust_jwt=self.trust_jwt, allowed_subjects=self.allowed_subjects,
                upload_token=self.upload_token, broker_login=self.broker_login)

  def describe(self) -> dict:
    return {'data_dir': str(self.data_dir), 'database': str(self.database),
            'read_only': self.read_only, 'root_path': self.root_path,
            'accept_pieces': self.accept_pieces, 'owner': self.owner or None,
            # Never the key or the secret: this dict is served to the browser.
            'archive': ({'endpoint': self.archive_url, 'bucket': self.archive_bucket}
                        if self.archive_enabled else None),
            'publish_url': self.publish_url or None,
            'frontend': str(self.frontend_dist),
            'frontend_built': (self.frontend_dist / 'index.html').is_file(),
            'capabilities': self.capabilities()}


def apply_worker_env(settings: Settings) -> list:
  """Fill gaps in `settings` from the worker's env file. Returns which keys were taken.

  `service/broker/worker.env` is what `run_worker.sh` sources, and it is where secrets live
  because they must not be retyped into a command line. Everything that runs on the machine
  that analyses music reads that file through here -- the worker, `service.app.publish` and
  `service.app.manage` -- because the alternative is what happened once: a token sitting in
  the file while a CLI read only the environment, and a failure that looked like TLS.

  A deliberately small parser rather than `source`: `KEY=value` lines, `#` comments, matching
  quotes stripped, nothing evaluated. Anything already in the environment wins, so a one-off
  override on the command line still works.
  """
  path = pathlib.Path(os.environ.get('NOTE_SEQ_WORKER_ENV')
                      or (SERVICE_DIR / 'broker' / 'worker.env'))
  if not path.is_file():
    return []
  taken: list = []
  for line in path.read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
      continue
    key, _, value = line.partition('=')
    key, value = key.strip(), value.strip().strip('"').strip("'")
    if not key or not value:
      continue
    if key in ('SERVICE_PUBLISH_TOKEN', 'SERVICE_UPLOAD_TOKEN') and not settings.publish_token:
      settings.publish_token = value
      taken.append(key)
    elif key in ('SERVICE_PUBLISH_URL', 'NOTE_SEQ_VIEWER_URL') and not settings.publish_url:
      settings.publish_url = value.rstrip('/')
      taken.append(key)
    elif key == 'SERVICE_ARCHIVE_URL' and not settings.archive_url:
      settings.archive_url = value.rstrip('/')
      taken.append(key)
    elif key == 'SERVICE_ARCHIVE_BUCKET' and not settings.archive_bucket:
      settings.archive_bucket = value
      taken.append(key)
    elif key == 'SERVICE_ARCHIVE_KEY' and not settings.archive_key:
      settings.archive_key = value
      taken.append(key)
    elif key == 'SERVICE_ARCHIVE_SECRET' and not settings.archive_secret:
      settings.archive_secret = value
      taken.append(key)
    elif key == 'SERVICE_ARCHIVE_REGION':
      if settings.archive_region == 'garage':          # the default, not a choice made here
        settings.archive_region = value
        taken.append(key)
  return taken


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
