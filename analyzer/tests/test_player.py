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
