"""Audio arriving: the microphone, and the file that stands in for one.

The follower is measured in `test_follow`; what is tested here is the plumbing it
sits on -- that a wav handed over in blocks comes back as the samples that were in
it, at the pace it was played; that the capture tool's bytes come back as mono
float32; that a device which will not open is an error and not a stream of silence;
and, if there is a real microphone on this machine, that it delivers something.

The capture tool itself is not needed for most of this: a source can be pointed at
any command that writes raw 16-bit samples to stdout, which is what the fake ones
below do. Only the last test opens real hardware, and it skips when it cannot.
"""

import os
import sys

import numpy as np
import pytest

from analyzer import capture

RATE = capture.RATE
BLOCK = capture.BLOCK


def _wav(path, samples, rate=RATE):
  import soundfile as sf
  sf.write(str(path), np.asarray(samples, dtype='float32'), rate)
  return path


def _ramp(seconds=1.0, rate=RATE):
  t = np.arange(int(seconds * rate)) / float(rate)
  return (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype('float32')


# ------------------------------------------------------------------- devices

PACTL = """\
1\talsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback\tmodule-alsa-card.c\ts16le 1ch 48000Hz\tSUSPENDED
0\talsa_output.usb-Lenovo_ThinkCentre_TIO22Gen4Touch_for_USB_Audio-00.analog-stereo.monitor\tmodule-alsa-card.c\ts16le 2ch 44100Hz\tSUSPENDED
3\talsa_input.pci-0000_00_1f.3.analog-stereo\tmodule-alsa-card.c\ts16le 2ch 48000Hz\tSUSPENDED
"""


def test_the_device_list_is_read_from_what_the_sound_server_says():
  names = capture.parse_sources(PACTL)
  assert len(names) == 3
  assert names[0].endswith('mono-fallback')
  assert any(n.endswith('.monitor') for n in names), \
      'monitors are kept: they are how a recording can be listened to without a piano'


def test_an_empty_or_junk_list_is_not_a_crash():
  assert capture.parse_sources('') == []
  assert capture.parse_sources('Source\n') == []


def test_a_device_is_named_so_a_person_can_pick_it():
  assert 'what is playing' in capture.device_label(PACTL.splitlines()[1].split()[1])
  label = capture.device_label('alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback')
  assert '0c76' in label and len(label) < 40, label
  assert capture.device_label('bluez_input.00:1A:7D:DA:71:13') != ''


# ------------------------------------------------------------------- files

def test_a_file_arrives_as_the_samples_that_were_in_it(tmp_path):
  samples = _ramp(0.5)
  path = _wav(tmp_path / 'a.wav', samples)
  source = capture.FileSource(path, real_time=False).start()
  assert source.duration == pytest.approx(0.5, abs=0.01)

  blocks = []
  while True:
    block = source.read()
    if block is None:
      break
    blocks.append(block)
  got = np.concatenate(blocks)
  assert len(blocks) == pytest.approx(len(samples) / BLOCK, abs=1)
  assert len(got) == len(samples), 'every sample, no more and no fewer'
  # A wav of float data is written as 16-bit, so the file itself is quantised to about
  # 3e-5: what is being checked is that nothing is dropped, shifted or duplicated.
  assert np.allclose(got, samples, atol=1e-4), 'and the same samples'


def test_a_file_is_delivered_at_the_pace_it_was_played(tmp_path):
  """Real time, which is the microphone's behaviour: one block, then wait a block.

  The wait before the *first* block is deliberate: a source is audio that arrives,
  and a block that can be had before it has been played is a file being read, not a
  performance being heard.
  """
  import time
  path = _wav(tmp_path / 'a.wav', _ramp(2.0))
  source = capture.FileSource(path, block=4410).start()      # 0.2 s a block
  assert source.read() is None, 'the first block has not been played yet'
  time.sleep(0.25)
  assert source.read() is not None, 'but it is there once its time has come'
  assert source.read() is None, 'and never two at once'


def test_a_stopped_file_source_gives_nothing_more(tmp_path):
  source = capture.FileSource(_wav(tmp_path / 'a.wav', _ramp(0.2)),
                              real_time=False).start()
  assert source.read() is not None
  source.stop()
  assert source.read() is None
  assert source.finished


def test_a_file_that_is_not_there_is_an_error(tmp_path):
  with pytest.raises(capture.CaptureError):
    capture.FileSource(tmp_path / 'nothing.wav').start()


def test_a_file_that_is_not_audio_is_an_error(tmp_path):
  path = tmp_path / 'not-a.wav'
  path.write_text('not audio at all')
  with pytest.raises(capture.CaptureError):
    capture.FileSource(path).start()


def test_a_block_is_about_a_frame_of_the_follower():
  """The two constants have to agree: one block per hop, or the follower buffers."""
  from analyzer import follow
  assert BLOCK == follow.HOP
  assert RATE == follow.SAMPLE_RATE
  assert 0.02 < BLOCK / float(RATE) < 0.06, 'a block is 20-60 ms of audio'


# ------------------------------------------------------------------- the tool

def _fake_tool(blocks: int = 4, rate: int = RATE, block: int = BLOCK, value: int = 16384,
               pause: float = 0.1, fail: bool = False, message: str = '') -> list:
  """A command that behaves like a recording tool, without any hardware.

  It writes raw 16-bit samples to stdout in blocks, slowly enough to be a stream
  rather than a burst, and can be told to refuse the way a busy device does.
  """
  body = (
    "import sys, time\n"
    "if %r:\n"
    "    sys.stderr.write(%r)\n"
    "    sys.exit(1)\n"
    "block = bytes([%d, %d]) * %d\n"
    "for _ in range(%d):\n"
    "    sys.stdout.buffer.write(block)\n"
    "    sys.stdout.buffer.flush()\n"
    "    time.sleep(%r)\n"
    % (fail, message, value & 0xff, (value >> 8) & 0xff, block, blocks, pause))
  return [sys.executable, '-c', body]


def test_the_capture_bytes_come_back_as_audio():
  """The conversion the whole path depends on: 16-bit little-endian -> float."""
  source = capture.MicSource(command=_fake_tool(blocks=3, value=16384)).start()
  try:
    blocks = []
    for _ in range(20):
      block = source.read(timeout=0.2)
      if block is None:
        if len(blocks) >= 3:
          break
        continue
      blocks.append(block)
      if len(blocks) == 3:
        break
  finally:
    source.stop()
  assert len(blocks) == 3, 'got %d of 3 blocks' % len(blocks)
  assert all(len(b) == BLOCK for b in blocks)
  assert all(b.dtype == np.float32 for b in blocks)
  assert np.allclose(blocks[0], 0.5, atol=1e-3), '16384 of 32768 is a half'


def test_stopping_closes_the_tool_and_the_queue():
  source = capture.MicSource(command=_fake_tool(blocks=6)).start()
  assert source.read(timeout=0.5) is not None
  source.stop()
  assert source._proc is None, 'the recording tool must not be left running'
  assert not (source._thread and source._thread.is_alive())
  assert source.read() is None


def test_a_device_that_refuses_is_an_error_not_a_silent_stream():
  """The failure that matters: a busy or wrong device must not look like a room
  with no piano in it -- the page would simply never move, and nothing would say why."""
  with pytest.raises(capture.CaptureError) as caught:
    capture.MicSource(command=_fake_tool(fail=True,
                                         message='Failure: Device unavailable')).start()
  assert 'Device unavailable' in str(caught.value)


def test_a_tool_that_does_not_exist_is_an_error():
  with pytest.raises((capture.CaptureError, OSError)):
    capture.MicSource(command=['no-such-recorder-anywhere']).start()


# ------------------------------------------------------------------- for real

needs_device = pytest.mark.skipif(
  not capture.available() or not capture.microphones(),
  reason='no capture tool or no capture device on this machine')


@needs_device
def test_a_real_microphone_delivers_audio():
  """Opportunistic: opens the default device and asks for one block of 46 ms.

  A silent room is a perfectly good answer -- this is about the tool being started,
  read, and stopped without leaving a process behind, which is the part a fake
  command cannot prove.
  """
  source = capture.MicSource()
  try:
    source.start()
  except capture.CaptureError as e:
    pytest.skip('the capture device would not open: %s' % e)
  try:
    block = None
    for _ in range(20):
      block = source.read(timeout=0.2)
      if block is not None:
        break
    if block is None:
      pytest.skip('the device opened but sent nothing (the sandbox may have no audio)')
    assert len(block) == BLOCK
    assert np.all(np.isfinite(block))
    assert np.abs(block).max() <= 1.0
  finally:
    source.stop()
  assert source._proc is None


# ------------------------------------------------------------------- keeping it

def test_a_session_can_be_written_down_while_it_is_listened_to(tmp_path):
  """`RecordSource` is the way a rehearsal becomes a file that can be replayed."""
  path = tmp_path / 'session.wav'
  source = capture.RecordSource(path, command=_fake_tool(blocks=4, value=8192)).start()
  got = []
  try:
    for _ in range(20):
      block = source.read(timeout=0.2)
      if block is not None:
        got.append(block)
      if len(got) == 4:
        break
  finally:
    source.stop()
  assert len(got) == 4
  assert path.is_file()
  import soundfile as sf
  written, rate = sf.read(str(path), dtype='float32')
  assert rate == RATE
  assert len(written) == 4 * BLOCK, 'everything listened to is on disk (%d)' % len(written)
  assert np.allclose(written[:BLOCK], got[0], atol=1e-3)


def test_a_monitor_is_marked_as_what_is_playing():
  """The distinction that matters when picking a device: hearing the room, or hearing
  what the machine is playing -- the second is how this is tried without a piano."""
  label = capture.device_label('alsa_output.usb-0b0e_Jabra-00.analog-stereo.monitor')
  assert '(what is playing)' in label
  assert 'alsa_output' not in label and not label.endswith('-00')
  assert capture.device_label('alsa_input.usb-0c76_USB-00.mono-fallback') != ''
