"""The Sync tab with its ears on: "listen to me", and what it does when it is ticked.

The follower's own accuracy is measured in `test_follow`; what is worth testing here
is the wiring, which is where an addition like this either stays an addition or starts
leaking into everything else:

* the box is off by default, and with it off nothing about listening exists;
* ticking it hands audio to the follower and the follower's position to the *same*
  reveal the player drives -- and while it is ticked, the player's clock is ignored;
* stopping, the ■ button, a new piece and leaving the tab all put it back and close
  the device;
* the switch in the config (`ANALYZER_FOLLOW`) takes the whole thing out of the tab.

No microphone is opened for any of this: the source is handed over explicitly and a
recording is replayed through the identical code path. The one test that does open a
device is the one about a device that refuses.
"""

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pathlib

import pytest

pytest.importorskip('PyQt5.QtWidgets', reason='the tab needs PyQt5')

from PyQt5.QtWidgets import QApplication, QFileDialog

from analyzer import capture, follow, library, scoreview
from analyzer.config import AnalyzerConfig
from analyzer.ui.sync_tab import MIC, SyncPlayingPanel

SCORE = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
SLUG = 'chopin-waltz-in-a-minor-op-34-no-2'
needs_real = pytest.mark.skipif(
  not (SCORE.is_file() and AnalyzerConfig().sync_path(SLUG).is_file()
       and AnalyzerConfig().audio_path(SLUG).is_file()),
  reason='the Chopin score, its recording and its map are needed')


@pytest.fixture(scope='module')
def qapp():
  app = QApplication.instance() or QApplication([])
  yield app


class SilentPlayer:
  """As in `test_sync_tab`: the same surface, playing nothing."""

  def __init__(self):
    self.duration = 400.0
    self.range = None
    self.stopped = 0
    self.speed = 1.0

  def play_range(self, start, end):
    self.range = (start, end)
    return True

  def stop(self):
    self.stopped += 1

  def set_speed(self, factor):
    self.speed = float(factor)

  def current_position(self):
    return None


@pytest.fixture
def panel(qapp):
  cfg = AnalyzerConfig()
  piece = library.find(cfg, SLUG)
  render = scoreview.cached_render(cfg, piece.musicxml) if piece else None
  if piece is None or piece.sync is None or render is None:
    pytest.skip('the piece needs a saved map and a rendering')
  player = SilentPlayer()
  tab = SyncPlayingPanel(cfg, player)
  tab.set_piece(piece, render, piece.sync.result)
  tab.resize(900, 700)
  yield tab, player, piece, cfg
  tab.stop_following()        # a timer still running into the next test aborts Qt
  tab.deleteLater()


def _recording(cfg, piece, real_time=False):
  """The piece's own recording, handed over as a source: the microphone's behaviour,
  without a microphone and without waiting 309 seconds."""
  return capture.FileSource(cfg.audio_path(piece.slug), real_time=real_time)


def _listen(tab, source, reference='recording', ticks=600):
  """Start listening and turn the tab's own clock until it stops itself."""
  assert tab.start_following(source=source, reference=reference), tab.follow_status.text()
  seen = 0
  try:
    while tab._following and seen < ticks:
      tab._follow_tick()
      seen += 1
  finally:
    if tab._following:                # a failing assertion must not leave it listening
      tab.stop_following()
  return seen


# ------------------------------------------------------------------- the switch

def test_listening_is_available_unless_it_is_switched_off(qapp):
  tab = SyncPlayingPanel(AnalyzerConfig(), SilentPlayer())
  assert tab.follow_check is not None
  assert not tab.follow_check.isChecked(), 'off by default: nothing is opened unasked'
  assert tab.follow_source.currentText() == MIC, 'and the real thing is what it offers'
  assert 'not listening' in tab.follow_status.text()


def test_the_kill_switch_removes_the_controls_entirely(qapp):
  """`ANALYZER_FOLLOW=0` is not a hidden setting: there is no box to tick."""
  cfg = AnalyzerConfig(follow_enabled=False)
  tab = SyncPlayingPanel(cfg, SilentPlayer())
  assert tab.follow_check is None
  assert tab.follow_source is None
  assert tab.follow_status is None
  assert not tab.start_following(), 'and asking anyway must not open a device'


def test_the_kill_switch_reads_as_off_in_the_usual_ways():
  cfg = AnalyzerConfig(follow_enabled=False)
  assert cfg.follow_enabled is False


@pytest.mark.parametrize('value,enabled', [('0', False), ('no', False), ('off', False),
                                           ('false', False), ('FALSE', False),
                                           ('1', True), ('yes', True), ('', True)])
