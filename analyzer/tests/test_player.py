"""Tests for the range player.

These exist because the app used to *abort* on the first play attempt: the state
and error enums live in the `QAudio` namespace, not on `QAudioOutput`, and an
AttributeError raised inside a Qt slot terminates the process instead of showing
a message. Every property and method that touches the device is exercised here
with a real file loaded, which is the case the earlier tests skipped -- they only
ever reached the "no recording loaded" early return.
"""

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
import pytest

pytest.importorskip('PyQt5.QtWidgets', reason='the player needs PyQt5')

from PyQt5.QtWidgets import QApplication

from analyzer.ui.player import RangePlayer


@pytest.fixture(scope='module')
def qapp():
  app = QApplication.instance() or QApplication([])
  yield app


@pytest.fixture
def tone(tmp_path):
  """A two-second WAV, so the player has something real to decode."""
  import soundfile as sf
  path = tmp_path / 'tone.wav'
  rate = 22050
  t = np.arange(rate * 2) / rate
  sf.write(str(path), 0.3 * np.sin(2 * np.pi * 440 * t).astype('float32'), rate)
  return path


def _device_available() -> bool:
  """Is there a real audio output on this machine?"""
  from PyQt5.QtMultimedia import QAudioDeviceInfo
  try:
    return not QAudioDeviceInfo.defaultOutputDevice().isNull()
  except Exception:
    return False


@pytest.fixture
def player(qapp):
  p = RangePlayer()
  errors = []
  p.errorOccurred.connect(errors.append)
  p.errors = errors
  yield p
  p.stop()


def test_load_reads_the_audio(player, tone):
  assert player.load(tone) is True
  assert player.duration == pytest.approx(2.0, abs=0.02)
  assert player.sample_rate == 22050
  assert player.samples is not None and len(player.samples) == pytest.approx(44100, abs=100)


def test_load_rejects_a_file_that_is_not_audio(player, tmp_path):
  bad = tmp_path / 'not-audio.wav'
  bad.write_text('this is text, not samples')
  assert player.load(bad) is False
  assert player.errors and 'could not read' in player.errors[-1]


def test_is_playing_is_false_before_anything_starts(player, tone):
  """Covers the state enum: QAudioOutput has no ActiveState attribute."""
  player.load(tone)
  assert player.is_playing is False


def test_playing_without_a_recording_reports_instead_of_raising(player):
  assert player.play_range(0.0, 1.0) is False
  assert player.errors and 'no recording loaded' in player.errors[-1]


def test_a_range_too_short_to_play_is_refused(player, tone):
  player.load(tone)
  assert player.play_range(0.5, 0.52) is False
  assert player.errors and 'too short' in player.errors[-1]


def test_playing_a_real_range_never_escapes_an_exception(player, tone):
  """The whole device path: start, poll, and stop, with a real file loaded."""
  player.load(tone)
  started = player.play_range(0.1, 0.4)          # bool either way: no device, no crash
  assert isinstance(started, bool)
  if started:
    assert player.is_playing in (True, False)    # must not raise
    player._tick()                               # the position timer's body
    assert player.duration == pytest.approx(2.0, abs=0.02)
  player.stop()
  assert player.is_playing is False


def test_positions_are_reported_inside_the_requested_range(player, tone):
  player.load(tone)
  seen = []
  player.positionChanged.connect(seen.append)
  if player.play_range(0.2, 0.6):
    player._tick()
    assert seen, 'a playing range should report a position'
    assert all(0.2 <= value <= 0.6 for value in seen)


@pytest.mark.skipif(not _device_available(), reason='no audio output device here')
def test_playback_actually_starts_when_a_device_exists(player, tone):
  """Not just "does not crash": it must play, and must report no error.

  The wrapper around the device path turns an exception into a reported failure
  rather than an abort, which is right for the user -- but it means a test that
  only asserts "returns a bool" passes even when playback is completely broken.
  So when a device exists, require success.
  """
  player.load(tone)
  assert player.play_range(0.1, 0.6) is True, 'playback refused: %s' % player.errors
  assert player.errors == [], player.errors
  assert player.is_playing is True
  player._tick()                     # the position timer, with a live output
  player.stop()
  assert player.is_playing is False


