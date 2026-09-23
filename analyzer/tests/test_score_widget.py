"""Tests for the score widget, run head-less.

`widget.grab()` is used deliberately: it forces a real `paintEvent`, so the
painting code -- including the highlight rectangles -- actually executes instead
of just being imported.
"""

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

pytest.importorskip('PyQt5.QtWidgets', reason='the score view needs PyQt5')

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QApplication

from analyzer.scoreview import MeasureBox, ScorePage, ScoreRender
from analyzer.ui.score_widget import ScoreView


@pytest.fixture(scope='module')
def qapp():
  app = QApplication.instance() or QApplication([])
  yield app


@pytest.fixture
def render(tmp_path):
  """Two pages of real PNGs with two bars each, so pixmaps load properly."""
  from PIL import Image
  pages = []
  for number, numbers in ((1, (1, 2)), (2, (3, 4))):
    image = tmp_path / ('page-%d.png' % number)
    Image.new('RGB', (400, 200), 'white').save(image)
    boxes = [MeasureBox(number=n, x0=20.0 + 180 * i, y0=40.0,
                        x1=20.0 + 180 * i + 160, y1=140.0)
             for i, n in enumerate(numbers)]
    pages.append(ScorePage(number=number, image=str(image), width=400, height=200,
                           measures=boxes))
  return ScoreRender(slug='demo', source='demo.musicxml', pages=pages)


@pytest.fixture
def view(qapp, render):
  widget = ScoreView()
  widget.resize(400, 200)
  widget.set_render(render)
  return widget


