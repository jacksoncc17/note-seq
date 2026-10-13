"""The Sync playing tab: does it play, follow, and cover the page it should.

Head-less, with the audio device stubbed out: what is worth testing here is the
*picture* -- whether the page really shows only what has been played -- and that is
pixel arithmetic, which needs no sound. The player is driven through its own
`positionChanged` signal, exactly as the window drives it.
"""

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pathlib

import pytest

pytest.importorskip('PyQt5.QtWidgets', reason='the tab needs PyQt5')

from PyQt5.QtCore import QPoint
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication

from analyzer import library, scoreview
from analyzer.config import AnalyzerConfig
from analyzer.ui.sync_tab import BY_BAR, BY_BEAT, SyncPlayingPanel, warped_clock

SCORE = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
SLUG = 'chopin-waltz-in-a-minor-op-34-no-2'
needs_real = pytest.mark.skipif(
    not (SCORE.is_file() and AnalyzerConfig().sync_path(SLUG).is_file()),
    reason='the Chopin score, its recording and its map are needed')

PAPER = QColor('#f6f4ef')


@pytest.fixture(scope='module')
def qapp():
  app = QApplication.instance() or QApplication([])
  yield app


class SilentPlayer:
  """Stands in for RangePlayer: records what it was asked to play, plays nothing.

  It carries the same surface the tab uses, `set_speed` included -- an AttributeError
  raised inside a Qt slot aborts the process rather than failing a test, which is
  exactly how this stub first broke.
  """

  def __init__(self):
    self.duration = 400.0
    self.range = None
    self.stopped = 0
    self.speed = 1.0
    self.speeds = []

  def play_range(self, start, end):
    self.range = (start, end)
    return True

  def stop(self):
    self.stopped += 1

  def set_speed(self, factor):
    self.speed = float(factor)
    self.speeds.append(float(factor))

  def current_position(self):
    return None


@pytest.fixture
def panel(qapp):
  cfg = AnalyzerConfig()
  piece = library.find(cfg, SLUG)
  render = scoreview.cached_render(cfg, piece.musicxml)
  if piece.sync is None or render is None:
    pytest.skip('the piece needs a saved map and a rendering')
  player = SilentPlayer()
  tab = SyncPlayingPanel(cfg, player)
  tab.set_piece(piece, render, piece.sync.result)
  tab.resize(900, 700)
  yield tab, player, piece, render, cfg
  tab.deleteLater()


def _rendered(tab, page_number=None):
  """The widget painted into an image, so the curtain can be measured."""
  if page_number is not None:
    tab.view.set_page(page_number)
  tab.view.resize(600, 800)
  image = tab.view.grab().toImage()
  return image


def _is_paper(image, x, y) -> bool:
  colour = QColor(image.pixel(x, y))
  return (abs(colour.red() - PAPER.red()) < 4 and abs(colour.green() - PAPER.green()) < 4
          and abs(colour.blue() - PAPER.blue()) < 4)


def _ink_columns(image, y0, y1) -> int:
  """How many columns in a band contain something other than paper."""
  columns = 0
  for x in range(image.width()):
    for y in range(y0, min(y1, image.height())):
      if not _is_paper(image, x, y):
        columns += 1
        break
  return columns


# ---------------------------------------------------------------------------

@needs_real
def test_the_tab_knows_what_it_needs_to_play(panel):
  tab, _player, piece, render, _cfg = panel
  assert tab.play_btn.isEnabled(), tab.status.text()
  assert tab.view.render is render
  assert 'bars' in tab.status.text()


@needs_real
def test_nothing_is_covered_before_the_music_starts(panel):
  tab, _player, _piece, _render, _cfg = panel
  tab.view.set_curtain([])
  image = _rendered(tab)
  assert _ink_columns(image, 0, image.height()) > 50, 'the page should be readable'


@needs_real
def test_playing_covers_the_page_ahead_of_the_clock(panel):
  """The feature in one assertion: with the clock in an early bar, the page is blank
  from the boundary onwards, and still readable before it."""
  tab, _player, _piece, _render, cfg = panel
  tab.view.set_page(1)
  page = next(p for p in tab.render.pages if p.number == 1)
  reveal = scoreview.page_reveal(cfg, tab.piece.musicxml, 1, page.width)
  first = min(m.measure for m in reveal.marks)
  span = next(m for m in tab.result.measures if m.measure == first)

  tab._playing = True
  tab.on_position(span.start_s + 0.05)
  assert tab.view.curtain(), 'playing must cover something'
  image = _rendered(tab)

  zoom = tab.view.zoom()
  x0, y0, _x1, _y1 = tab.view.curtain()[0]
  edge = int(x0 * zoom)
  band_top = int(reveal.systems[0][0] * zoom)
  band_bottom = int(reveal.systems[0][1] * zoom)

  before = any(not _is_paper(image, x, y)
               for x in range(max(0, edge - 200), max(1, edge - 2))
               for y in range(band_top, band_bottom))
  assert before, 'the music already played must stay readable'
  after = [x for x in range(edge + 2, image.width())
           for y in range(band_top, image.height())
           if not _is_paper(image, x, y)]
  assert not after, '%d painted pixels survive past the boundary' % len(after)


