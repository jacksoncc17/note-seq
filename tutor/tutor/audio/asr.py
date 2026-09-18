"""ASR adapters, for two real API shapes.

1. `OpenAiCompatAsr` -- `/v1/audio/transcriptions` (faster-whisper-server,
   speaches, vLLM audio, whisper.cpp's OpenAI-compatible route).
2. `WhisperAsrWebservice` -- the native API of ahmetoner/whisper-asr-webservice
   (v1.10): `POST /asr?output=json&language=..&initial_prompt=..` with a
   multipart field named `audio_file`. Note there is NO OpenAI-compatible route
   on that server, so the two are genuinely different clients.

`detect_asr_api` picks one by reading the server's OpenAPI document, which both
shapes expose. `parse_*` and `build_multipart` are pure functions so they can be
unit-tested without a server; the canned payloads in the tests came from a live
whisper-asr-webservice.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

from .base import Transcript
from .wav_io import pcm16_to_wav_bytes

# A token repeated three or more times in a row ("bich, bich, bich, bich").
REPEAT_RE = re.compile(r'\b(\w+)(?:[\s,.]+\1\b){2,}', re.I)


# --------------------------------------------------------------------------
# pure helpers (unit-tested)
# --------------------------------------------------------------------------

def build_multipart(fields: Dict[str, str], file_field: str, filename: str,
                    file_bytes: bytes,
                    content_type: str = 'audio/wav') -> Tuple[bytes, str]:
  """Hand-rolled multipart/form-data body -> (body, Content-Type header)."""
  boundary = '----tutorasrboundary7d1a'
  chunks: List[bytes] = []
  for name, value in fields.items():
    chunks.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                   % (boundary, name, value)).encode('utf-8'))
  chunks.append(('--%s\r\nContent-Disposition: form-data; name="%s"; '
                 'filename="%s"\r\nContent-Type: %s\r\n\r\n'
                 % (boundary, file_field, filename, content_type)).encode('utf-8'))
  chunks.append(file_bytes)
  chunks.append(('\r\n--%s--\r\n' % boundary).encode('utf-8'))
  return b''.join(chunks), 'multipart/form-data; boundary=%s' % boundary


def whisper_asr_url(base_url: str, language: str, initial_prompt: str,
                    output: str = 'json', vad_filter: bool = False,
                    word_timestamps: bool = False) -> str:
  """Native whisper-asr-webservice URL with its query-parameter API."""
  params = {'output': output, 'task': 'transcribe',
            'encode': 'true', 'vad_filter': 'true' if vad_filter else 'false',
            'word_timestamps': 'true' if word_timestamps else 'false'}
  if language:
    params['language'] = language
  if initial_prompt:
    params['initial_prompt'] = initial_prompt
  return '%s/asr?%s' % (base_url.rstrip('/'), urllib.parse.urlencode(params))


def _logprob_to_confidence(avg_logprob: Optional[float]) -> float:
  if avg_logprob is None:
    return 1.0
  return max(0.0, min(1.0, math.exp(float(avg_logprob))))


def looks_like_hallucination(text: str, no_speech_prob: float = 0.0,
                             confidence: float = 1.0) -> bool:
  """Reject recogniser output that is almost certainly not speech.

  A room with a piano in it produces a lot of non-speech audio, and Whisper
  answers it with confident nonsense. Observed in a live session, from someone
  playing rather than talking:

      'Bich, Bich, bich, bich, bich, bich, bich'

  Three cheap signals catch that class of failure: the server's own no-speech
  probability, a token loop, and a very unsure short blurt. Being wrong here is
  cheap (the student repeats themselves); tutoring them about a sentence they
  never said is not.
  """
  stripped = (text or '').strip()
  if not stripped:
    return True
  words = stripped.split()
  if no_speech_prob >= 0.6:
    return True
  if REPEAT_RE.search(stripped):
    return True
  # Phrase loops too ("Thank you. Thank you. Thank you."): a run of words with
  # almost no vocabulary is Whisper padding, not a student talking. The six-word
  # floor keeps short real answers ("A minor", "bar 53") out of this rule.
  tokens = [w.strip('.,!?;:').lower() for w in words]
  tokens = [w for w in tokens if w]
  if len(tokens) >= 6 and len(set(tokens)) / len(tokens) <= 0.4:
    return True
  if confidence < 0.2 and len(words) <= 3:
    return True
  return False


def parse_whisper_asr(payload: dict) -> Transcript:
  """Parse whisper-asr-webservice output=json.

  Real payload shape (v1.10, verified against a live server):
    {"language": "en",
     "segments": [{"text": " Measure 53, ...", "avg_logprob": -0.44,
                   "no_speech_prob": 0.0079, ...}],
     "text": " Measure 53, ..."}
  There is no `confidence` field, so one is derived from avg_logprob, and
  `no_speech_prob` is surfaced because a room with a piano in it produces plenty
  of audio that is not speech.

  An empty transcript reports confidence 0.0 rather than 1.0: "I recognised
  nothing" must not look like "I am certain". (Playing music at the server
  returns exactly that: empty text, no hallucinated words.)
  """
  text = (payload.get('text') or '').strip()
  segments = payload.get('segments') or []
  logprobs = [s.get('avg_logprob') for s in segments
              if isinstance(s, dict) and s.get('avg_logprob') is not None]
  no_speech = [s.get('no_speech_prob') for s in segments
               if isinstance(s, dict) and s.get('no_speech_prob') is not None]
  if not text:
    confidence = 0.0
  else:
    confidence = _logprob_to_confidence(
        sum(logprobs) / len(logprobs) if logprobs else None)
  np_ = (sum(no_speech) / len(no_speech)) if no_speech else 0.0
  return Transcript(text=text, confidence=confidence,
                    no_speech_prob=float(np_),
                    language=payload.get('language') or '')


def parse_openai_asr(payload: dict) -> Transcript:
  text = (payload.get('text') or '').strip()
  given = payload.get('confidence')
  confidence = (float(given) if given is not None
                else (1.0 if text else 0.0))
  if not text:
    confidence = 0.0
  return Transcript(text=text, confidence=confidence,
                    no_speech_prob=float(payload.get('no_speech_prob') or 0.0),
                    language=payload.get('language') or '')


def detect_asr_api(base_url: str, timeout_s: float = 5.0) -> str:
  """'whisper_asr' | 'openai' -- decided by the server's OpenAPI document."""
  url = '%s/openapi.json' % base_url.rstrip('/')
  try:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
      spec = json.loads(resp.read().decode('utf-8', 'replace'))
    paths = spec.get('paths') or {}
    if '/asr' in paths:
      return 'whisper_asr'
    if any(p.endswith('/audio/transcriptions') for p in paths):
      return 'openai'
    title = str((spec.get('info') or {}).get('title', '')).lower()
    if 'whisper asr webservice' in title:
      return 'whisper_asr'
  except Exception:  # noqa: BLE001 - fall through to the default
    pass
  return 'openai'