def test_the_environment_switches_it(monkeypatch, value, enabled):
  monkeypatch.setenv('ANALYZER_FOLLOW', value)
  assert AnalyzerConfig().follow_enabled is enabled


def test_it_is_on_when_nothing_says_otherwise(monkeypatch):
  monkeypatch.delenv('ANALYZER_FOLLOW', raising=False)
  assert AnalyzerConfig().follow_enabled is True


def test_with_it_switched_off_the_rest_of_the_tab_is_untouched(qapp):
  """The point of the switch: every other way of revealing the page still works."""
  cfg = AnalyzerConfig(follow_enabled=False)
  tab = SyncPlayingPanel(cfg, SilentPlayer())
  assert tab.play_btn is not None and tab.unit_combo is not None
  assert tab.view_combo.currentText() and tab.speed_spin.value() == 1.0
  assert tab.hint.text() and 'listen to me' not in tab.hint.text()


# ------------------------------------------------------------------- listening

@needs_real
def test_hearing_the_music_reveals_the_page(panel):
  """The feature in one test: with the audio handed over, the page is revealed from
  the position found in it -- and stops itself at the end of the passage."""
  tab, _player, piece, cfg = panel
  tab.set_range(1, 5)
  tab.view.set_page(1)
  page = tab.view.page_number

  ticks = _listen(tab, _recording(cfg, piece))

  assert ticks < 600, 'it should have got through five bars well inside the budget'
  assert not tab._following, 'reaching the end of the passage stops the listening'
  assert 'reached the end' in tab.follow_status.text(), tab.follow_status.text()
  assert tab._seconds > 5.0, 'it heard the piece: %.1f s in' % tab._seconds
  measure = tab._playing_measure()
  assert measure is not None and measure >= 5, 'the page is at bar %s' % measure
  assert tab.view.curtain(), 'the music not reached yet is covered'
  assert tab.view.page_number == page


@needs_real
def test_the_player_clock_is_ignored_while_listening(panel):
  """Two clocks, one page: while listening, the recording's position is not the one
  that matters -- the recording being played may not even be audible."""
  tab, _player, piece, cfg = panel
  tab.set_range(1, 20)
  assert tab.start_following(source=_recording(cfg, piece))
  try:
    for _ in range(40):
      tab._follow_tick()
    heard = tab._seconds
    assert heard > 0
    tab._playing = True
    tab.on_position(200.0)                    # the player, saying something else
    assert tab._seconds == heard, 'the player must not move a page being listened to'
    tab.on_finished()                         # ... nor uncover it
    assert tab._following and tab.view.curtain()
  finally:
    tab.stop_following()


@needs_real
def test_stopping_goes_back_to_how_it_was(panel):
  tab, _player, piece, cfg = panel
  tab.set_range(1, 20)
  assert tab.start_following(source=_recording(cfg, piece))
  for _ in range(10):
    tab._follow_tick()
  tab.stop_following()

  assert not tab._following
  assert tab._source is None, 'the device must be closed'
  assert not tab.follow_check.isChecked(), 'and the box must say so'
  assert 'not listening' in tab.follow_status.text()
  assert tab.play_btn.isEnabled(), 'the ordinary transport is back'
  tab.stop_following()                        # twice is not an error


@needs_real
def test_the_stop_button_stops_listening_too(panel):
  tab, player, piece, cfg = panel
  tab.set_range(1, 20)
  assert tab.start_following(source=_recording(cfg, piece))
  for _ in range(10):
    tab._follow_tick()
  tab.stop()
  assert not tab._following
  assert tab._source is None
  assert tab.view.curtain() == [], 'and the page is uncovered for browsing again'
  assert player.stopped, 'the transport still does its own job'


@needs_real
def test_leaving_the_tab_closes_the_device(panel):
  """A microphone left open while you are reading something else is not acceptable."""
  tab, _player, piece, cfg = panel
  tab.set_range(1, 20)
  assert tab.start_following(source=_recording(cfg, piece))
  for _ in range(5):
    tab._follow_tick()
  tab.unfollow()
  assert not tab._following
  assert tab._source is None


@needs_real
def test_a_new_piece_stops_the_listening(panel):
  tab, _player, piece, cfg = panel
  tab.set_range(1, 20)
  assert tab.start_following(source=_recording(cfg, piece))
  tab.set_piece(piece, tab.render, tab.result)
  assert not tab._following
  assert tab._source is None


