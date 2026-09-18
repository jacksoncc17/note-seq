"""End-to-end voice-loop test with a fake microphone.

This is the test that would have caught a missing `SpeechStart` import: the
crash only appeared once someone actually spoke into the live loop, because
that was the first time the speech-start branch ran.
"""

import asyncio
import json

import numpy as np
import pytest

from tutor import cli
from tutor.audio.base import AudioFrame, Transcript

NEEDS_BANK = True

RATE = 16000


def fake_frames(count_silence=5, count_speech=60, tail_silence=60):
  frames = []
  for _ in range(count_silence):
    frames.append(AudioFrame(pcm=b'\x00' * 640, sample_rate=RATE))
  n = int(RATE * 0.02)
  t = np.arange(n) / RATE
  tone = (np.sin(2 * np.pi * 300 * t) * 0.3 * 32767).astype('<i2').tobytes()
  for _ in range(count_speech):
    frames.append(AudioFrame(pcm=tone, sample_rate=RATE))
  for _ in range(tail_silence):
    frames.append(AudioFrame(pcm=b'\x00' * 640, sample_rate=RATE))
  return frames


class FakeMic:
  """Stands in for MicrophoneSource: same interface, scripted frames."""

  last = None

  def __init__(self, device=None, rate=RATE, frame_ms=20, backend='auto'):
    self.device = device or 'fake-mic'
    self.rate = rate
    self.frame_ms = frame_ms
    FakeMic.last = self

  async def frames(self):
    for frame in fake_frames():
      yield frame


class FakeAsr:
  def __init__(self, text='measure 53'):
    self.text = text
    self.calls = 0

  async def transcribe(self, pcm, sample_rate):
    self.calls += 1
    return Transcript(text=self.text, confidence=0.9)


@pytest.fixture
def voice_env(monkeypatch, tmp_path):
  log = tmp_path / 'voice.jsonl'
  monkeypatch.setattr('tutor.audio.mic.MicrophoneSource', FakeMic)
  asr = FakeAsr()
  monkeypatch.setattr('tutor.audio.make_asr', lambda cfg: asr)
  return {'log': log, 'asr': asr}


def run_voice(voice_env, tmp_path, mode='converse', seconds=0.0):
  from tutor.config import TutorConfig
  cfg = TutorConfig()
  cfg.log_path = str(voice_env['log'])
  cfg.render_dir = str(tmp_path)
  code = asyncio.run(cli.cmd_voice(cfg, mode, None, None, seconds))
  return code


def read_log(path):
  with open(path) as fh:
    return [json.loads(line) for line in fh if line.strip()]


def test_voice_loop_completes_and_transcribes(voice_env, tmp_path):
  code = run_voice(voice_env, tmp_path)
  assert code == 0
  assert voice_env['asr'].calls >= 1          # the fake mic produced speech

  records = read_log(voice_env['log'])
  turns = [r for r in records if r['event'] == 'turn']
  # Greeting plus a turn driven by the transcript.
  assert turns, 'no turns logged'
  assert any(t['user'] == 'measure 53' for t in turns), turns
  # And the tutor answered from a tool, not from thin air.
  answered = [t for t in turns if t['user'] == 'measure 53']
  assert answered[0]['tool_calls'], answered


def test_voice_loop_handles_silence_without_a_turn(voice_env, tmp_path, monkeypatch):
  class QuietMic(FakeMic):
    async def frames(self):
      for _ in range(50):
        yield AudioFrame(pcm=b'\x00' * 640, sample_rate=RATE)

  monkeypatch.setattr('tutor.audio.mic.MicrophoneSource', QuietMic)
  code = run_voice(voice_env, tmp_path)
  assert code == 0
  turns = [r for r in read_log(voice_env['log']) if r['event'] == 'turn']
  assert all(t['user'] == '' for t in turns)   # greeting only
  assert voice_env['asr'].calls == 0


def test_voice_loop_honours_the_time_limit(voice_env, tmp_path):
  assert run_voice(voice_env, tmp_path, seconds=0.01) == 0
