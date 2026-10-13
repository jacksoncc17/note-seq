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
from analyzer.ui.sync_tab import BY_BAR, BY_BEAT, SyncPlayingPanel

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
  """Stands in for RangePlayer: records what it was asked to play, plays nothing."""

  def __init__(self):
    self.duration = 400.0
    self.range = None
    self.stopped = 0

  def play_range(self, start, end):
    self.range = (start, end)
    return True

  def stop(self):
    self.stopped += 1


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
