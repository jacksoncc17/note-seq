"""The analyzer over HTTP, for a phone or tablet browser.

A second front-end on the same core. Nothing here re-implements the work: the
pieces, the recordings, the maps and the engraved pages all come from the modules
the desktop app already uses, out of the same caches. Only the window is missing,
because Qt does not run in a browser.

Two decisions worth knowing:

**The recording is served from the local cache, sliced.** A phone never talks to
YouTube. yt-dlp fetched the audio once, on the machine that runs this server, and
the map's timestamps are offsets into *that* file -- `syncmap` even records its
SHA-256 so a replaced recording is detected as stale. So ``/audio/<slug>?from=&to=``
decodes the cached WAV and returns exactly that passage as a complete WAV: no
client-side seeking, no media-fragment quirks on iOS, and a start that is
sample-accurate rather than "close enough".

**Long work is not done here.** Fetching a recording, aligning and engraving are
one-time setup steps that want a keyboard and a progress bar; they stay in the
desktop app. This server reads what is already on disk and says so plainly when
something is missing.
"""

from __future__ import annotations

import asyncio
import io
import json
import pathlib
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote

from .config import AnalyzerConfig
from . import library, measures, scoreview, store, syncmap, synth
from .store import PieceQuery

# A bar's audio starts before its downbeat and ends after its last note: starting
# exactly on the barline clips the previous note's resonance. The desktop uses the
# same two numbers for the audio it puts on a card, and the phone adds them itself for
# the recording -- so a score track gets them here, where the bars are turned into
# seconds, and the phone is told which seconds it is getting.
PRE_ROLL_S = 0.12
POST_ROLL_S = 0.25


WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / 'web'
INDEX = 'index.html'
HEADER_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 30.0


def _response(status: int, content_type: str, body: bytes,
              extra: Optional[Dict[str, str]] = None) -> bytes:
  reason = {200: 'OK', 400: 'Bad Request', 404: 'Not Found', 405: 'Method Not Allowed',
            500: 'Internal Server Error'}.get(status, 'OK')
  headers = {'Content-Type': content_type,
             'Content-Length': str(len(body)),
             'Cache-Control': 'no-store',
             'Connection': 'close'}
  headers.update(extra or {})
  head = 'HTTP/1.1 %d %s\r\n' % (status, reason)
  head += ''.join('%s: %s\r\n' % (k, v) for k, v in headers.items())
  return head.encode('utf-8') + b'\r\n' + body


def json_response(payload, status: int = 200) -> bytes:
  return _response(status, 'application/json; charset=utf-8',
                   json.dumps(payload, ensure_ascii=False).encode('utf-8'))


def error_response(message: str, status: int = 400) -> bytes:
  """Every failure is JSON, so the client can always read `.error`.

  Mixing plain text and JSON errors meant half of them arrived as "HTTP 400" with
  the explanation silently dropped.
  """
  return json_response({'error': message}, status)


class Request:
  """One parsed request: method, path and query, with no reading left to do."""

  def __init__(self, method: str, path: str, query: Dict[str, List[str]]):
    self.method = method
    self.path = path
    self.query = query

  def get(self, name: str, default=None):
    values = self.query.get(name)
    return values[0] if values else default

  def number(self, name: str) -> Optional[float]:
    raw = self.get(name)
    if raw is None:
      return None
    try:
      return float(raw)
    except ValueError:
      return None


async def read_request(reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> Optional[Request]:
  try:
    head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=HEADER_TIMEOUT_S)
  except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
    return None
  lines = head.decode('latin-1').split('\r\n')
  try:
    method, target, _version = lines[0].split(' ', 2)
  except ValueError:
    return None
  headers = {}
  for line in lines[1:]:
    if ':' in line:
      key, value = line.split(':', 1)
      headers[key.strip().lower()] = value.strip()
  try:
    length = int(headers.get('content-length') or 0)
  except ValueError:
    length = 0
  if length:
    try:
      await asyncio.wait_for(reader.readexactly(length), timeout=READ_TIMEOUT_S)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
      return None
  path, _, query = target.partition('?')
  return Request(method.upper(), unquote(path), parse_qs(query))


# ---------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------

