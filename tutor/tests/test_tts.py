"""TTS adapter tests.

The request body is the part worth pinning down: two settings are easy to get
wrong and both were, against a live Kokoro server --

  * `stream` defaults to true there, so audio arrives chunked with a placeholder
    frame count in the WAV header (2,147,483,647 frames);
  * non-English voices need `lang_code` ('z' for Mandarin), and without it a
    Chinese voice mangles the text.

The payload is also serialised by hand, so a refactor once shipped a dict where
bytes were expected.
"""

import asyncio
import json

import pytest

from tutor.audio.tts import OpenAiCompatTts, sentences


class FakeResponse:
  def __init__(self, payload=b'RIFFxxxxWAVE'):
    self.payload = payload

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False

  def read(self):
    return self.payload


def capture(monkeypatch, store, payload=b'RIFFxxxxWAVE'):
  def fake_urlopen(req, timeout=None):
    store['url'] = req.full_url
    store['headers'] = dict(req.headers)
    # A dict here means the body was never serialised to JSON.
    assert isinstance(req.data, (bytes, bytearray)), (
        'request body must be bytes, got %r' % type(req.data))
    store['body'] = json.loads(req.data.decode('utf-8'))
    return FakeResponse(payload)
  monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)


def test_request_asks_for_a_complete_wav(monkeypatch, tmp_path):
  store = {}
  capture(monkeypatch, store)
  tts = OpenAiCompatTts('http://host:8880/v1', out_dir=str(tmp_path))
  asyncio.run(tts.speak('Measure fifty three.', asyncio.Event()))
  assert store['url'].endswith('/audio/speech')
  assert store['body']['stream'] is False
  assert store['body']['response_format'] == 'wav'
  assert store['body']['input'] == 'Measure fifty three.'
  assert 'lang_code' not in store['body']      # English needs no code


def test_lang_code_is_sent_for_chinese(monkeypatch, tmp_path):
  store = {}
  capture(monkeypatch, store)
  tts = OpenAiCompatTts('http://host:8880/v1', voice='zf_xiaoxiao',
                        lang_code='z', out_dir=str(tmp_path))
  asyncio.run(tts.speak('第五十三小节。', asyncio.Event()))
  assert store['body']['lang_code'] == 'z'
  assert store['body']['voice'] == 'zf_xiaoxiao'
  assert store['body']['input'] == '第五十三小节。'   # not mangled in transit


def test_audio_is_written_to_disk(monkeypatch, tmp_path):
  store = {}
  capture(monkeypatch, store, payload=b'RIFFsomeaudiodata')
  tts = OpenAiCompatTts('http://host:8880/v1', out_dir=str(tmp_path))
  asyncio.run(tts.speak('One sentence.', asyncio.Event()))
  written = list(tmp_path.glob('tts_*.wav'))
  assert len(written) == 1
  assert written[0].read_bytes() == b'RIFFsomeaudiodata'


def test_server_errors_are_explained(monkeypatch, tmp_path):
  import urllib.error

  def boom(req, timeout=None):
    raise urllib.error.URLError(ConnectionRefusedError(111, 'refused'))
  monkeypatch.setattr('urllib.request.urlopen', boom)
  tts = OpenAiCompatTts('http://127.0.0.1:1/v1', out_dir=str(tmp_path))
  with pytest.raises(RuntimeError) as err:
    asyncio.run(tts.speak('hello', asyncio.Event()))
  assert 'unreachable' in str(err.value)


def test_sentence_split_for_streaming():
  assert sentences('One. Two! Three?') == ['One.', 'Two!', 'Three?']
  assert sentences('  ') == []
