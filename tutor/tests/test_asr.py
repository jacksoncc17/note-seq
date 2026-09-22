"""ASR adapter tests.

The canned payloads are real: they came from a live whisper-asr-webservice
(v1.10) at 192.168.2.174:9000. Keeping them verbatim means the parser is tested
against the shape the server actually sends, not the shape the docs imply.
"""

import asyncio
import json
import urllib.error
import wave

import pytest

from tutor.audio.asr import (OpenAiCompatAsr, StubAsr, WhisperAsrWebservice,
                             build_multipart, detect_asr_api, make_asr,
                             parse_openai_asr, parse_whisper_asr,
                             whisper_asr_url)
from tutor.audio.wav_io import read_wav, wav_to_pcm16_mono
from tutor.config import TutorConfig

# Verbatim response from whisper-asr-webservice (output=json, language=en).
LIVE_PAYLOAD = {
    "language": "en",
    "segments": [{
        "id": 1, "seek": 0, "start": 0.0, "end": 7.0,
        "text": " Measure 53, the key changes to a major, now play bar for the...",
        "tokens": [50364, 41436, 21860],
        "avg_logprob": -0.4407738729527122,
        "compression_ratio": 0.9130434782608695,
        "no_speech_prob": 0.007923473604023457,
        "words": None, "temperature": 0.0,
    }],
    "text": " Measure 53, the key changes to a major, now play bar for the...",
}


# -- parsing ----------------------------------------------------------------

def test_parses_a_real_whisper_asr_payload():
  tr = parse_whisper_asr(LIVE_PAYLOAD)
  assert tr.text.startswith('Measure 53')          # leading space stripped
  assert tr.language == 'en'
  assert tr.no_speech_prob == pytest.approx(0.0079, abs=1e-3)
  # No confidence field exists; it is derived from avg_logprob.
  assert tr.confidence == pytest.approx(0.6434, abs=1e-3)


def test_parses_a_minimal_payload():
  tr = parse_whisper_asr({'text': '  bar 40  '})
  assert tr.text == 'bar 40'
  assert tr.confidence == 1.0
  assert tr.no_speech_prob == 0.0


def test_empty_transcript_is_not_confident():
  # Verified live: sending music to the server returns empty text. Reporting
  # confidence 1.0 there would tell the tutor "I am certain I heard nothing".
  music = parse_whisper_asr({'text': '', 'language': 'en'})
  assert music.text == '' and music.confidence == 0.0
  assert parse_openai_asr({'text': '   '}).confidence == 0.0
  assert parse_openai_asr({'text': ''}).confidence == 0.0


def test_openai_parser_keeps_an_explicit_confidence():
  tr = parse_openai_asr({'text': 'measure 53', 'confidence': 0.42})
  assert tr.confidence == pytest.approx(0.42)


def test_parses_an_openai_payload():
  tr = parse_openai_asr({'text': 'measure 53', 'confidence': 0.8})
  assert tr.text == 'measure 53' and tr.confidence == 0.8


def test_averaged_logprob_across_segments():
  payload = {'text': 'a b', 'segments': [{'avg_logprob': 0.0},
                                         {'avg_logprob': -1.0}]}
  tr = parse_whisper_asr(payload)
  assert tr.confidence == pytest.approx(0.6065, abs=1e-3)   # exp(-0.5)


# -- request construction ---------------------------------------------------

def test_multipart_body_carries_fields_and_file():
  body, content_type = build_multipart({'model': 'whisper-1'}, 'audio_file',
                                       'speech.wav', b'RIFFDATA')
  assert content_type.startswith('multipart/form-data; boundary=')
  boundary = content_type.split('boundary=')[1]
  text = body.decode('utf-8', 'replace')
  assert 'name="model"' in text and 'whisper-1' in text
  assert 'name="audio_file"' in text and 'filename="speech.wav"' in text
  assert text.rstrip().endswith('--%s--' % boundary)
  assert b'RIFFDATA' in body


def test_whisper_asr_url_encodes_the_biasing_prompt():
  url = whisper_asr_url('http://host:9000', 'en', 'measure, bar, A major')
  assert url.startswith('http://host:9000/asr?')
  assert 'output=json' in url and 'language=en' in url
  assert 'initial_prompt=measure%2C+bar%2C+A+major' in url
  assert 'vad_filter=false' in url


def test_whisper_asr_url_omits_empty_language():
  url = whisper_asr_url('http://host:9000/', '', '')
  assert 'language=' not in url and 'initial_prompt=' not in url
  assert url.startswith('http://host:9000/asr?')


# -- API detection ----------------------------------------------------------

class FakeResponse:
  def __init__(self, payload):
    self.payload = payload

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False

  def read(self):
    return json.dumps(self.payload).encode()


def test_detects_whisper_asr_from_openapi(monkeypatch):
  spec = {'info': {'title': 'Whisper Asr Webservice'},
          'paths': {'/asr': {'post': {}}, '/detect-language': {'post': {}}}}
  monkeypatch.setattr('urllib.request.urlopen',
                      lambda *a, **k: FakeResponse(spec))
  assert detect_asr_api('http://192.168.2.174:9000') == 'whisper_asr'


