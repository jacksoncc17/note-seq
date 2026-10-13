"""The HTTP API, and the page that uses it.

Seven endpoints, and the shape of them is the whole design:

    POST /api/v1/jobs                  upload a score and a recording (or a URL)
    GET  /api/v1/jobs                  the queue, newest first -- the list you click
    GET  /api/v1/jobs/{id}             one job: status, progress, log, and its piece
    POST /api/v1/jobs/{id}/retry       put a failed job back in the queue
    GET  /api/v1/pieces                what has been analysed
    GET  /api/v1/pieces/{slug}         the score page's data: map, pages, measure boxes
    GET  /api/v1/pieces/{slug}/pages/{n}.svg
    GET  /api/v1/pieces/{slug}/audio?first_bar=&last_bar=   -- or ?start=&end= seconds

The last one is the point of the whole thing: bars in, a WAV of exactly those bars out.
It answers in the *map's* seconds, so "bars 40-83" means here what it means in the
desktop app, and it is a real file with range support, so the browser can seek in it.
"""

from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .jobs import JobStore
from .pieces import AUDIO_SUFFIXES, SCORE_SUFFIXES, PieceStore
from .worker import WorkerThread

# A range is trimmed this much beyond the bars themselves, exactly as the desktop app
# plays a passage: starting exactly on the downbeat clips the piano's attack, and
# stopping exactly on the last beat cuts the pedal. Both are audible.
PRE_ROLL_S = 0.12
POST_ROLL_S = 0.25

API = '/api/v1'


