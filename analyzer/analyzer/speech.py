"""Speaking the question on a card, with a self-hosted TTS server.

A card can carry a spoken question as well as a written one, which is what makes it
usable away from the screen -- at the piano, phone in hand, the question read aloud
and the passage played back when you turn it over.

The request shape is the OpenAI-compatible `/audio/speech` one, the same the tutor
uses against Kokoro, and it is copied deliberately rather than guessed at:

    POST {base_url}/audio/speech
    {"model": "kokoro", "voice": "af_heart", "input": "...",
     "response_format": "wav", "stream": false}

`stream: false` matters. Kokoro-FastAPI streams by default, and a streamed WAV header
declares 2147483647 frames, which decoders then report as a two-day file.

Nothing here raises: a card must still be built when the speech server is down, so
failures come back as `None` plus a reason the caller can show.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Optional, Tuple


DEFAULT_BASE_URL = os.environ.get('ANALYZER_TTS_BASE_URL', 'http://192.168.2.174:8880/v1')
DEFAULT_VOICE = os.environ.get('ANALYZER_TTS_VOICE', 'af_heart')
DEFAULT_MODEL = os.environ.get('ANALYZER_TTS_MODEL', 'kokoro')
TIMEOUT_S = float(os.environ.get('ANALYZER_TTS_TIMEOUT', '60'))


def speak(text: str, base_url: Optional[str] = None, voice: Optional[str] = None,
          model: Optional[str] = None, timeout: Optional[float] = None
          ) -> Tuple[Optional[bytes], str]:
  """Ask the server to say `text`.

  Args:
    text: what to say. Surrounding whitespace is ignored; empty text is refused.
    base_url: the API root, e.g. `http://host:8880/v1`.
    voice: the server's voice name.
    model: the server's model name.
    timeout: seconds to wait.

  Returns:
    (audio bytes, '') on success, or (None, a reason) on any failure -- including a
    server that is simply not running, which is a normal state, not an error.
  """
  text = (text or '').strip()
  if not text:
    return None, 'nothing to say'
  root = (base_url or DEFAULT_BASE_URL).rstrip('/')
  payload = {
      'model': model or DEFAULT_MODEL,
      'voice': voice or DEFAULT_VOICE,
      'input': text,
      'response_format': 'wav',
      'stream': False,          # see the module docstring: streamed WAV headers lie
  }
  request = urllib.request.Request(
      root + '/audio/speech', data=json.dumps(payload).encode('utf-8'),
      headers={'Content-Type': 'application/json'}, method='POST')
  try:
    with urllib.request.urlopen(request, timeout=timeout or TIMEOUT_S) as response:
      data = response.read()
  except urllib.error.HTTPError as e:
    detail = ''
    try:
      detail = e.read().decode('utf-8', 'replace')[:160]
    except Exception:
      pass
    return None, 'the speech server refused the request (HTTP %s) %s' % (e.code, detail)
  except urllib.error.URLError as e:
    return None, 'no speech server at %s (%s)' % (root, getattr(e, 'reason', e))
  except (TimeoutError, OSError) as e:
    return None, 'the speech server did not answer in time (%s)' % e
  if not data:
    return None, 'the speech server returned no audio'
  return data, ''


def to_mp3(audio: bytes, bitrate: str = '64k') -> bytes:
  """Re-encode to MP3 when ffmpeg is available, else hand the bytes back.

  Cards with several clips get large quickly, and a spoken question is a couple of
  seconds of speech; MP3 cuts that by roughly ten.
  """
  ffmpeg = shutil.which('ffmpeg')
  if not ffmpeg or not audio:
    return audio
  try:
    result = subprocess.run(
        [ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'wav', '-i', 'pipe:0',
         '-codec:a', 'libmp3lame', '-b:a', bitrate, '-ac', '1', '-f', 'mp3', 'pipe:1'],
        input=audio, capture_output=True, timeout=120)
  except (OSError, subprocess.SubprocessError):
    return audio
  return result.stdout if result.returncode == 0 and result.stdout else audio


def suffix_for(audio: bytes) -> str:
  """`.mp3` or `.wav`, judged by what the bytes actually are."""
  if audio[:3] == b'ID3' or audio[:2] == b'\xff\xfb':
    return '.mp3'
  return '.wav'


def speak_to_file(text: str, path, **kwargs) -> Tuple[Optional[pathlib.Path], str]:
  """Say `text` and write the clip to `path`. Returns (path, '') or (None, reason)."""
  audio, reason = speak(text, **kwargs)
  if audio is None:
    return None, reason
  audio = to_mp3(audio)
  target = pathlib.Path(path).with_suffix(suffix_for(audio))
  target.parent.mkdir(parents=True, exist_ok=True)
  target.write_bytes(audio)
  return target, ''