# --------------------------------------------------------------------------
# adapters
# --------------------------------------------------------------------------

class StubAsr:
  """Scripted recogniser: pops utterances off a list, then falls back to ''. """

  def __init__(self, script: Optional[List[str]] = None, default: str = ''):
    self.script = list(script or [])
    self.default = default
    self.calls = 0
    self.api = 'stub'

  def push_utterance(self, text: str) -> None:
    self.script.append(text)

  async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
    self.calls += 1
    await asyncio.sleep(0)
    if self.script:
      return Transcript(text=self.script.pop(0), confidence=0.99)
    return Transcript(text=self.default, confidence=1.0)


class _HttpAsrBase:
  """Shared plumbing: POST a multipart body in a thread, wrap errors."""

  api = 'unknown'
  endpoint = '/'

  def __init__(self, base_url: str, timeout_s: float = 60.0,
               api_key: str = 'not-needed'):
    self.base_url = base_url.rstrip('/')
    self.timeout_s = timeout_s
    self.api_key = api_key

  def _post(self, url: str, body: bytes, content_type: str) -> dict:
    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': content_type,
                 'Authorization': 'Bearer %s' % self.api_key})
    with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
      raw = resp.read().decode('utf-8', 'replace')
    return json.loads(raw)

  def _error(self, exc: Exception) -> RuntimeError:
    """Turn transport failures into advice.

    Tested against a live server: posting the OpenAI shape to
    whisper-asr-webservice does NOT return 404 -- the server closes the
    connection, so a naive "unreachable, start it" message sends you chasing a
    server that is running fine.
    """
    if isinstance(exc, urllib.error.HTTPError):
      hint = ''
      if exc.code == 404:
        hint = (' That endpoint does not exist on this server. If it is '
                'whisper-asr-webservice, set TUTOR_ASR_API=whisper_asr (its '
                'native API is POST /asr, not /v1/audio/transcriptions).')
      elif exc.code == 422:
        hint = ' The server rejected the request fields; check the field names.'
      return RuntimeError('ASR server returned HTTP %s for %s on %s.%s'
                          % (exc.code, self.endpoint, self.base_url, hint))
    reason = getattr(exc, 'reason', None) or exc
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError,
                           BrokenPipeError)) or 'reset' in str(reason).lower():
      return RuntimeError(
          'ASR server at %s accepted the connection but closed it on %s -- the '
          'server is running, this endpoint probably is not. Try '
          'TUTOR_ASR_API=auto (it reads the server\'s OpenAPI document) or set '
          'it to whisper_asr for whisper-asr-webservice (%s).'
          % (self.base_url, self.endpoint, reason))
    return RuntimeError(
        'ASR server unreachable at %s (%s). Start it, or use TUTOR_ASR=stub.'
        % (self.base_url, reason))

  async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
    try:
      payload = await asyncio.to_thread(self._transcribe_sync, pcm, sample_rate)
    except Exception as exc:  # noqa: BLE001 - re-raised with guidance
      raise self._error(exc) from exc
    return self._parse(payload)

  def _transcribe_sync(self, pcm: bytes, sample_rate: int) -> dict:
    raise NotImplementedError

  def _parse(self, payload: dict) -> Transcript:
    raise NotImplementedError