@needs_real
def test_the_dials_still_move_the_page_while_listening(panel):
  """The dials are the answer to a follower that is a little out, so they must not be
  disabled by it. A negative nudge reveals the page earlier, exactly as it does when
  playing, so the cover begins earlier on the line -- which is what is measured here."""
  tab, _player, piece, cfg = panel
  tab.set_range(1, 20)
  assert tab.start_following(source=_recording(cfg, piece))
  try:
    for _ in range(40):
      tab._follow_tick()
    late = tab.view.curtain()[0][0] if tab.view.curtain() else None
    tab.nudge_spin.setValue(-2000)
    tab._follow_tick()
    early = tab.view.curtain()[0][0] if tab.view.curtain() else None
    assert late is not None and early is not None
    assert early < late, 'nudging the page earlier must cover from earlier on'
  finally:
    tab.stop_following()


# ------------------------------------------------------------------- refusing

def test_nothing_to_follow_says_so(qapp):
  tab = SyncPlayingPanel(AnalyzerConfig(), SilentPlayer())
  assert not tab.start_following()
  assert 'nothing to follow' in tab.follow_status.text()
  assert not tab.follow_check.isChecked() or not tab._following


@needs_real
def test_a_device_that_will_not_open_is_reported(panel):
  """The honest failure: a wrong device name is a message where the box is, not a
  page that silently never moves."""
  if not capture.available():
    pytest.skip('no capture tool on this machine')
  tab, _player, _piece, _cfg = panel
  tab.set_range(1, 20)
  source = capture.MicSource(device='no-such-device-anywhere')
  assert not tab.start_following(source=source)
  assert not tab._following and tab._source is None
  assert 'stopped at once' in tab.follow_status.text() \
      or 'no-such-device' in tab.follow_status.text(), tab.follow_status.text()


@needs_real
def test_choosing_a_file_is_what_starts_a_file_source(panel, monkeypatch, tmp_path):
  """The dialog is the choosing, so it is stubbed here: what is tested is that the
  chosen file is what gets fed in, and that cancelling leaves the microphone chosen
  rather than a source that cannot start."""
  tab, _player, _piece, _cfg = panel
  chosen = tmp_path / 'mine.wav'
  monkeypatch.setattr(QFileDialog, 'getOpenFileName',
                      lambda *a, **k: (str(chosen), ''))
  tab.follow_source.setCurrentText('a file…')
  assert tab.follow_file == chosen
  source = tab._follow_source_for('a file…', None)
  assert isinstance(source, capture.FileSource) and source.path == chosen

  monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a, **k: ('', ''))
  tab.follow_source.setCurrentText(MIC)
  tab.follow_source.setCurrentText('a file…')          # opens the dialog: cancelled
  assert tab.follow_source.currentText() == MIC, 'a cancelled choice must not stick'
  assert tab.follow_device.isVisibleTo(tab), \
      'the microphone is the chosen source again, so its device box is back'


@needs_real
def test_a_file_source_with_nothing_chosen_is_an_error(panel):
  tab, _player, _piece, _cfg = panel
  assert tab.follow_file is None
  with pytest.raises(capture.CaptureError):
    tab._follow_source_for('a file…', None)


@needs_real
def test_the_reference_for_a_practice_track_is_the_track(panel, monkeypatch):
  """The one place a rendering is the right reference: the practice track is what is
  being fed in, so it is what the audio is compared against."""
  tab, _player, piece, cfg = panel
  asked = {}

  def fake_reference(cfg_, piece_, source='auto', **kwargs):
    asked['source'] = source
    raise follow.FollowError('stop here: the choice is what is being tested')

  monkeypatch.setattr(follow, 'reference_for', fake_reference)
  tab.set_range(1, 4)
  tab.follow_source.setCurrentText('the practice track')
  assert not tab.start_following()
  assert asked['source'] == 'track'
  tab.follow_source.setCurrentText(MIC)
  assert not tab.start_following()
  assert asked['source'] == 'recording'
  assert 'stop here' in tab.follow_status.text()


@needs_real
def test_what_is_being_listened_to_cannot_be_changed_mid_look(panel):
  """The reference and the audio are matched by the choice, so it is fixed while
  listening -- stopping and starting is how it is changed."""
  tab, _player, piece, cfg = panel
  tab.set_range(1, 20)
  assert tab.start_following(source=_recording(cfg, piece))
  try:
    assert not tab.follow_source.isEnabled()
    assert not tab.follow_device.isEnabled()
  finally:
    tab.stop_following()
  assert tab.follow_source.isEnabled()
  assert tab.follow_device.isEnabled()
