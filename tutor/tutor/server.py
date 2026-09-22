"""HTTP transport: drive the tutor from a phone browser.

Design note -- this is deliberately *not* WebSocket. A phone can push-to-talk and
POST one audio blob per turn, which is simpler than streaming, works on mobile
Safari as well as Chrome, needs no extra dependency (stdlib asyncio only), and
maps cleanly onto the conductor's event model: one request is one turn.

The conductor is driven directly (`await conductor.handle(event)`) rather than
through its queue, so a request can return the reply it produced. Everything
else -- modes, the fact gate, grading, grounding, the session log -- is the same
code the desktop tutor runs.

Endpoints
    GET  /            the web client (a single self-contained page)
    GET  /health      liveness plus what is configured
    GET  /state       piece, mode, pending question, progress
    POST /start       greet, and return the opening utterance
    POST /turn        body: raw audio (any container) or {"text": "..."}

Mic access needs a secure context: http://localhost, or https. A phone hitting a
LAN IP over plain http will have getUserMedia refused by the browser -- see the
README for the two ways around it (`adb reverse`, or a self-signed cert).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .audio.asr import looks_like_hallucination
from .audio.wav_io import pcm16, pcm16_to_wav_bytes, read_wav
from .config import TutorConfig
from .events import SpeechEnd, SpeechStart

MAX_BODY = 25 * 1024 * 1024          # a 2-minute Opus clip is about 1 MB
HEADER_TIMEOUT_S = 15.0
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

CONTENT_TYPES = {'.html': 'text/html; charset=utf-8',
                 '.js': 'application/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8',
                 '.json': 'application/json',
                 '.png': 'image/png', '.svg': 'image/svg+xml'}


@dataclass
class Request:
  method: str
  path: str
  headers: Dict[str, str] = field(default_factory=dict)
  body: bytes = b''

  def header(self, name: str, default: str = '') -> str:
    return self.headers.get(name.lower(), default)


# --------------------------------------------------------------------------
# a small HTTP/1.1 server
# --------------------------------------------------------------------------

def _response(status: int, content_type: str, body: bytes,
              extra: Optional[Dict[str, str]] = None) -> bytes:
  reason = {200: 'OK', 400: 'Bad Request', 404: 'Not Found',
            405: 'Method Not Allowed', 413: 'Payload Too Large',
            500: 'Internal Server Error'}.get(status, 'OK')
  headers = {'Content-Type': content_type,
             'Content-Length': str(len(body)),
             'Cache-Control': 'no-store',
             'Connection': 'close'}
  headers.update(extra or {})
  head = 'HTTP/1.1 %d %s\r\n' % (status, reason)
  head += ''.join('%s: %s\r\n' % (k, v) for k, v in headers.items())
  return head.encode('utf-8') + b'\r\n' + body


def json_response(payload: dict, status: int = 200) -> bytes:
  return _response(status, 'application/json',
                   json.dumps(payload, ensure_ascii=False).encode('utf-8'))


async def read_request(reader: asyncio.StreamReader) -> Optional[Request]:
  try:
    head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),
                                 timeout=HEADER_TIMEOUT_S)
  except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
    return None
  except asyncio.TimeoutError:
    return None
  lines = head.decode('latin-1').split('\r\n')
  try:
    method, target, _version = lines[0].split(' ', 2)
  except ValueError:
    return None
  headers: Dict[str, str] = {}
  for line in lines[1:]:
    if ':' in line:
      key, value = line.split(':', 1)
      headers[key.strip().lower()] = value.strip()
  try:
    length = int(headers.get('content-length') or 0)
  except ValueError:
    length = 0
  if length > MAX_BODY:
    raise ValueError('body too large: %d bytes' % length)
  body = b''
  if length:
    body = await asyncio.wait_for(reader.readexactly(length), timeout=60.0)
  path, _, query = target.partition('?')
  request = Request(method=method.upper(), path=path, headers=headers, body=body)
  request.headers['x-query'] = query
  return request


# --------------------------------------------------------------------------
# turn handling
# --------------------------------------------------------------------------

class TutorHttpApp:
  """Routes requests onto a live conductor."""

  def __init__(self, cfg: TutorConfig, conductor, facts, session, tools,
               asr=None, tts=None, ca_path: Optional[str] = None):
    self.cfg = cfg
    self.conductor = conductor
    self.facts = facts
    self.session = session
    self.tools = tools
    self.asr = asr
    self.tts = tts
    self.ca_path = ca_path            # served at /ca.pem for the tablet
    self.lock = asyncio.Lock()       # one turn at a time, whoever is asking
    self.turns = 0

  # -- helpers ------------------------------------------------------------

  def _piece_info(self) -> dict:
    facts = self.facts
    return {'slug': self.cfg.piece,
            'title': facts.global_facts.get('title') or '',
            'bars': facts.global_facts.get('num_measures'),
            'key': facts.global_facts.get('key_inferred')}

  def state(self) -> dict:
    pending = self.tools.pending
    return {'piece': self._piece_info(),
            'mode': self.conductor.state.mode.value,
            'floor': self.conductor.state.floor.value,
            'pending_question': pending.public() if pending else None,
            'status': self.session.status(),
            'weak_spots': self.session.weak_spot_prompts(3),
            'turns': self.turns}

  async def _collect(self, since_turns: int, since_audio: int) -> dict:
    """What the conductor said during this turn, plus its audio."""
    new_turns = self.conductor.transcript[since_turns:]
    replies = [t['reply'] for t in new_turns if t.get('reply')]
    tools: List[dict] = []
    grounded = True
    ungrounded: List = []
    think_ms = 0.0
    for turn in new_turns:
      for call in turn.get('tools') or []:
        tools.append(call)
      if not turn.get('grounded', True):
        grounded = False
        ungrounded.extend(turn.get('ungrounded') or [])
      think_ms += float(turn.get('think_ms') or 0)

    audio_b64 = None
    paths = self._audio_since(since_audio)
    if paths:
      wav = _join_wavs(paths)
      if wav:
        audio_b64 = base64.b64encode(wav).decode('ascii')
    warnings = []
    if getattr(self.conductor, 'tts_error', None):
      warnings.append('speech unavailable: %s' % self.conductor.tts_error)
    return {'replies': replies, 'reply': ' '.join(replies),
            'tools': tools, 'grounded': grounded, 'ungrounded': ungrounded,
            'think_ms': round(think_ms, 1), 'audio_wav_b64': audio_b64,
            'warnings': warnings}

  def _audio_since(self, count: int) -> List[str]:
    """Files this turn actually synthesized.

    Slicing the adapter's record of successful writes, rather than scanning the
    output directory for the current counter range: filenames restart each
    process, so a stale `tts_0001.wav` from a previous session would otherwise be
    served as the reply to a fresh turn.
    """
    written = getattr(self.tts, 'written', None)
    if written is None:
      return []
    fresh = [path for path in written[count:] if os.path.exists(path)]
    return fresh

  # -- endpoints ----------------------------------------------------------

  async def handle(self, request: Request) -> bytes:
    path = request.path
    if path in ('/', '/index.html'):
      return self._static('index.html')
    if path == '/health':
      return json_response({'ok': True, 'piece': self._piece_info(),
                            'mode': self.conductor.state.mode.value,
                            'asr': type(self.asr).__name__ if self.asr else None,
                            'tts': type(self.tts).__name__ if self.tts else None,
                            'turns': self.turns})
    if path == '/state':
      return json_response(self.state())
    if path == '/start':
      if request.method != 'POST':
        return json_response({'error': 'use POST'}, 405)
      return json_response(await self.start())
    if path == '/turn':
      if request.method != 'POST':
        return json_response({'error': 'use POST'}, 405)
      return json_response(await self.turn(request))
    if path == '/ca.pem':
      # Handing the CA to the tablet is the one step that cannot happen over
      # https (it is not trusted yet), so this route is served in the clear.
      if self.ca_path and os.path.isfile(self.ca_path):
        with open(self.ca_path, 'rb') as fh:
          return _response(200, 'application/x-pem-file', fh.read())
      return json_response(
          {'error': 'no CA here. Create one with: '
                    'python -m tutor.cli --make-cert'}, 404)
    if path.startswith('/static/'):
      return self._static(os.path.basename(path))
    return json_response({'error': 'no such endpoint: %s' % path}, 404)

  def _static(self, name: str) -> bytes:
    path = os.path.join(WEB_DIR, name)
    if not os.path.isfile(path):
      return json_response({'error': 'missing client file %s' % name}, 404)
    with open(path, 'rb') as fh:
      body = fh.read()
    extension = os.path.splitext(name)[1].lower()
    return _response(200, CONTENT_TYPES.get(extension, 'application/octet-stream'),
                     body)

  async def start(self) -> dict:
    async with self.lock:
      if self.conductor.greeted:
        # The phone reloaded or woke up: re-state where we are rather than
        # saying nothing, which looks like a broken app.
        pending = self.tools.pending
        last = self.conductor.transcript[-1]['reply'] if self.conductor.transcript else ''
        reply = pending.prompt if pending else last
        return {'resumed': True, 'replies': [reply] if reply else [],
                'reply': reply, 'transcript': '', 'grounded': True,
                'ungrounded': [], 'tools': [], 'audio_wav_b64': None,
                'warnings': [], 'think_ms': 0.0,
                'mode': self.conductor.state.mode.value,
                'pending_question': pending.public() if pending else None,
                'state': self.state()}
      since_turns = len(self.conductor.transcript)
      since_audio = len(getattr(self.tts, 'written', None) or [])
      await self.conductor.greet()
      self.turns += 1
      payload = await self._collect(since_turns, since_audio)
      payload.update({'resumed': False, 'transcript': '',
                      'mode': self.conductor.state.mode.value,
                      'pending_question': (self.tools.pending.public()
                                           if self.tools.pending else None),
                      'state': self.state()})
      return payload

  async def turn(self, request: Request) -> dict:
    started = time.monotonic()
    content_type = request.header('content-type', '').lower()
    transcript = ''
    confidence = 1.0
    no_speech = 0.0
    skipped = None
    asr_ms = 0.0

    if 'application/json' in content_type:
      try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
      except ValueError:
        return {'error': 'body is not valid JSON'}
      transcript = str(payload.get('text', '')).strip()
      if payload.get('mode'):
        from .modes import Mode
        try:
          self.conductor.state.mode = Mode(str(payload['mode']).lower())
        except ValueError:
          pass
      if not transcript:
        skipped = 'empty text'
    else:
      if self.asr is None:
        return {'error': 'no ASR configured on the server'}
      if not request.body:
        return {'error': 'empty audio body'}
      filename = _filename_for(content_type)
      asr_started = time.monotonic()
      try:
        result = await self.asr.transcribe_file(request.body, filename)
      except Exception as exc:  # noqa: BLE001 - report to the client
        return {'error': 'transcription failed: %s' % exc,
                'transcript': '', 'reply': ''}
      asr_ms = (time.monotonic() - asr_started) * 1000
      transcript, confidence = result.text.strip(), result.confidence
      no_speech = result.no_speech_prob
      if looks_like_hallucination(transcript, no_speech, confidence):
        skipped = 'not speech' if transcript else 'silence'
        transcript = ''

    if skipped:
      return {'transcript': transcript, 'confidence': round(confidence, 3),
              'no_speech': round(no_speech, 3), 'skipped': skipped,
              'reply': '', 'replies': [], 'audio_wav_b64': None,
              'mode': self.conductor.state.mode.value, 'state': self.state(),
              'timings': {'asr_ms': round(asr_ms, 1),
                          'total_ms': round((time.monotonic() - started) * 1000, 1)}}

    async with self.lock:
      since_turns = len(self.conductor.transcript)
      since_audio = len(getattr(self.tts, 'written', None) or [])
      await self.conductor.handle(SpeechStart())
      await self.conductor.handle(SpeechEnd(text=transcript,
                                            confidence=confidence))
      self.turns += 1
      payload = await self._collect(since_turns, since_audio)

    payload.update({'transcript': transcript, 'confidence': round(confidence, 3),
                    'no_speech': round(no_speech, 3), 'skipped': None,
                    'mode': self.conductor.state.mode.value,
                    'pending_question': (self.tools.pending.public()
                                         if self.tools.pending else None),
                    'state': self.state(),
                    'timings': {'asr_ms': round(asr_ms, 1),
                                'total_ms': round((time.monotonic() - started) * 1000, 1)}})
    return payload


def _filename_for(content_type: str) -> str:
  if 'webm' in content_type:
    return 'speech.webm'
  if 'mp4' in content_type or 'm4a' in content_type or 'aac' in content_type:
    return 'speech.m4a'
  if 'ogg' in content_type:
    return 'speech.ogg'
  if 'mpeg' in content_type or 'mp3' in content_type:
    return 'speech.mp3'
  return 'speech.wav'


def _join_wavs(paths: List[str]) -> Optional[bytes]:
  """Concatenate synthesized sentences into one WAV for the client to play."""
  chunks: List = []
  rate = 0
  for path in paths:
    try:
      samples, sample_rate = read_wav(path, quiet=True)
    except Exception:  # noqa: BLE001 - a broken file should not kill the turn
      continue
    rate = sample_rate or rate
    chunks.append(samples)
  if not chunks or not rate:
    return None
  import numpy as np
  merged = np.concatenate(chunks)
  return pcm16_to_wav_bytes(pcm16(merged), rate)


# --------------------------------------------------------------------------
# serving
# --------------------------------------------------------------------------

async def start(cfg: TutorConfig, app: TutorHttpApp, host: str = '127.0.0.1',
                port: int = 8080, certfile: Optional[str] = None,
                keyfile: Optional[str] = None) -> Tuple[asyncio.AbstractServer, int]:
  context = None
  if certfile and keyfile:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)

  async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
      while True:
        request = await read_request(reader)
        if request is None:
          break
        try:
          response = await app.handle(request)
        except Exception as exc:  # noqa: BLE001 - never kill the server
          response = json_response({'error': 'server error: %s' % exc}, 500)
        writer.write(response)
        await writer.drain()
        if request.header('connection', '').lower() == 'keep-alive':
          continue
        break
    except (ConnectionResetError, BrokenPipeError, asyncio.TimeoutError):
      pass
    except ValueError as exc:
      try:
        writer.write(json_response({'error': str(exc)}, 413))
        await writer.drain()
      except Exception:  # noqa: BLE001
        pass
    finally:
      try:
        writer.close()
      except Exception:  # noqa: BLE001
        pass

  server = await asyncio.start_server(client, host, port, ssl=context)
  sockets = server.sockets or []
  actual = sockets[0].getsockname()[1] if sockets else port
  return server, actual