class OpenAiCompatAsr(_HttpAsrBase):
  """POST {base}/audio/transcriptions (OpenAI shape)."""

  api = 'openai'
  endpoint = '/audio/transcriptions'

  def __init__(self, base_url: str, model: str = 'whisper-1',
               api_key: str = 'not-needed', hotwords: str = '',
               timeout_s: float = 60.0, language: str = 'en'):
    super().__init__(base_url, timeout_s, api_key)
    self.model = model
    self.hotwords = hotwords
    self.language = language

  def _transcribe_sync(self, pcm: bytes, sample_rate: int) -> dict:
    fields = {'model': self.model, 'language': self.language,
              'response_format': 'json'}
    if self.hotwords:
      fields['prompt'] = self.hotwords
    body, content_type = build_multipart(
        fields, 'file', 'speech.wav', pcm16_to_wav_bytes(pcm, sample_rate))
    return self._post(self.base_url + '/audio/transcriptions', body, content_type)

  def _parse(self, payload: dict) -> Transcript:
    return parse_openai_asr(payload)


class WhisperAsrWebservice(_HttpAsrBase):
  """POST {base}/asr -- native API of ahmetoner/whisper-asr-webservice.

  `initial_prompt` is the biasing hook on this server: it is where the musical
  vocabulary goes, and it visibly changes output (a live probe turned "measure
  fifty three" into "Measure 53").
  """

  api = 'whisper_asr'
  endpoint = '/asr'

  def __init__(self, base_url: str, hotwords: str = '', language: str = 'en',
               timeout_s: float = 60.0, api_key: str = 'not-needed',
               vad_filter: bool = False, word_timestamps: bool = False):
    super().__init__(base_url, timeout_s, api_key)
    self.hotwords = hotwords
    self.language = language
    self.vad_filter = vad_filter
    self.word_timestamps = word_timestamps

  def _transcribe_sync(self, pcm: bytes, sample_rate: int) -> dict:
    url = whisper_asr_url(self.base_url, self.language, self.hotwords,
                          vad_filter=self.vad_filter,
                          word_timestamps=self.word_timestamps)
    body, content_type = build_multipart(
        {}, 'audio_file', 'speech.wav', pcm16_to_wav_bytes(pcm, sample_rate))
    return self._post(url, body, content_type)

  def _parse(self, payload: dict) -> Transcript:
    return parse_whisper_asr(payload)


def make_asr(cfg) -> object:
  """Build the configured ASR client (shared by the CLI and the conductor)."""
  if cfg.asr == 'stub':
    return StubAsr()
  api = cfg.asr_api
  if api in ('auto', '', None):
    api = detect_asr_api(cfg.asr_base_url, timeout_s=min(cfg.llm_timeout_s, 5.0))
  if api == 'whisper_asr':
    return WhisperAsrWebservice(cfg.asr_base_url, hotwords=cfg.asr_hotwords,
                                language=cfg.asr_language,
                                timeout_s=cfg.asr_timeout_s,
                                vad_filter=cfg.asr_vad_filter)
  return OpenAiCompatAsr(cfg.asr_base_url, model=cfg.asr_model,
                         hotwords=cfg.asr_hotwords,
                         timeout_s=cfg.asr_timeout_s,
                         language=cfg.asr_language)