def query_from(request: Request) -> PieceQuery:
  """Build a `PieceQuery` from the URL.

  The same object the desktop app uses, so "list the pieces that have a recording"
  means one thing everywhere -- and becomes one WHERE clause if the storage moves
  to a database.
  """
  def flag(name: str):
    raw = request.get(name)
    if raw is None:
      return None
    return raw.lower() not in ('0', 'false', 'no', '')

  limit = request.number('limit')
  return PieceQuery(
      text=request.get('q', '') or '',
      has_audio=flag('has_audio'),
      has_map=flag('has_map'),
      tag=request.get('tag', '') or '',
      include_hidden=bool(flag('include_hidden')),
      order_by=request.get('order', PieceQuery.ORDER_AUDIO_FIRST),
      limit=int(limit) if limit and limit > 0 else 0)


def piece_detail(cfg: AnalyzerConfig, piece: library.Piece) -> Dict[str, object]:
  """Everything the practice client needs for one piece, in one response."""
  detail = piece.summary()
  detail['notes'] = list(piece.notes)

  if piece.sync is not None:
    result = piece.sync.result
    detail['map'] = {
        'method': result.method,
        'trusted': result.is_trusted,
        'seconds_per_measure': round(float(result.seconds_per_measure), 3),
        'audio_duration_s': round(float(result.audio_duration_s), 3),
        'offset_s': round(float(result.offset_s), 3),
        'quality': result.quality,
        'warnings': list(result.warnings),
        'updated_at': piece.sync.updated_at,
        'audio_sha256': (piece.sync.audio or {}).get('sha256', ''),
        'measures': [m.to_dict() for m in sorted(result.measures, key=lambda m: m.play_index)],
    }
    stale = syncmap.staleness(cfg, piece.sync,
                              audio_path=piece.audio.path if piece.audio else None,
                              musicxml_path=piece.musicxml)
    if stale:
      detail['stale'] = stale
  else:
    detail['map'] = None

  if piece.has_score:
    try:
      played = measures.expand_measures(piece.musicxml)
      detail['played'] = played.played_numbers
      detail['expansion'] = played.summary()
      if played.warnings:
        detail.setdefault('notes', []).extend(played.warnings)
    except measures.ScoreError as e:
      detail['played'] = []
      detail.setdefault('notes', []).append('could not read the score: %s' % e)

  render = scoreview.cached_render(cfg, piece.musicxml) if piece.has_score else None
  detail['score'] = ({
      'pages': [{'number': p.number, 'width': p.width, 'height': p.height,
                 'measures': [b.to_dict() for b in p.measures]} for p in render.pages],
      'warnings': list(render.warnings),
  } if render else None)
  # The practice tracks: which hands this score can be played in, and the grid their
  # bar positions follow. Answered from the score file, so it costs one XML parse.
  detail['tracks'] = synth.available(cfg, piece)
  return detail


def slice_audio(cfg: AnalyzerConfig, piece: library.Piece, start_s: float,
                end_s: float) -> Tuple[Optional[bytes], Optional[str]]:
  """`[start_s, end_s)` of the cached recording as a standalone WAV.

  Returns:
    (wav bytes, None) or (None, an error message to show the caller).
  """
  if not piece.has_audio:
    return None, 'no recording has been fetched for this piece'
  return slice_file(piece.audio.path, start_s, end_s)


def slice_track(path, start_s: float, end_s: float) -> Tuple[Optional[bytes], Optional[str]]:
  """`[start_s, end_s)` of a practice track; the same slicing, a different source."""
  return slice_file(path, start_s, end_s)


def slice_file(path, start_s: float,
               end_s: float) -> Tuple[Optional[bytes], Optional[str]]:
  """`[start_s, end_s)` of an audio file as a standalone WAV.

  Returns:
    (wav bytes, None) or (None, an error message to show the caller).
  """
  import soundfile as sf
  path = str(path)
  try:
    info = sf.info(path)
  except Exception as e:
    return None, 'could not read the audio: %s' % e

  duration = float(info.frames) / float(info.samplerate)
  start_s = max(0.0, min(float(start_s), duration))
  end_s = max(0.0, min(float(end_s), duration))
  if end_s <= start_s:
    return None, 'the requested range is empty (%.2f s to %.2f s)' % (start_s, end_s)

  first = int(round(start_s * info.samplerate))
  last = int(round(end_s * info.samplerate))
  try:
    data, rate = sf.read(path, dtype='float32', start=first, stop=last, always_2d=False)
  except Exception as e:
    return None, 'could not read that part of the audio: %s' % e
  if len(data) == 0:
    return None, 'that range contains no audio'

  buffer = io.BytesIO()
  subtype = info.subtype if info.subtype in ('PCM_16', 'PCM_24', 'FLOAT') else 'PCM_16'
  sf.write(buffer, data, rate, format='WAV', subtype=subtype)
  return buffer.getvalue(), None