@needs_real
def test_the_cover_moves_as_the_clock_advances(panel):
  tab, _player, _piece, _render, cfg = panel
  tab.view.set_page(1)
  page = next(p for p in tab.render.pages if p.number == 1)
  reveal = scoreview.page_reveal(cfg, tab.piece.musicxml, 1, page.width)
  first = min(m.measure for m in reveal.marks)
  span = next(m for m in tab.result.measures if m.measure == first)

  tab._playing = True
  tab.on_position(span.start_s + 0.01)
  early = tab.view.curtain()[0][0]
  tab.on_position(span.start_s + (span.end_s - span.start_s) * 0.9)
  late = tab.view.curtain()[0][0]
  assert late > early, 'the boundary must move right as the bar plays: %s -> %s' % (early, late)


@needs_real
def test_whole_bar_reveal_is_the_fallback(panel):
  tab, _player, _piece, _render, _cfg = panel
  tab.view.set_page(1)
  tab._playing = True
  first = min(m.measure for m in tab.result.measures)
  span = next(m for m in tab.result.measures if m.measure == first)
  tab.unit_combo.setCurrentText(BY_BAR)
  tab.on_position(span.start_s + 0.01)
  whole_bar = tab.view.curtain()[0][0]
  tab.unit_combo.setCurrentText(BY_BEAT)
  tab.on_position(span.start_s + 0.01)
  by_beat = tab.view.curtain()[0][0]
  assert whole_bar > by_beat, 'the whole-bar reveal should give away more of the bar'
  assert 'beat 1 of' in tab.status.text()


@needs_real
def test_stopping_uncovers_the_page_again(panel):
  tab, player, _piece, _render, _cfg = panel
  tab._playing = True
  tab.on_position(20.0)
  assert tab.view.curtain()
  tab.stop()
  assert tab.view.curtain() == [], 'stopping must let the page be browsed again'
  assert player.stopped, 'and it must stop the player'


@needs_real
def test_the_page_turns_when_the_music_leaves_it(panel):
  tab, _player, _piece, _render, _cfg = panel
  later = tab.render.pages[1]
  measure = min(b.number for b in later.measures)
  span = next(m for m in tab.result.measures if m.measure == measure)
  tab._playing = True
  tab.view.set_page(1)
  tab.on_position(span.start_s + 0.02)
  assert tab.view.page_number == later.number, 'the tab should have turned the page'


@needs_real
def test_play_asks_for_the_selected_range(panel):
  tab, player, _piece, _render, _cfg = panel
  tab.set_range(40, 43)
  tab.play()
  assert player.range is not None
  span = tab.result.range_for_measures(40, 43)
  start, end = player.range
  assert start == pytest.approx(span[0] - 0.12, abs=0.01)
  assert end == pytest.approx(span[1] + 0.25, abs=0.01)


@needs_real
def test_a_piece_with_no_map_says_so_instead_of_playing(panel):
  tab, _player, piece, render, _cfg = panel
  tab.set_piece(piece, render, None)
  assert not tab.play_btn.isEnabled()
  assert 'no measure map' in tab.status.text()


@needs_real
def test_leaving_the_tab_stops_covering_the_page(panel):
  tab, _player, _piece, _render, _cfg = panel
  tab._playing = True
  tab.on_position(20.0)
  assert tab.view.curtain()
  tab.unfollow()
  assert tab.view.curtain() == []


# ---------------------------------------------------------------------------
# lining the page up with the music by hand
# ---------------------------------------------------------------------------

def test_the_page_clock_is_the_music_clock_unless_told_otherwise():
  """Untouched dials change nothing: the display is the map's own times."""
  assert warped_clock(30.0, start=20.0) == 30.0
  assert warped_clock(30.0, start=20.0, speed=1.0, nudge_ms=0.0) == 30.0


def test_page_speed_scales_the_page_against_the_music():
  """The music running ahead of the page is what a map that is too slow looks like."""
  assert warped_clock(30.0, start=20.0, speed=2.0) == 40.0
  assert warped_clock(30.0, start=20.0, speed=0.5) == 25.0


def test_the_warp_is_anchored_at_the_start_of_the_passage():
  """Otherwise turning the dial would jump you to another bar mid-passage."""
  for speed in (0.5, 1.0, 2.0):
    assert warped_clock(20.0, start=20.0, speed=speed) == 20.0, speed


def test_a_nudge_shifts_the_page_in_time():
  assert warped_clock(30.0, start=20.0, nudge_ms=-250) == pytest.approx(29.75)
  assert warped_clock(30.0, start=20.0, nudge_ms=500) == pytest.approx(30.5)


