"""Microphone-side tests: segmentation policy and capture command selection.

Capture itself cannot be unit-tested, but the decisions that make live testing
painful -- where an utterance starts and ends, what happens to a cough, what
happens when someone talks for a minute -- can all be tested on synthetic audio.
"""

import numpy as np
import pytest

from tutor.audio import mic
from tutor.audio.base import AudioFrame
from tutor.audio.mic import capture_command, pick_source
from tutor.audio.segmenter import UtteranceSegmenter
from tutor.audio.vad_energy import dbfs_of, rms

RATE = 16000


def silence(ms=20):
  return AudioFrame(pcm=b'\x00' * (int(RATE * ms / 1000) * 2),
                    sample_rate=RATE)


def tone(amplitude=0.3, freq=300, ms=20):
  n = int(RATE * ms / 1000)
  t = np.arange(n) / RATE
  sig = np.clip(amplitude * np.sin(2 * np.pi * freq * t), -1, 1)
  return AudioFrame(pcm=(sig * 32767).astype('<i2').tobytes(), sample_rate=RATE)


def segmenter(**kw):
  opts = dict(energy_threshold=0.012, hangover_ms=100, frame_ms=20,
              pre_roll_ms=100, min_utterance_ms=100, max_utterance_s=20.0)
  opts.update(kw)
  return UtteranceSegmenter(**opts)


def collect(seg, frames):
  events = []
  for f in frames:
    events.extend(seg.push(f))
  return events


def utterances(events):
  return [payload for kind, payload in events if kind == 'utterance']


def kinds(events):
  return [kind for kind, _ in events]


# -- levels -----------------------------------------------------------------

def test_rms_and_dbfs_track_amplitude():
  assert rms(silence().pcm) == 0.0
  loud, quiet = rms(tone(0.5).pcm), rms(tone(0.05).pcm)
  assert loud > quiet > 0
  assert dbfs_of(tone(0.5).pcm) > dbfs_of(tone(0.05).pcm)
  assert dbfs_of(b'') == -120.0


# -- segmentation -----------------------------------------------------------

def test_silence_alone_produces_nothing():
  seg = segmenter()
  events = collect(seg, [silence() for _ in range(50)])
  assert events == []


def test_speech_burst_produces_exactly_one_utterance():
  seg = segmenter()
  frames = ([silence() for _ in range(10)]
            + [tone() for _ in range(25)]
            + [silence() for _ in range(10)])
  events = collect(seg, frames)
  assert kinds(events).count('speech_start') == 1
  assert kinds(events).count('speech_end') == 1
  utts = utterances(events)
  assert len(utts) == 1
  # 500ms of speech, plus the 100ms pre-roll, plus hangover frames.
  assert utts[0].duration_s >= 0.5
  assert utts[0].peak > 0.1


def test_pre_roll_keeps_the_attack_of_the_first_word():
  short_roll = segmenter(pre_roll_ms=0)
  long_roll = segmenter(pre_roll_ms=200)
  frames = ([silence() for _ in range(10)]
            + [tone() for _ in range(20)]
            + [silence() for _ in range(10)])
  without = utterances(collect(short_roll, frames))[0]
  with_roll = utterances(collect(long_roll, frames))[0]
  assert with_roll.duration_s > without.duration_s


def test_a_click_is_not_an_utterance():
  seg = segmenter(min_utterance_ms=400)
  frames = ([silence() for _ in range(10)]
            + [tone() for _ in range(3)]      # 60ms: a click, not a word
            + [silence() for _ in range(10)])
  events = collect(seg, frames)
  assert kinds(events).count('speech_start') == 1
  assert utterances(events) == []


def test_a_breath_does_not_end_the_phrase():
  # 200ms hangover swallows a 100ms pause mid-phrase.
  seg = segmenter(hangover_ms=200, min_utterance_ms=100)
  frames = ([silence() for _ in range(10)]
            + [tone() for _ in range(10)]
            + [silence() for _ in range(5)]   # a breath
            + [tone() for _ in range(10)]
            + [silence() for _ in range(15)])
  events = collect(seg, frames)
  assert kinds(events).count('speech_start') == 1
  assert len(utterances(events)) == 1


def test_sentence_pauses_do_not_split_a_turn():
  """Measured on a real clip: 300ms hangover turned one spoken turn into six
  fragments and corrupted "53" and "bar 69". At the 900ms default, a 700ms
  pause between sentences stays inside a single utterance."""
  frames = ([silence() for _ in range(10)]
            + [tone() for _ in range(25)]
            + [silence() for _ in range(35)]     # 700ms between sentences
            + [tone() for _ in range(25)]
            + [silence() for _ in range(50)])
  live = segmenter(hangover_ms=900)
  assert len(utterances(collect(live, frames))) == 1

  # The old default chopped the same audio in two.
  choppy = segmenter(hangover_ms=300)
  assert len(utterances(collect(choppy, frames))) == 2