def test_stop_is_safe_to_call_repeatedly(player, tone):
  player.load(tone)
  player.stop()
  player.stop()
  assert player.is_playing is False


def test_a_refused_range_leaves_the_player_usable(player, tone):
  """A failed play must not wedge the player for the next attempt."""
  player.load(tone)
  assert player.play_range(0.0, 0.01) is False
  assert player.play_range(0.5, 0.9) in (True, False)
  player.stop()
  assert player.is_playing is False


def test_an_unexpected_slot_error_is_reported_not_fatal(qapp, monkeypatch):
  """The hook that keeps a wrong attribute name from becoming a core dump."""
  from PyQt5.QtWidgets import QMessageBox

  from analyzer import app as app_module

  shown = {}
  monkeypatch.setattr(QMessageBox, 'critical',
                      staticmethod(lambda *a, **k: shown.setdefault('msg', a[2])))
  before = __import__('sys').excepthook
  try:
    app_module._install_excepthook(lambda: None)
    try:
      raise AttributeError("type object 'QAudioOutput' has no attribute 'NoError'")
    except AttributeError:
      import sys as _sys
      _sys.excepthook(*_sys.exc_info())
  finally:
    import sys as _sys
    _sys.excepthook = before
  assert 'NoError' in shown.get('msg', ''), 'the user should be told what broke'


# ---------------------------------------------------------------------------
# workers: cancellation (an uncancelled thread aborts the process on close)
# ---------------------------------------------------------------------------

def test_a_worker_reports_progress_until_it_is_cancelled(qapp):
  from analyzer.ui.workers import CancelableWorker, WorkerCancelled

  seen = []
  worker = CancelableWorker()
  report = worker.reporter(lambda pct, msg: seen.append(pct))
  report(10.0, 'starting')
  assert seen == [10.0]

  worker.cancel()
  assert worker.cancelled is True
  with pytest.raises(WorkerCancelled):
    report(20.0, 'too late')


def test_cancellation_is_not_swallowed_by_the_progress_helpers():
  """The sentinel must survive `_report`, which swallows Exception on purpose."""
  import numpy as np
  from analyzer.ui.workers import WorkerCancelled
  from analyzer import align, scoreview

  for helper in (align._report, scoreview._report):
    def angry(percent, message):
      raise WorkerCancelled()
    with pytest.raises(WorkerCancelled):
      helper(angry, 50.0, 'cancelled')
    # but an ordinary broken callback is still ignored, as documented
    def broken(percent, message):
      raise RuntimeError('the window went away')
    helper(broken, 50.0, 'ignored')


# ---------------------------------------------------------------------------
# playing at a different speed
# ---------------------------------------------------------------------------

class _Clock:
  """A wall clock the test drives by hand."""

  def __init__(self):
    self.ms = 0.0

  def start(self):
    self.ms = 0.0

  def elapsed(self):
    return self.ms


class _Output:
  """Enough of a QAudioOutput for the tick to run, with no device involved."""

  def __init__(self):
    self.state_value = None
    self.stopped = 0

  def state(self):
    if self.state_value is None:
      from PyQt5.QtMultimedia import QAudio
      return QAudio.ActiveState
    return self.state_value

  def stop(self):
    self.stopped += 1

  def deleteLater(self):
    pass


def test_speed_is_clamped_to_something_musical(player):
  player.set_speed(0.01)
  assert player.speed == pytest.approx(0.4)
  player.set_speed(9.0)
  assert player.speed == pytest.approx(2.0)


def test_the_stretch_keeps_the_pitch_and_changes_the_length(player, tone):
  """Slowing a passage down is for hearing the same notes go by more slowly -- a
  transposed version would be useless for practising the notes."""
  import librosa
  player.load(tone)
  samples = player.samples
  for speed in (0.5, 0.75, 1.25):
    player.set_speed(speed)
    stretched = player._stretched(0, len(samples))
    assert len(stretched) / len(samples) == pytest.approx(1.0 / speed, rel=0.02)
    f0 = librosa.yin(np.ascontiguousarray(stretched), fmin=200, fmax=900,
                     sr=player.sample_rate)
    f0 = f0[np.isfinite(f0)]
    assert float(np.median(f0)) == pytest.approx(440.0, rel=0.03), \
        'a 440 Hz tone must still be 440 Hz at %sx' % speed