def test_the_page_cannot_run_past_the_passage_being_played():
  """At speed 4 the page reaches the last bar and waits, rather than running off."""
  assert warped_clock(100.0, start=0.0, speed=4.0, end=60.0) == 60.0
  assert warped_clock(0.0, start=10.0, nudge_ms=-5000) == 0.0, 'and never before zero'


@needs_real
def test_raising_the_page_speed_advances_the_page_sooner(panel):
  """The complaint this answers: the music gets ahead of the page."""
  tab, _player, _piece, _render, _cfg = panel
  tab.set_range(40, 43)
  tab.play()
  start = tab._range[0]
  span = next(m for m in tab.result.measures if m.measure == 40)
  clock = start + (span.start_s - start) + 1.5        # 1.5 s into the passage

  tab._playing = True
  tab.speed_spin.setValue(1.0)
  tab.on_position(clock)
  plain = tab.view.curtain()[0][0]
  plain_status = tab.status.text()

  tab.speed_spin.setValue(1.5)
  tab.on_position(clock)                              # same clock, faster page
  faster = tab.view.curtain()[0][0]
  assert faster > plain, 'a raised page speed must uncover more, not less'
  assert 'x1.50' in tab.status.text(), tab.status.text()
  assert 'x1.50' not in plain_status


@needs_real
def test_a_nudge_moves_the_page_within_the_bar(panel):
  tab, _player, _piece, _render, _cfg = panel
  tab.set_range(40, 43)
  tab.play()
  start = tab._range[0]
  span = next(m for m in tab.result.measures if m.measure == 40)
  clock = start + (span.start_s - start) + 1.0

  tab._playing = True
  tab.on_position(clock)
  plain = tab.view.curtain()[0][0]
  tab.nudge_spin.setValue(-400)                       # reveal earlier
  tab.on_position(clock)
  assert tab.view.curtain()[0][0] != plain, 'the nudge must move the boundary'
  assert 'ms' in tab.status.text(), tab.status.text()


@needs_real
def test_moving_a_dial_mid_passage_does_not_jump_the_bar(panel):
  """The anchor is the passage start, so a dial change cannot skip music."""
  tab, _player, _piece, _render, _cfg = panel
  tab.set_range(40, 43)
  tab.play()
  span = next(m for m in tab.result.measures if m.measure == 40)
  tab._playing = True
  tab.on_position(span.start_s + 0.2)
  before = tab.view.curtain()[0][0]
  tab.speed_spin.setValue(1.05)                       # a small correction
  after = tab.view.curtain()[0][0]
  assert abs(after - before) < 40, 'a 5%% correction moved the page %.0f px' % abs(after - before)


@needs_real
def test_the_tab_says_when_the_map_is_unsure_of_a_bar(panel):
  """The page follows the map, so where the map is guessing the page can slip -- and
  saying which bars those are is more use than leaving the player to guess. This score
  has eleven of them."""
  tab, _player, piece, _render, _cfg = panel
  unsure = [m.measure for m in tab.result.measures
            if m.confidence is not None and m.confidence < 0.5]
  assert unsure, 'this score is expected to have low-confidence bars'
  sure = [m.measure for m in tab.result.measures
          if m.confidence is not None and m.confidence > 0.8]
  tab._playing = True

  def status_for(measure):
    span = next(m for m in tab.result.measures if m.measure == measure)
    tab.view.set_page(tab.render.box_of(measure)[0].number)
    tab.on_position(span.start_s + 0.05)
    return tab.status.text()

  assert 'map unsure' in status_for(unsure[0]), status_for(unsure[0])
  assert 'map unsure' not in status_for(sure[0]), status_for(sure[0])


@needs_real
def test_the_play_speed_dial_drives_the_player(panel):
  """The music's own speed, next to the page's: this is the one that slows the music."""
  tab, player, _piece, _render, _cfg = panel
  assert tab.play_speed_spin.value() == pytest.approx(1.0)
  tab.play_speed_spin.setValue(0.7)
  assert player.speed == pytest.approx(0.7), 'the dial must reach the player'
  tab.reset_speed_btn.click()
  assert player.speed == pytest.approx(1.0)
  assert tab.play_speed_spin.value() == pytest.approx(1.0)


@needs_real
def test_the_status_names_a_speed_that_is_not_the_recordings_own(panel):
  tab, _player, _piece, _render, _cfg = panel
  tab.set_range(40, 43)
  tab.play()
  span = next(m for m in tab.result.measures if m.measure == 40)
  tab._playing = True
  tab.on_position(span.start_s + 0.2)
  assert 'play x' not in tab.status.text()
  tab.play_speed_spin.setValue(0.75)
  tab.on_position(span.start_s + 0.2)
  assert 'play x0.75' in tab.status.text(), tab.status.text()