def create_app(settings: Optional[Settings] = None) -> FastAPI:
  settings = settings or Settings()
  settings.ensure_dirs()
  store = JobStore(settings)
  pieces = PieceStore(settings)
  worker: Optional[WorkerThread] = None

  @asynccontextmanager
  async def lifespan(_app: FastAPI):
    """Start the worker with the API, and stop it with the API.

    Embedded by default: one command starts everything, and the worker's thread is a
    daemon that dies with the process. `SERVICE_EMBEDDED_WORKER=0` leaves the queue for
    `python -m service.app.worker`, which is what you want when an alignment should not
    share a process with FluidSynth and the DTW.
    """
    nonlocal worker
    if settings.embedded_worker:
      worker = WorkerThread(settings, store=store)
      _app.state.worker = worker
      worker.start()
    try:
      yield
    finally:
      if worker is not None:
        worker.shutdown()

  app = FastAPI(title='note-seq service', version='1.0', lifespan=lifespan,
                description='Upload a score and a recording; read the score and listen '
                            'to any range of bars.')
  app.state.settings = settings
  app.state.jobs = store
  app.state.pieces = pieces

  # The frontend is served from this origin in production and from Vite's own port in
  # development, which is what this is for.
  app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'],
                     allow_headers=['*'])

  # ------------------------------------------------------------------ health

  @app.get(API + '/status')
  def status() -> Dict[str, Any]:
    """What this service is, and what it can do on this machine."""
    return {'service': 'note-seq', 'version': app.version,
            'jobs': store.stats(),
            'pieces': len(pieces.slugs()),
            **settings.describe()}

  # ------------------------------------------------------------------ jobs

  @app.post(API + '/jobs', status_code=201)
  async def create_job(
      score: UploadFile = File(..., description='MusicXML (.musicxml, .xml or .mxl)'),
      audio: Optional[UploadFile] = File(None, description='mp3, wav, flac, m4a, ogg'),
      youtube_url: str = Form('', description='or a YouTube URL instead of a file'),
      title: str = Form('', description='what to call the piece'),
  ) -> Dict[str, Any]:
    """Accept an upload and queue it. The work happens on the worker, not here."""
    name = (score.filename or 'score.musicxml')
    suffix = pathlib.Path(name).suffix.lower()
    if suffix not in SCORE_SUFFIXES:
      raise HTTPException(400, 'the score must be MusicXML (%s), not %r'
                               % (', '.join(SCORE_SUFFIXES), suffix or name))
    url = (youtube_url or '').strip()
    if audio is None and not url:
      raise HTTPException(400, 'give either an audio file or a YouTube URL')
    if not settings.capabilities()['ffmpeg']:
      raise HTTPException(503, 'ffmpeg is not installed on this machine, so the '
                               'recording cannot be read')
    if url and not settings.capabilities()['yt_dlp']:
      raise HTTPException(503, 'yt-dlp is not installed on this machine, so a YouTube '
                               'URL cannot be fetched; upload an audio file instead')

    job = store.create({'score': '', 'audio': None, 'youtube_url': url, 'title': title},
                       title=title or pathlib.Path(name).stem)
    job_dir = settings.job_dir(job.id)
    job_dir.mkdir(parents=True, exist_ok=True)

    score_path = job_dir / ('score' + suffix)
    await _save(score, score_path, settings.max_score_mb, 'score')
    audio_path: Optional[str] = None
    if audio is not None:
      audio_name = (audio.filename or 'audio.mp3')
      audio_suffix = pathlib.Path(audio_name).suffix.lower()
      if audio_suffix not in AUDIO_SUFFIXES:
        raise HTTPException(400, 'that audio format is not recognised: %r'
                                 % (audio_suffix or audio_name))
      target = job_dir / ('upload' + audio_suffix)
      await _save(audio, target, settings.max_audio_mb, 'audio')
      audio_path = str(target)

    params = {'score': str(score_path), 'audio': audio_path, 'youtube_url': url,
              'title': title, 'job_id': job.id}
    store.set_params(job.id, params)
    store.append_log(job.id, None, 'queued: %s' % _describe(params))
    return _job_json(store.get(job.id))

  @app.get(API + '/jobs')
  def list_jobs(limit: int = 50, status: Optional[str] = None) -> Dict[str, Any]:
    return {'jobs': [_job_json(job, brief=True) for job in store.list(limit, status)],
            'stats': store.stats()}

  @app.get(API + '/jobs/{job_id}')
  def get_job(job_id: str) -> Dict[str, Any]:
    job = store.get(job_id)
    if job is None:
      raise HTTPException(404, 'no such job')
    return _job_json(job)

  @app.post(API + '/jobs/{job_id}/retry')
  def retry_job(job_id: str) -> Dict[str, Any]:
    job = store.get(job_id)
    if job is None:
      raise HTTPException(404, 'no such job')
    if job.status != 'failed':
      raise HTTPException(409, 'only a failed job can be queued again')
    store.requeue(job_id, 'queued again by hand')
    return _job_json(store.get(job_id))

  # ------------------------------------------------------------------ pieces

  @app.get(API + '/pieces')
  def list_pieces() -> Dict[str, Any]:
    return {'pieces': pieces.listing()}

  @app.get(API + '/pieces/{slug}')
  def get_piece(slug: str) -> Dict[str, Any]:
    """Everything the score page needs, in one request."""
    meta = pieces.meta(slug)
    map_dict = pieces.map(slug)
    geometry = pieces.geometry(slug)
    if meta is None or map_dict is None or geometry is None:
      raise HTTPException(404, 'no such piece: %s' % slug)
    return {'meta': meta, 'map': map_dict, 'geometry': geometry,
            'audio': {'url': '%s/pieces/%s/audio' % (API, slug),
                      'duration_s': meta.get('duration_s') or 0.0}}

  @app.get(API + '/pieces/{slug}/pages/{number}.svg')
  def get_page(slug: str, number: int) -> Response:
    path = pieces.page_path(slug, number)
    if not path.is_file():
      raise HTTPException(404, 'no page %d for %s' % (number, slug))
    return Response(path.read_text(encoding='utf-8'), media_type='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})

  @app.get(API + '/pieces/{slug}/audio')
  def get_audio(slug: str, first_bar: Optional[int] = None,
                last_bar: Optional[int] = None, start: Optional[float] = None,
                end: Optional[float] = None, pad: int = 1) -> Response:
    """A WAV of the recording -- all of it, or exactly the bars asked for.

    Bars are answered from the map, so the clip is the music of those bars and not the
    stretch of seconds they happen to occupy in some other performance. `pad=1` keeps
    the desktop app's small run-in and run-out.
    """
    meta = pieces.meta(slug)
    map_dict = pieces.map(slug)
    if meta is None or map_dict is None:
      raise HTTPException(404, 'no such piece: %s' % slug)
    audio_path = pieces.audio(slug)
    if not audio_path.is_file():
      raise HTTPException(404, 'the recording for %s is missing' % slug)

    if first_bar is not None or last_bar is not None:
      if first_bar is None or last_bar is None:
        raise HTTPException(400, 'give both first_bar and last_bar')
      span = range_for_bars(map_dict, int(first_bar), int(last_bar))
      if span is None:
        raise HTTPException(404, 'the map has nothing for bars %s-%s'
                                 % (first_bar, last_bar))
      start_s, end_s = span
      if pad:
        start_s -= PRE_ROLL_S
        end_s += POST_ROLL_S
    elif start is not None or end is not None:
      start_s = float(start or 0.0)
      end_s = float(end if end is not None else meta.get('duration_s') or 0.0)
    else:
      # The whole recording: a real file response, so the browser can seek in it and
      # ask for the parts it wants rather than pulling 13 MB before playing a note.
      return FileResponse(str(audio_path), media_type='audio/wav',
                          filename='%s.wav' % slug)

    from analyzer.server import slice_file
    payload, problem = slice_file(audio_path, max(0.0, start_s), end_s)
    if payload is None:
      raise HTTPException(400, problem or 'that range could not be cut')
    return _wav_response(payload, slug, (start_s, end_s))

  # ------------------------------------------------------------------ frontend

  _mount_frontend(app, settings)
  return app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _save(upload: UploadFile, path: pathlib.Path, limit_mb: float,
                what: str) -> None:
  """Stream an upload to disk, refusing anything past the limit before writing it."""
  limit = int(limit_mb * 1024 * 1024)
  written = 0
  path.parent.mkdir(parents=True, exist_ok=True)
  with open(path, 'wb') as handle:
    while True:
      block = await upload.read(1 << 20)
      if not block:
        break
      written += len(block)
      if written > limit:
        handle.close()
        path.unlink(missing_ok=True)
        raise HTTPException(413, 'that %s is larger than %g MB' % (what, limit_mb))
      handle.write(block)
  if written == 0:
    path.unlink(missing_ok=True)
    raise HTTPException(400, 'the %s arrived empty' % what)


def _wav_response(payload: bytes, slug: str, span) -> Response:
  headers = {'Content-Disposition': 'inline; filename="%s.wav"' % slug,
             'Cache-Control': 'public, max-age=300'}
  if span is not None:
    # The clip starts at a known point in the recording, which is what lets the page
    # put a bar under the playhead: clip time + this offset = the map's seconds.
    headers['X-Clip-Start'] = '%.3f' % float(span[0])
    headers['X-Clip-End'] = '%.3f' % float(span[1])
  headers['Access-Control-Expose-Headers'] = 'X-Clip-Start, X-Clip-End'
  return Response(payload, media_type='audio/wav', headers=headers)


def range_for_bars(map_dict: Dict[str, Any], first: int, last: int):
  """(start_s, end_s) for bars `first`..`last` inclusive, from the map.

  Reuses the analyzer's own rule through `SyncResult`, materialised from the stored
  map: a bar played twice by a repeat maps to the *first* pass here, which is the same
  choice the desktop app makes when you click a bar twice over.
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
                      offset_s=float(map_dict.get('offset_s') or 0.0),
                      quality=map_dict.get('quality') or {},
                      warnings=list(map_dict.get('warnings') or []))
  return result.range_for_measures(int(first), int(last))


def _job_json(job, brief: bool = False) -> Dict[str, Any]:
  if job is None:
    raise HTTPException(404, 'no such job')
  row = job.row
  out = {
      'id': job.id,
      'kind': row.get('kind'),
      'status': row.get('status'),
      'progress': round(float(row.get('progress') or 0.0), 1),
      'message': row.get('message') or '',
      'title': row.get('title') or '',
      'slug': row.get('slug'),
      'error': row.get('error'),
      'attempts': int(row.get('attempts') or 0),
      'created_at': _iso(row.get('created_at')),
      'started_at': _iso(row.get('started_at')),
      'finished_at': _iso(row.get('finished_at')),
      'seconds': round((row.get('finished_at') or 0) - (row.get('started_at') or 0), 1)
      if row.get('started_at') and row.get('finished_at') else None,
      'params': {k: v for k, v in job.params.items() if k != 'job_id'},
  }
  if not brief:
    out['log'] = row.get('log') or ''
  return out


def _iso(stamp: Optional[float]) -> Optional[str]:
  if not stamp:
    return None
  from datetime import datetime, timezone
  return datetime.fromtimestamp(float(stamp), tz=timezone.utc).isoformat(timespec='seconds')


def _describe(params: Dict[str, Any]) -> str:
  bits = ['score=%s' % pathlib.Path(params['score']).name]
  if params.get('audio'):
    bits.append('audio=%s' % pathlib.Path(params['audio']).name)
  if params.get('youtube_url'):
    bits.append('youtube=%s' % params['youtube_url'])
  return ', '.join(bits)


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
  """Serve the built React app, if it has been built.

  Vite writes hashed asset names, so the assets are mounted as static files and every
  unknown path falls back to `index.html` for the client-side routes. When `dist/` is
  missing the root path says how to build it rather than 404ing: a service that is
  running but has no page is worth explaining.
  """
  dist = settings.frontend_dist
  assets = dist / 'assets'
  if assets.is_dir():
    app.mount('/assets', StaticFiles(directory=str(assets)), name='assets')

  @app.get('/', include_in_schema=False)
  def index() -> Response:
    page = dist / 'index.html'
    if page.is_file():
      return HTMLResponse(page.read_text(encoding='utf-8'))
    return HTMLResponse(_NOT_BUILT % {'dist': dist}, status_code=200)

  @app.get('/{path:path}', include_in_schema=False)
  def spa(path: str, request: Request) -> Response:
    if path.startswith(API.lstrip('/')) or path in ('openapi.json', 'docs', 'redoc'):
      raise HTTPException(404, 'no such endpoint: /%s' % path)
    candidate = dist / path
    if candidate.is_file():
      return FileResponse(str(candidate))
    page = dist / 'index.html'
    if page.is_file():
      return HTMLResponse(page.read_text(encoding='utf-8'))
    raise HTTPException(404, 'not found: /%s' % path)


_NOT_BUILT = """<!doctype html>
<html><head><meta charset="utf-8"><title>note-seq service</title>
<style>body{font:15px/1.6 system-ui,sans-serif;max-width:44em;margin:3em auto;padding:0 1em}
code{background:#f2f2f2;padding:.1em .3em;border-radius:3px}
pre{background:#f7f7f7;padding:1em;border-radius:6px;overflow:auto}</style></head>
<body><h1>note-seq service</h1>
<p>The API is running; the web page has not been built yet. From the repository root:</p>
<pre>cd service/frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>or start it in development mode with <code>npm run dev</code>, which serves the page
itself and proxies <code>/api</code> here.</p>
<p>Expected build directory: <code>%(dist)s</code>. The API is at
<a href="/docs">/docs</a> in the meantime.</p>
</body></html>
"""


# ---------------------------------------------------------------------------
# running it
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:      # pragma: no cover - a server
  """`python -m service.app` -- the API, the worker, and the built page."""
  import argparse
  import os

  import uvicorn

  parser = argparse.ArgumentParser(description='note-seq web service')
  parser.add_argument('--host', default=os.environ.get('SERVICE_HOST', '127.0.0.1'))
  parser.add_argument('--port', type=int, default=int(os.environ.get('SERVICE_PORT', 8100)))
  parser.add_argument('--data', default=None, help='data directory (default SERVICE_DATA)')
  parser.add_argument('--reload', action='store_true', help='reload on code changes')
  parser.add_argument('--no-worker', action='store_true',
                      help='leave the queue to a separate worker process')
  args = parser.parse_args(argv)

  if args.data:
    os.environ['SERVICE_DATA'] = args.data
  if args.no_worker:
    os.environ['SERVICE_EMBEDDED_WORKER'] = '0'

  settings = Settings()
  settings.ensure_dirs()
  print('\n== note-seq service ==')
  for key, value in settings.describe().items():
    print('  %-16s %s' % (key, value))
  if settings.embedded_worker:
    print('  worker           in this process (SERVICE_EMBEDDED_WORKER=0 to separate it)')
  print('\n  http://%s:%d\n' % (args.host, args.port))
  uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level='info')
  return 0


if __name__ == '__main__':                              # pragma: no cover
  raise SystemExit(main())