def test_the_speed_does_not_touch_the_recording_itself(player, tone):
  player.load(tone)
  before = player.duration
  player.set_speed(0.5)
  player._stretched(0, len(player.samples))
  assert player.duration == pytest.approx(before, abs=1e-6)
  assert player.samples is not None and len(player.samples) == int(before * player.sample_rate)


def test_a_stretch_is_remembered_for_the_next_loop(player, tone):
  player.load(tone)
  player.set_speed(0.6)
  first = player._stretched(0, len(player.samples))
  again = player._stretched(0, len(player.samples))
  assert again is first, 'the same range at the same speed must not be stretched twice'


def test_a_new_recording_forgets_the_old_stretches(player, tone, tmp_path):
  import soundfile as sf
  player.load(tone)
  player.set_speed(0.6)
  player._stretched(0, len(player.samples))
  assert player._stretch_cache
  other = tmp_path / 'other.wav'
  sf.write(str(other), np.zeros(1000, dtype='float32'), 22050)
  player.load(other)
  assert player._stretch_cache == {}, 'another recording must not reuse the stretch'


def test_positions_stay_in_the_recordings_own_seconds(player, tone):
  """Everything outside the player counts in recording seconds: the map, the reveal,
  the waveform. Only the wall clock is stretched."""
  player.load(tone)
  player.set_speed(0.5)
  player._output = _Output()
  player._clock = _Clock()
  player._range = (0.5, 1.5)
  heard = []
  player.positionChanged.connect(heard.append)

  player._tick()
  assert heard[-1] == pytest.approx(0.5), 'nothing has elapsed yet'

  player._clock.ms = 500.0                      # half a second of wall time
  player._tick()
  assert heard[-1] == pytest.approx(0.75), \
      'at half speed, 0.5 s of wall time is 0.25 s of music'
  player.stop()


def test_the_position_stops_at_the_end_of_the_range_however_slow(player, tone):
  player.load(tone)
  player.set_speed(0.5)
  player._output = _Output()
  player._clock = _Clock()
  player._range = (0.0, 0.2)
  heard = []
  player.positionChanged.connect(heard.append)
  player._clock.ms = 60_000.0                   # far past the end
  player._tick()
  assert heard[-1] == pytest.approx(0.2)


def test_a_speed_change_mid_playback_keeps_the_place(player, tone, monkeypatch):
  player.load(tone)
  player._output = _Output()
  player._clock = _Clock()
  player._range = (0.2, 1.8)
  player._clock.ms = 400.0                      # 0.4 s in

  started = []
  monkeypatch.setattr(player, 'play_range',
                      lambda start, end: started.append((start, end)) or True)
  player.set_speed(0.5)
  assert player.speed == pytest.approx(0.5)
  assert started and started[0][0] == pytest.approx(0.6), \
      'it should carry on from where it had got to, at the new speed'
  assert started[0][1] == pytest.approx(1.8)
  player._output = None


def test_a_tiny_range_is_played_unstretched_rather_than_failing(player, tone):
  """Half a second of music is not a passage anybody slows down, and a stretch of it
  would be all edge effects."""
  player.load(tone)
  player.set_speed(0.5)
  tiny = player._stretched(0, 100)
  assert len(tiny) == 100


def test_a_long_stretch_says_so_before_it_starts(player, tmp_path):
  """Two seconds of silence before the first note looks like a hang, so the player
  announces the work it is about to do."""
  import soundfile as sf
  from analyzer.ui import player as player_module
  rate = 22050
  long_path = tmp_path / 'long.wav'
  sf.write(str(long_path), np.zeros(rate * 200, dtype='float32'), rate)
  player.load(long_path)
  announced = []
  player.preparing.connect(announced.append)
  player.set_speed(0.5)
  player._stretched(0, rate * 200)
  assert announced == [pytest.approx(0.5)], announced

  announced.clear()
  player._stretch_cache.clear()
  player._stretched(0, rate * 30)                 # short enough to be instant
  assert announced == [], 'a short passage must not interrupt with a message'