def _grid_headers(info: Dict[str, object], start_s: float) -> Dict[str, str]:
  """Where the audio being returned sits in its track, for the phone's bar highlight.

  A practice track's bars are arithmetic -- one tempo throughout -- so three small
  headers are all the phone needs to follow along without a second request.
  """
  headers = {'X-Track-Start': '%.4f' % float(start_s or 0.0)}
  if info.get('seconds_per_bar'):
    headers['X-Track-Seconds-Per-Bar'] = '%.6f' % float(info['seconds_per_bar'])
    headers['X-Track-First-Bar'] = str(int(info.get('first_bar') or 1))
  return headers


class AnalyzerApp:
  """Routes. Kept free of asyncio so the same logic could be reused elsewhere."""

  def __init__(self, cfg: Optional[AnalyzerConfig] = None):
    self.cfg = cfg or AnalyzerConfig()
    self.started_at = time.time()

  def handle(self, request: Request) -> bytes:
    path = request.path

    if request.method not in ('GET', 'HEAD'):
      return error_response('only GET is supported', 405)

    if path in ('/', '/index.html'):
      return self._client()
    if path == '/health':
      pieces = library.discover(self.cfg)
      return json_response({'ok': True, 'pieces': len(pieces),
                            'uptime_s': round(time.time() - self.started_at, 1)})
    if path == '/pieces':
      query = query_from(request)
      pieces = library.discover(self.cfg, query=query)
      return json_response({'pieces': [p.summary() for p in pieces],
                            'count': len(pieces),
                            'query': {'q': query.text, 'has_audio': query.has_audio,
                                      'has_map': query.has_map, 'tag': query.tag,
                                      'limit': query.limit}})

    match = re.fullmatch(r'/piece/([^/]+)', path)
    if match:
      piece = library.find(self.cfg, match.group(1))
      if piece is None:
        return json_response({'error': 'no such piece: %s' % match.group(1)}, 404)
      return json_response(piece_detail(self.cfg, piece))

    match = re.fullmatch(r'/audio/([^/]+)', path)
    if match:
      return self._audio(request, match.group(1))

    match = re.fullmatch(r'/decks/([^/]+)', path)
    if match:
      return self._decks(match.group(1))

    match = re.fullmatch(r'/decks/([^/]+)/([^/]+)/download', path)
    if match:
      return self._deck_package(match.group(1), match.group(2))

    match = re.fullmatch(r'/score/([^/]+)/pages\.json', path)
    if match:
      return self._score_geometry(match.group(1))

    match = re.fullmatch(r'/score/([^/]+)/page-(\d+)\.png', path)
    if match:
      return self._score_page(match.group(1), int(match.group(2)))

    return error_response('no such path: %s' % path, 404)

  # ------------------------------------------------------------------ pieces

  def _client(self) -> bytes:
    page = WEB_DIR / INDEX
    if not page.is_file():
      return error_response('the client page is missing: %s' % page, 500)
    return _response(200, 'text/html; charset=utf-8', page.read_bytes())

  def _piece(self, slug: str) -> Tuple[Optional[library.Piece], Optional[bytes]]:
    piece = library.find(self.cfg, slug)
    if piece is None:
      return None, json_response({'error': 'no such piece: %s' % slug}, 404)
    return piece, None

  # ------------------------------------------------------------------ audio

  def _audio(self, request: Request, slug: str) -> bytes:
    piece, error = self._piece(slug)
    if error:
      return error
    track = (request.get('track') or 'recording').strip().lower()
    if track in synth.TRACKS:
      return self._track(request, piece, track)
    if track not in ('recording', ''):
      return json_response({'error': 'unknown track %r; try %s or recording'
                                     % (track, ', '.join(synth.TRACKS))}, 404)
    from_s = request.number('from')
    to_s = request.number('to')
    first = request.number('from_bar')
    last = request.number('to_bar')

    # No range at all: hand back the whole recording.
    if from_s is None and to_s is None and first is None and last is None:
      if not piece.has_audio:
        return error_response('no recording has been fetched for this piece', 404)
      path = pathlib.Path(piece.audio.path)
      if not path.is_file():
        return error_response('the cached recording is missing', 404)
      return _response(200, 'audio/wav', path.read_bytes())

    # A range in bars is answered from the map, which is what makes "bars 40-83"
    # mean the same thing here as it does in the desktop app.
    if from_s is None or to_s is None:
      if piece.sync is not None and first is not None and last is not None:
        span = piece.sync.result.range_for_measures(int(first), int(last))
        if span is None:
          return json_response({'error': 'the map has nothing for bars %d-%d'
                                         % (int(first), int(last))}, 404)
        from_s, to_s = span
      else:
        return error_response(
            'give either from/to in seconds, or from_bar/to_bar (bars need a map)', 400)

    wav, message = slice_audio(self.cfg, piece, from_s, to_s)
    if wav is None:
      status = 404 if 'no recording' in (message or '') else 400
      return json_response({'error': message}, status)
    return _response(200, 'audio/wav', wav,
                     extra={'Content-Disposition': 'inline; filename="%s.wav"' % slug})

  def _track(self, request: Request, piece: library.Piece, track: str) -> bytes:
    """A practice track: the score rendered whole, or one hand at a time.

    Bars are placed by the track's *own* grid, not by the recording's map -- the two
    have different tempos on purpose, and using the map here would cut the wrong
    audio. The grid goes back in the headers so the phone can highlight the bars it
    is hearing without asking again.
    """
    grid = synth.grid(self.cfg, piece)
    try:
      path = synth.build_track(self.cfg, piece, track)
    except synth.SynthError as e:
      return json_response({'error': str(e)}, 404)

    info = grid or {}
    from_s = request.number('from')
    to_s = request.number('to')
    first = request.number('from_bar')
    last = request.number('to_bar')
    if from_s is None and to_s is None and first is None and last is None:
      # no range at all: the whole track
      return _response(200, 'audio/wav', path.read_bytes(), extra={
          'Content-Disposition': 'inline; filename="%s-%s.wav"' % (piece.slug, track),
          **_grid_headers(info, 0.0)})

    if (from_s is None or to_s is None) and first is not None and last is not None:
      if not info.get('seconds_per_bar'):
        return json_response({'error': 'this piece has no bar grid yet for the %s'
                                       % synth.TRACK_LABELS[track]}, 404)
      per_bar = float(info['seconds_per_bar'])
      origin = int(info.get('first_bar') or 1)
      low, high = min(int(first), int(last)), max(int(first), int(last))
      from_s = max(0.0, (low - origin) * per_bar - PRE_ROLL_S)
      to_s = (high - origin + 1) * per_bar + POST_ROLL_S
    if from_s is None or to_s is None:
      return error_response('give either from/to in seconds, or from_bar/to_bar', 400)

    wav, message = slice_track(path, from_s, to_s)
    if wav is None:
      return json_response({'error': message}, 400)
    return _response(200, 'audio/wav', wav, extra={
        'Content-Disposition': 'inline; filename="%s-%s.wav"' % (piece.slug, track),
        **_grid_headers(info, from_s)})

  # ------------------------------------------------------------------ decks

  def _decks(self, slug: str) -> bytes:
    """The Anki decks saved for a piece, ready for AnkiDroid to download.

    Scheduling is Anki's job, so all this does is hand over a package: the phone
    opens it in AnkiDroid, which owns the intervals from then on. No Anki server is
    involved, on either side.
    """
    repo = store.open_repository(self.cfg)
    out = []
    for deck in repo.decks.list(slug) or []:
      ref = repo.decks.latest_export(slug, deck.name)
      out.append({
          'name': deck.name,
          'cards': len(deck.cards),
          'bars': [list(card.bar_range) for card in deck.cards],
          'rules': sorted({rule for card in deck.cards for rule in card.rules}),
          'updated_at': deck.updated_at,
          'bytes': ref.size if ref else 0,
          'url': ('/decks/%s/%s/download' % (slug, quote(deck.name))) if ref else '',
      })
    return json_response({'slug': slug, 'decks': out})

  def _deck_package(self, slug: str, name: str) -> bytes:
    repo = store.open_repository(self.cfg)
    ref = repo.decks.latest_export(slug, unquote(name))
    if ref is None:
      return error_response('no built deck called %r for %s' % (unquote(name), slug), 404)
    try:
      data = repo.blobs.read(ref)
    except KeyError:
      return error_response('that deck package has gone missing', 404)
    filename = '%s-%s.apkg' % (slug, re.sub(r'[^a-zA-Z0-9._-]+', '-', unquote(name)))
    return _response(200, 'application/vnd.anki.apkg', data,
                     extra={'Content-Disposition': 'attachment; filename="%s"' % filename})

  # ------------------------------------------------------------------ score

  def _render(self, slug: str):
    piece = library.find(self.cfg, slug)
    if piece is None:
      return None, json_response({'error': 'no such piece: %s' % slug}, 404)
    if not piece.has_score:
      return None, json_response({'error': 'this piece has no score on disk'}, 404)
    render = scoreview.cached_render(self.cfg, piece.musicxml)
    if render is None:
      return None, json_response(
          {'error': 'the score has not been engraved yet; open the piece in the '
                    'desktop app, or run: ./run.sh run'}, 404)
    return render, None

  def _score_geometry(self, slug: str) -> bytes:
    render, error = self._render(slug)
    if error:
      return error
    return json_response({
        'slug': slug,
        'pages': [{'number': p.number, 'width': p.width, 'height': p.height,
                   'measures': [b.to_dict() for b in p.measures]} for p in render.pages],
        'warnings': list(render.warnings)})

  def _score_page(self, slug: str, number: int) -> bytes:
    render, error = self._render(slug)
    if error:
      return error
    page = next((p for p in render.pages if p.number == number), None)
    if page is None:
      return error_response('no page %d for %s' % (number, slug), 404)
    path = pathlib.Path(page.image)
    if not path.is_file():
      return error_response('page image missing: %s' % path.name, 404)
    return _response(200, 'image/png', path.read_bytes(),
                     extra={'Cache-Control': 'public, max-age=300'})