def test_detects_openai_shape_from_openapi(monkeypatch):
  spec = {'info': {'title': 'faster-whisper-server'},
          'paths': {'/v1/audio/transcriptions': {'post': {}}}}
  monkeypatch.setattr('urllib.request.urlopen',
                      lambda *a, **k: FakeResponse(spec))
  assert detect_asr_api('http://localhost:8080/v1') == 'openai'


def test_detection_falls_back_to_openai_when_unreachable(monkeypatch):
  def boom(*a, **k):
    raise OSError('connection refused')
  monkeypatch.setattr('urllib.request.urlopen', boom)
  assert detect_asr_api('http://127.0.0.1:1') == 'openai'


# -- client selection and errors -------------------------------------------

def test_make_asr_picks_the_right_client():
  cfg = TutorConfig()
  cfg.asr = 'stub'
  assert isinstance(make_asr(cfg), StubAsr)
  cfg.asr = 'openai'
  cfg.asr_api = 'whisper_asr'
  assert isinstance(make_asr(cfg), WhisperAsrWebservice)
  cfg.asr_api = 'openai'
  assert isinstance(make_asr(cfg), OpenAiCompatAsr)


def test_404_error_message_names_the_right_api():
  client = OpenAiCompatAsr('http://192.168.2.174:9000/v1')
  err = client._error(urllib.error.HTTPError(
      'http://x', 404, 'Not Found', {}, None))
  assert 'HTTP 404' in str(err)
  assert '/audio/transcriptions' in str(err)
  assert 'whisper_asr' in str(err)          # tells you how to fix it


def test_connection_reset_is_not_reported_as_server_down():
  """Live behaviour: whisper-asr-webservice resets the connection on an
  unknown path rather than returning 404, so the advice must differ.

  It must not claim the server is down (it isn't), and it must not claim the
  endpoint is wrong either -- a firewall in between produces the same reset, as
  a live outage here showed. It offers both readings.
  """
  client = OpenAiCompatAsr('http://192.168.2.174:9000')
  err = client._error(urllib.error.URLError(
      ConnectionResetError(104, 'Connection reset by peer')))
  message = str(err)
  assert '/audio/transcriptions' in message      # names what it tried
  assert 'whisper_asr' in message                # and how to switch shape
  assert 'unreachable' not in message            # not the same as a dead host
  assert 'does not exist' in message or 'refused' in message

  # A genuine connection failure still gets the plain message.
  down = client._error(urllib.error.URLError(ConnectionRefusedError(111,
                                                                    'refused')))
  assert 'unreachable' in str(down)


def test_stub_asr_is_scripted():
  asr = StubAsr(['measure 53'])
  first = asyncio.run(asr.transcribe(b'', 16000))
  second = asyncio.run(asr.transcribe(b'', 16000))
  assert first.text == 'measure 53'
  assert second.text == '' and asr.calls == 2


# -- audio plumbing ---------------------------------------------------------

def test_wav_is_resampled_to_the_target_rate(tmp_path):
  path = tmp_path / 'tone.wav'
  rate, seconds = 22050, 0.5
  import numpy as np
  samples = (np.sin(np.linspace(0, 400, int(rate * seconds))) * 8000).astype('<i2')
  with wave.open(str(path), 'wb') as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(samples.tobytes())

  data, src_rate = read_wav(str(path))
  assert src_rate == rate and abs(data.size / rate - seconds) < 0.01

  pcm, src_rate, duration = wav_to_pcm16_mono(str(path), 16000)
  assert src_rate == 22050 and abs(duration - seconds) < 0.01
  # 16 kHz, 16-bit, mono -> 2 bytes per sample
  assert abs(len(pcm) - int(seconds * 16000) * 2) < 200


# -- hallucination guard ----------------------------------------------------
# These strings are from a live session: the first is what Whisper produced when
# the student was playing the piano rather than talking.

LIVE_NOISE = 'Bich, Bich, bich, bich, bich, bich, bich'


def test_hallucinated_noise_is_rejected():
  from tutor.audio.asr import looks_like_hallucination
  assert looks_like_hallucination(LIVE_NOISE) is True
  assert looks_like_hallucination('Thank you. Thank you. Thank you.') is True
  assert looks_like_hallucination('') is True
  assert looks_like_hallucination('   ') is True


def test_real_speech_is_kept():
  from tutor.audio.asr import looks_like_hallucination
  for text in ('bar 53', 'measure fifty three, the key changes to A major',
               'A minor', 'how am I doing?', 'drill me',
               'What happens at bar fifty three?'):
    assert looks_like_hallucination(text, 0.01, 0.6) is False, text


def test_no_speech_probability_is_respected():
  from tutor.audio.asr import looks_like_hallucination
  assert looks_like_hallucination('hello there', 0.85, 0.9) is True
  # A short blurt the model is very unsure about is not worth tutoring on.
  assert looks_like_hallucination('mm hmm', 0.1, 0.1) is True


def test_short_confident_answers_survive():
  from tutor.audio.asr import looks_like_hallucination
  assert looks_like_hallucination('A minor', 0.05, 0.5) is False