def test_endless_talking_is_capped():
  seg = segmenter(max_utterance_s=0.4)        # 20 frames
  events = collect(seg, [tone() for _ in range(60)])
  utts = utterances(events)
  assert len(utts) >= 2                       # emitted in chunks, not lost
  assert all(u.duration_s <= 0.45 for u in utts)


def test_flush_emits_what_is_buffered():
  seg = segmenter()
  for _ in range(10):
    seg.push(silence())
  for _ in range(20):
    seg.push(tone())
  assert seg.active is True
  pending = seg.flush()
  assert pending is not None and pending.duration_s >= 0.4
  assert seg.active is False


# -- capture plumbing -------------------------------------------------------

# Verbatim `pactl list short sources` output (fields separated by tabs).
PACtl_OUTPUT = (
    "2\talsa_output.pci-0000_09_00.4.analog-stereo.monitor\tmodule-alsa-card.c"
    "\ts16le 2ch 44100Hz\tSUSPENDED\n"
    "3\talsa_input.pci-0000_09_00.4.analog-stereo\tmodule-alsa-card.c"
    "\ts16le 2ch 44100Hz\tSUSPENDED\n"
    "65\talsa_input.usb-0b0e_Jabra_BIZ_2400_II_501AA5923400-00.mono-fallback"
    "\tmodule-alsa-card.c\ts16le 1ch 16000Hz\tSUSPENDED\n"
)


def _fake_run(output):
  class Result:
    stdout = output
    returncode = 0
  return lambda *a, **k: Result()


def test_parses_real_pactl_output(monkeypatch):
  """Regression: requiring six fields made this return nothing at all."""
  monkeypatch.setattr(mic.shutil, 'which', lambda name: '/usr/bin/pactl')
  monkeypatch.setattr(mic.subprocess, 'run', _fake_run(PACtl_OUTPUT))
  sources = mic.list_input_sources()
  assert [s['name'] for s in sources] == [
      'alsa_input.pci-0000_09_00.4.analog-stereo',
      'alsa_input.usb-0b0e_Jabra_BIZ_2400_II_501AA5923400-00.mono-fallback',
  ]                                      # monitors excluded
  usb = sources[-1]
  assert usb['channels'] == 1 and usb['rate'] == 16000 and usb['index'] == 65
  assert sources[0]['channels'] == 2 and sources[0]['rate'] == 44100


def test_parses_space_separated_pactl_output(monkeypatch):
  monkeypatch.setattr(mic.shutil, 'which', lambda name: '/usr/bin/pactl')
  monkeypatch.setattr(mic.subprocess, 'run', _fake_run(
      '65 alsa_input.usb-mic module-alsa-card.c s16le 1ch 16000Hz SUSPENDED\n'))
  sources = mic.list_input_sources()
  assert len(sources) == 1 and sources[0]['rate'] == 16000


def test_pick_source_prefers_a_usb_microphone(monkeypatch):
  monkeypatch.setattr(mic, 'list_input_sources', lambda: [
      {'name': 'alsa_input.pci-0000_09_00.4.analog-stereo'},
      {'name': 'alsa_input.usb-0b0e_Jabra_BIZ_2400_II.mono-fallback'},
  ])
  assert pick_source(None).endswith('mono-fallback')
  assert 'analog' in pick_source('analog')


def test_pick_source_returns_the_hint_when_nothing_is_listed(monkeypatch):
  monkeypatch.setattr(mic, 'list_input_sources', lambda: [])
  assert pick_source('my-mic') == 'my-mic'
  assert pick_source(None) is None


def test_capture_command_uses_parecord_raw_mono(monkeypatch):
  monkeypatch.setattr(mic.shutil, 'which', lambda name: '/usr/bin/' + name)
  cmd = capture_command('alsa_input.usb-x', 16000)
  assert cmd[0] == 'parecord'
  assert '--device=alsa_input.usb-x' in cmd
  assert '--format=s16le' in cmd and '--rate=16000' in cmd
  assert '--channels=1' in cmd and '--raw' in cmd


def test_capture_command_falls_back_to_arecord(monkeypatch):
  monkeypatch.setattr(mic.shutil, 'which',
                      lambda name: '/usr/bin/arecord' if name == 'arecord' else None)
  cmd = capture_command(None, 16000)
  assert cmd[0] == 'arecord' and 'pulse' in cmd and '-' in cmd


def test_capture_command_reports_missing_backends(monkeypatch):
  monkeypatch.setattr(mic.shutil, 'which', lambda name: None)
  with pytest.raises(RuntimeError) as err:
    capture_command(None, 16000)
  assert 'no audio capture backend' in str(err.value).lower()


def test_frame_size_matches_the_configured_rate():
  source = mic.MicrophoneSource.__new__(mic.MicrophoneSource)
  source.rate, source.frame_ms = 16000, 20
  assert int(source.rate * source.frame_ms / 1000) * 2 == 640


def test_reset_clears_partial_speech():
  """Half-duplex uses this to start clean after the tutor stops talking."""
  seg = segmenter()
  for _ in range(10):
    seg.push(silence())
  for _ in range(10):
    seg.push(tone())
  assert seg.active is True
  seg.reset()
  assert seg.active is False
  assert seg.flush() is None