async def handle_client(app: AnalyzerApp, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
  try:
    request = await read_request(reader, writer)
    if request is None:
      return
    if request.path.startswith('/audio/') or request.path.endswith('.png'):
      # multi-megabyte bodies: build them off the loop's critical path cheaply by
      # running the blocking file/soundfile work in a thread
      response = await asyncio.get_running_loop().run_in_executor(None, app.handle, request)
    else:
      response = app.handle(request)
    writer.write(response)
    await writer.drain()
  except Exception as e:                        # never take the server down
    try:
      writer.write(json_response({'error': 'server error: %s' % e}, 500))
    except Exception:
      pass
  finally:
    try:
      writer.close()
      await writer.wait_closed()
    except Exception:
      pass


async def build_server(cfg: Optional[AnalyzerConfig] = None, host: str = '0.0.0.0',
                       port: int = 8090) -> Tuple[asyncio.AbstractServer, int]:
  """Start listening and return (server, the port actually bound)."""
  app = AnalyzerApp(cfg)
  server = await asyncio.start_server(
      lambda r, w: handle_client(app, r, w), host, port)
  sockets = server.sockets or []
  actual = sockets[0].getsockname()[1] if sockets else port
  return server, actual


def local_addresses() -> List[str]:
  """LAN addresses this server can be reached on (best effort)."""
  import socket
  addresses: List[str] = []
  try:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect(('8.8.8.8', 80))              # no packet is sent
    addresses.append(probe.getsockname()[0])
    probe.close()
  except OSError:
    pass
  if not addresses:
    try:
      addresses = [socket.gethostbyname(socket.gethostname())]
    except OSError:
      addresses = []
  return addresses


def serve(cfg: Optional[AnalyzerConfig] = None, host: str = '0.0.0.0',
          port: int = 8090) -> None:
  """Run until interrupted, printing where to point a phone."""
  async def main() -> None:
    server, actual = await build_server(cfg, host, port)
    print('\n== analyzer server ==')
    print('  pieces : %d' % len(library.discover(cfg or AnalyzerConfig())))
    print('  this machine : http://127.0.0.1:%d' % actual)
    if host == '0.0.0.0':
      for address in local_addresses():
        print('  your phone   : http://%s:%d' % (address, actual))
      print('\n  audio is served from the local cache — the phone never talks to YouTube.')
    print('\n  Ctrl-C to stop\n')
    try:
      async with server:
        await server.serve_forever()
    except asyncio.CancelledError:
      pass

  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    print('\nstopped')
