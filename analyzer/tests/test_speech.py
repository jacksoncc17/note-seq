"""Tests for speaking a card's question.

The speech server is not required: a card must still be built when it is down, so
most of these tests are about failing cleanly. One live test is opt-in.
"""

import io
import json
import os
import urllib.error

import pytest

from analyzer import speech


class FakeResponse:
  def __init__(self, payload: bytes):
    self.payload = payload

  def read(self) -> bytes:
    return self.payload

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False


def test_the_request_is_the_shape_the_server_expects(monkeypatch):
  """Copied from the tutor's working call, including stream=False: a streamed WAV
  header declares two days of audio."""
  seen = {}

  def fake_urlopen(request, timeout=None):
    seen['url'] = request.full_url
    seen['body'] = json.loads(request.data.decode('utf-8'))
    return FakeResponse(b'RIFF....WAVE')

  monkeypatch.setattr(speech.urllib.request, 'urlopen', fake_urlopen)
  audio, reason = speech.speak('What is the chord in bar 53?', base_url='http://host:8880/v1',
                               voice='af_heart')
  assert audio == b'RIFF....WAVE' and reason == ''
  assert seen['url'] == 'http://host:8880/v1/audio/speech'
  assert seen['body'] == {'model': 'kokoro', 'voice': 'af_heart',
                          'input': 'What is the chord in bar 53?',
                          'response_format': 'wav', 'stream': False}


def test_a_server_that_is_not_running_is_a_reason_not_an_exception(monkeypatch):
  def refuse(request, timeout=None):
    raise urllib.error.URLError('Connection refused')

  monkeypatch.setattr(speech.urllib.request, 'urlopen', refuse)
  audio, reason = speech.speak('anything', base_url='http://192.168.2.174:8880/v1')
  assert audio is None
  assert 'no speech server' in reason and '192.168.2.174' in reason


def test_an_http_error_is_reported_with_its_status(monkeypatch):
  def broken(request, timeout=None):
    raise urllib.error.HTTPError('http://host/v1/audio/speech', 422, 'no', {}, io.BytesIO(b'bad voice'))

  monkeypatch.setattr(speech.urllib.request, 'urlopen', broken)
  audio, reason = speech.speak('x', base_url='http://host:8880/v1')
  assert audio is None and '422' in reason


def test_empty_text_is_not_sent(monkeypatch):
  def explode(*a, **k):
    raise AssertionError('nothing should be sent for empty text')

  monkeypatch.setattr(speech.urllib.request, 'urlopen', explode)
  assert speech.speak('   ')[0] is None
  assert speech.speak('')[1] == 'nothing to say'


def test_mp3_conversion_falls_back_when_ffmpeg_is_missing(monkeypatch):
  monkeypatch.setattr(speech.shutil, 'which', lambda name: None)
  assert speech.to_mp3(b'RIFF....WAVE') == b'RIFF....WAVE'


def test_an_empty_response_is_refused(monkeypatch):
  monkeypatch.setattr(speech.urllib.request, 'urlopen', lambda *a, **k: FakeResponse(b''))
  audio, reason = speech.speak('x', base_url='http://host:8880/v1')
  assert audio is None and 'no audio' in reason


def test_the_suffix_follows_the_bytes_not_the_request():
  assert speech.suffix_for(b'ID3\x03') == '.mp3'
  assert speech.suffix_for(b'\xff\xfb\x90') == '.mp3'
  assert speech.suffix_for(b'RIFF....WAVE') == '.wav'


def test_speak_to_file_writes_the_clip_and_reports_failure(tmp_path, monkeypatch):
  monkeypatch.setattr(speech, 'speak', lambda *a, **k: (b'RIFF....WAVE', ''))
  monkeypatch.setattr(speech, 'to_mp3', lambda audio, bitrate='64k': audio)
  path, reason = speech.speak_to_file('hello', tmp_path / 'q')
  assert path is not None and path.suffix == '.wav' and path.read_bytes() == b'RIFF....WAVE'
  assert reason == ''

  monkeypatch.setattr(speech, 'speak', lambda *a, **k: (None, 'no speech server'))
  path, reason = speech.speak_to_file('hello', tmp_path / 'q2')
  assert path is None and reason == 'no speech server'


@pytest.mark.skipif(os.environ.get('ANALYZER_NETWORK_TESTS') != '1',
                    reason='set ANALYZER_NETWORK_TESTS=1 to use the real speech server')
def test_the_configured_server_actually_speaks():
  audio, reason = speech.speak('Bars forty to forty three.', timeout=60)
  assert audio, reason
  assert audio[:4] == b'RIFF'