def press(widget, point, double=False):
  kind = QEvent.MouseButtonDblClick if double else QEvent.MouseButtonPress
  event = QMouseEvent(kind, QPointF(point), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
  widget.mouseDoubleClickEvent(event) if double else widget.mousePressEvent(event)


# ---------------------------------------------------------------------------

def test_loading_a_render_shows_the_first_page(view):
  assert view.page_number == 1
  assert view.page().number == 1
  assert view.render.measure_count == 4


def test_clicking_a_bar_reports_it(view):
  seen = []
  view.rangeSelected.connect(lambda a, b: seen.append(b))
  press(view, QPoint(60, 90))          # inside bar 1
  view.set_selection(*view._selection, external=True)
  press(view, QPoint(300, 90))         # inside bar 2
  assert seen == [1, 2], seen


def test_double_click_asks_for_playback(view):
  seen = []
  view.measureActivated.connect(seen.append)
  press(view, QPoint(300, 90), double=True)
  assert seen == [2]


def test_clicking_outside_the_staves_still_picks_the_nearest_bar(view):
  seen = []
  view.rangeSelected.connect(lambda a, b: seen.append(b))
  press(view, QPoint(380, 5))          # above the system, far right
  assert seen == [2]


def test_page_navigation(view):
  pages = []
  view.pageChanged.connect(pages.append)
  view.next_page()
  assert view.page_number == 2 and pages == [2]
  view.next_page()                     # clamped at the last page
  assert view.page_number == 2
  view.previous_page()
  assert view.page_number == 1
  view.previous_page()
  assert view.page_number == 1


def test_show_measure_turns_to_the_right_page(view):
  assert view.show_measure(4) is True
  assert view.page_number == 2
  assert view.show_measure(99) is False, 'an unknown bar must not move the view'


def test_arrow_keys_step_bar_by_bar(view):
  seen = []
  view.rangeSelected.connect(lambda a, b: seen.append(b))
  view.setFocus()
  view.keyPressEvent(_key(Qt.Key_Right))
  assert seen == [2], 'from no playhead, Right goes to the second bar'
  view.set_playhead_measure(3)
  view.keyPressEvent(_key(Qt.Key_Right))
  assert seen[-1] == 4
  view.keyPressEvent(_key(Qt.Key_Left))
  assert seen[-1] == 3


def _key(code):
  from PyQt5.QtGui import QKeyEvent
  return QKeyEvent(QEvent.KeyPress, code, Qt.NoModifier)


def test_zooming_changes_the_drawn_size(view):
  before = view.minimumWidth()
  view.zoom_in()
  assert view.minimumWidth() > before
  assert view.zoom() > 1.0
  view.zoom_out()
  assert view.minimumWidth() == before


def test_ctrl_wheel_zooms(view):
  from PyQt5.QtGui import QWheelEvent
  event = QWheelEvent(QPointF(200, 100), QPointF(200, 100), QPoint(0, 0), QPoint(0, 120),
                      Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False)
  before = view.zoom()
  view.wheelEvent(event)
  assert view.zoom() > before


def test_painting_runs_with_selection_and_playhead(view):
  """Forces a real paint pass, which is where the highlight drawing lives."""
  view.set_selection(1, 2)
  view.set_playhead_measure(2)
  view._hover = 1
  pixmap = view.grab()
  assert not pixmap.isNull() and pixmap.width() == view.width()

  view.set_playhead_measure(4)         # a bar on the other page: must not crash
  assert not view.grab().isNull()


def test_painting_an_empty_view_is_harmless(qapp):
  widget = ScoreView()
  widget.resize(200, 100)
  widget.set_render(None)
  assert not widget.grab().isNull()
  assert widget.measure_at(QPoint(10, 10)) is None


def test_zoom_to_fit_uses_the_available_width(view):
  view.fit_width(800)
  assert view.zoom() == pytest.approx(2.0)     # page is 400 px wide
  assert view.minimumWidth() == pytest.approx(800, abs=2)


def test_a_pixmap_that_is_gone_is_reported_not_crashed(view, tmp_path):
  view.render.pages[0].image = str(tmp_path / 'deleted.png')
  view._pixmaps.clear()
  assert view._pixmap() is None
  assert not view.grab().isNull(), 'a missing page image must not break painting'


# ---------------------------------------------------------------------------
# selecting a range by clicking bars (the Score tab's own selection)
# ---------------------------------------------------------------------------

def test_a_second_click_extends_the_range(view):
  seen = []
  view.rangeSelected.connect(lambda a, b: seen.append((a, b)))
  view.set_selection(1, 1, external=True)          # as the window would
  press(view, QPoint(60, 90))                      # bar 1: starts again
  assert seen == [(1, 1)]
  view.set_selection(*seen[-1], external=False)    # the window echoes it back
  press(view, QPoint(300, 90))                     # bar 2 extends to it
  assert seen[-1] == (1, 2), seen
  view.set_selection(*seen[-1], external=False)
  press(view, QPoint(60, 90))                      # a third click starts over
  assert seen[-1] == (1, 1), seen


def test_shift_click_extends_from_the_current_start(view):
  seen = []
  view.rangeSelected.connect(lambda a, b: seen.append((a, b)))
  view.set_selection(2, 2, external=True)
  shift_press(view, QPoint(300, 90))               # bar 2, shift from bar 2
  assert seen[-1] == (2, 2)
  view.set_selection(1, 1, external=True)
  shift_press(view, QPoint(300, 90))
  assert seen[-1] == (1, 2)


def test_an_external_selection_closes_the_open_range(view):
  """Setting the range from the spin boxes must not be extended by the next click."""
  seen = []
  view.rangeSelected.connect(lambda a, b: seen.append((a, b)))
  view.set_selection(1, 1, external=False)         # mid-selection
  view.set_selection(2, 2, external=True)          # the user typed a range instead
  press(view, QPoint(300, 90))
  assert seen[-1] == (2, 2), 'the click should start a new selection'


def shift_press(widget, point):
  from PyQt5.QtCore import QEvent, QPointF
  from PyQt5.QtGui import QMouseEvent
  event = QMouseEvent(QEvent.MouseButtonPress, QPointF(point), Qt.LeftButton,
                      Qt.LeftButton, Qt.ShiftModifier)
  widget.mousePressEvent(event)
