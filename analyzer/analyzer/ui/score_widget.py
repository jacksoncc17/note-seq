"""The engraved score on screen, with clickable measures.

Deliberately dumb about engraving: it is handed PNG pages plus the rectangle of
every measure (from `scoreview.py`) and draws them. Clicking translates a point on
the page into a measure number and emits it; the window decides what that means
(select the bar, play it). Keeping the arithmetic out of the painting is what
makes the click behaviour testable without a display.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QSizePolicy, QWidget

from ..scoreview import ScorePage, ScoreRender


_ZOOM_STEPS = (0.5, 0.65, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)


class ScoreView(QWidget):
  """One engraved page, scaled, with the selected bars highlighted.

  Can also show a single **system** -- one line of music -- instead of the whole page,
  which is what the Sync playing tab uses: a line at a time is easier to read, and it
  removes the awkward question of what to do with the lines above and below the one
  being played. `set_line(top, bottom)` takes page-pixel coordinates, so the caller
  keeps working in the coordinates the render and the reveal already speak.

  Signals:
    measureClicked: (measure number) on a single click.
    measureActivated: (measure number) on a double click -- the window plays it.
    pageChanged: (page number) after the shown page changes.
  """

  measureActivated = pyqtSignal(int)
  pageChanged = pyqtSignal(int)
  glyphClicked = pyqtSignal(str)          # a note was tapped in mask mode
  rangeSelected = pyqtSignal(int, int)    # first and last bar of a range

  def __init__(self, parent=None):
    super().__init__(parent)
    self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    self.setFocusPolicy(Qt.StrongFocus)
    self.setMouseTracking(True)
    self.setMinimumSize(200, 200)

    self.render: Optional[ScoreRender] = None
    self._page_number = 1
    self._pixmaps: Dict[int, QPixmap] = {}
    self._zoom = 1.0
    self._fit_pending = True
    self._selection = (None, None)
    self._playhead: Optional[int] = None
    self._hover: Optional[int] = None
    self._mask_mode = False
    self._masked: set = set()
    self._range_open = False            # a click has started a range, not finished it
    self._hover_glyph: Optional[str] = None
    self._curtain: List[Tuple[float, float, float, float]] = []
    self._line: Optional[Tuple[float, float]] = None      # one system, in page pixels

  # ------------------------------------------------------------------ content

  def set_render(self, render: Optional[ScoreRender]) -> None:
    self.render = render
    self._pixmaps.clear()
    self._selection = (None, None)
    self._playhead = None
    self._hover = None
    self._page_number = 1
    self._fit_pending = True
    if render and render.pages:
      self._apply_page_size()
      self.pageChanged.emit(self._page_number)
    self.update()

  def page(self) -> Optional[ScorePage]:
    if not self.render:
      return None
    return next((p for p in self.render.pages if p.number == self._page_number), None)

  @property
  def page_number(self) -> int:
    return self._page_number

  def set_page(self, number: int) -> None:
    if not self.render or not self.render.pages:
      return
    numbers = [p.number for p in self.render.pages]
    number = min(max(int(number), min(numbers)), max(numbers))
    if number != self._page_number:
      self._page_number = number
      self._apply_page_size()
      self.pageChanged.emit(number)
      self.update()

  def next_page(self) -> None:
    self.set_page(self._page_number + 1)

  def previous_page(self) -> None:
    self.set_page(self._page_number - 1)

  def show_measure(self, measure: int) -> bool:
    """Switch to the page holding `measure`. Returns False when it is unknown."""
    if not self.render:
      return False
    found = self.render.box_of(measure)
    if found is None:
      return False
    page, _box = found
    self.set_page(page.number)
    return True

  # ------------------------------------------------------------------ zoom

  def zoom(self) -> float:
    return self._zoom

  def set_zoom(self, factor: float, keep_fit: bool = False) -> None:
    factor = min(max(float(factor), _ZOOM_STEPS[0]), _ZOOM_STEPS[-1])
    self._fit_pending = keep_fit
    if abs(factor - self._zoom) < 1e-6:
      return
    self._zoom = factor
    self._apply_page_size()
    self.update()

  def zoom_in(self) -> None:
    self.set_zoom(next((z for z in _ZOOM_STEPS if z > self._zoom + 1e-6), _ZOOM_STEPS[-1]))

  def zoom_out(self) -> None:
    self.set_zoom(next((z for z in reversed(_ZOOM_STEPS) if z < self._zoom - 1e-6), _ZOOM_STEPS[0]))

  def fit_width(self, viewport_width: Optional[int] = None) -> None:
    """Scale so the page fills the available width."""
    page = self.page()
    if page is None:
      return
    width = viewport_width or self.parentWidget().width() if self.parentWidget() else viewport_width
    if not width:
      return
    self._zoom = min(max(float(width) / float(page.width), _ZOOM_STEPS[0]), _ZOOM_STEPS[-1])
    self._fit_pending = False
    self._apply_page_size()
    self.update()

  def _apply_page_size(self) -> None:
    page = self.page()
    if page is None:
      return
    height = page.height
    if self._line is not None:
      height = max(1.0, self._line[1] - self._line[0])
    self.setMinimumSize(max(1, int(page.width * self._zoom)),
                        max(1, int(height * self._zoom)))
    self.updateGeometry()

  def resizeEvent(self, event):
    super().resizeEvent(event)
    if self._fit_pending and self.parentWidget() is not None:
      # first layout pass: make the page readable rather than 1:1
      self.fit_width(self.parentWidget().width())

  # ------------------------------------------------------------------ state

  def set_mask_mode(self, on: bool) -> None:
    """In mask mode a click picks the nearest *note* instead of a bar."""
    self._mask_mode = bool(on)
    self.setCursor(Qt.CrossCursor if self._mask_mode else Qt.ArrowCursor)
    self.update()

  def set_hover_glyph(self, key: Optional[str]) -> None:
    """Point at a note without tapping it, for stepping through them."""
    self._hover_glyph = key
    self._masked = self._masked            # unchanged; only the cursor target moves
    self.update()

  def set_masked(self, keys) -> None:
    """Which notes the dialog currently has masked, so they can be shown."""
    self._masked = set(keys or ())
    self.update()

  def set_selection(self, first: Optional[int], last: Optional[int],
                    external: bool = False) -> None:
    """Show a selection.

    `external=True` means the user set the range somewhere else (the measure list, a
    new piece) and any range being built by clicking is finished. It defaults to
    False on purpose: showing a selection is not the same as ending one, and while
    the default was True a routine re-sync during a click silently closed the range,
    so a second click started over instead of extending.
    """
    self._selection = (first, last)
    if external:
      self._range_open = False
    self.update()

  def range_from_click(self, measure: int, extend: bool = False):
    """The range a click on `measure` should select.

    Clicking a bar picks it; clicking a second one extends to it, which is how the
    phone client works and what a pianist expects; a third click starts again.
    Shift extends from the existing start whatever the state.
    """
    first, _last = self._selection
    if extend and first is not None:
      self._range_open = False
      return (min(first, measure), max(first, measure))
    if self._range_open and first is not None:
      self._range_open = False
      return (min(first, measure), max(first, measure))
    self._range_open = True
    return (measure, measure)

  def set_playhead_measure(self, measure: Optional[int]) -> None:
    self._playhead = measure
    self.update()

  def set_line(self, top: Optional[float], bottom: Optional[float] = None) -> None:
    """Show one system only: page-pixel `top`..`bottom`, or None for the whole page.

    Everything the widget does is in page pixels -- the pixmap, the highlights, the
    curtain -- so this only changes what part of that space is put on screen: the
    widget shrinks to the line and painting is offset by `top`.
    """
    line = None if top is None else (float(top), float(bottom))
    if line == self._line:
      return
    self._line = line
    self._apply_page_size()
    self.update()

  def line(self):
    """The system being shown, in page pixels, or None for the whole page."""
    return self._line

  def set_curtain(self, rects) -> None:
    """Cover these page-pixel rectangles, hiding what has not been played yet.

    Painted rather than masked in the SVG, and that is not the shortcut it looks like:
    masking by elements is what the *cards* need, because a note's element can be
    suppressed exactly, while the question here is a region -- everything after the
    playhead. Verovio's own note boxes are too tight to hide a note with (that is why
    masking is done in the SVG at all), but a rectangle over the paper is exactly what
    this wants. The fill is the colour the widget already paints the page against, so
    the concealed part simply looks like blank paper.
    """
    self._curtain = [tuple(float(v) for v in r) for r in (rects or [])]
    self.update()

  def curtain(self):
    """The rectangles currently covered, in page pixels."""
    return list(self._curtain)

  def measure_at(self, point: QPoint) -> Optional[int]:
    """The measure under a widget-space point, or None."""
    page = self.page()
    if page is None or self.render is None:
      return None
    scale = self._zoom
    x = point.x() / scale
    y = point.y() / scale
    return self.render.measure_at(page.number, x, y)

  # ------------------------------------------------------------------ painting

  def _pixmap(self) -> Optional[QPixmap]:
    page = self.page()
    if page is None:
      return None
    cached = self._pixmaps.get(page.number)
    if cached is None:
      pixmap = QPixmap(page.image)
      if pixmap.isNull():
        return None
      self._pixmaps[page.number] = pixmap
      cached = pixmap
    return cached

  def paintEvent(self, event):
    painter = QPainter(self)
    painter.fillRect(self.rect(), QColor('#f6f4ef'))

    pixmap = self._pixmap()
    if pixmap is None:
      painter.setPen(QColor('#8a8a8a'))
      painter.drawText(self.rect(), Qt.AlignCenter,
                       'no score rendered yet' if not self.render else 'page image missing')
      painter.end()
      return

    target = QRect(0, 0, int(pixmap.width() * self._zoom), int(pixmap.height() * self._zoom))
    if self._line is not None:
      # draw the page shifted up so the chosen system starts at the top of the widget
      painter.setClipRect(self.rect())
      painter.translate(0, -int(self._line[0] * self._zoom))
    painter.drawPixmap(target, pixmap)

    page = self.page()
    if page is not None:
      self._paint_highlight(painter, page, target)

    # The curtain goes on last, so it covers the highlight of bars that have not been
    # reached as well: a highlighted bar would give away how much music is coming.
    if self._curtain:
      painter.setPen(Qt.NoPen)
      painter.setBrush(QColor('#f6f4ef'))
      for x0, y0, x1, y1 in self._curtain:
        painter.drawRect(QRect(int(x0 * self._zoom), int(y0 * self._zoom),
                               int((x1 - x0) * self._zoom), int((y1 - y0) * self._zoom)))
    painter.end()

  def _paint_highlight(self, painter: QPainter, page: ScorePage, target: QRect) -> None:
    scale = self._zoom

    def rect_of(box) -> QRect:
      return QRect(int(target.left() + box.x0 * scale), int(target.top() + box.y0 * scale),
                   max(1, int((box.x1 - box.x0) * scale)),
                   max(1, int((box.y1 - box.y0) * scale)))

    first, last = self._selection
    if first is not None and last is not None:
      low, high = min(first, last), max(first, last)
      painter.setPen(Qt.NoPen)
      painter.setBrush(QColor(70, 140, 220, 46))
      for box in page.measures:
        if low <= box.number <= high:
          painter.drawRect(rect_of(box))
      painter.setBrush(Qt.NoBrush)

    if self._playhead is not None:
      found = self.render.box_of(self._playhead) if self.render else None
      if found and found[0].number == page.number:
        painter.setPen(QPen(QColor('#e0a020'), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect_of(found[1]).adjusted(0, 0, -1, -1))

    if self._hover is not None:
      box = next((b for b in page.measures if b.number == self._hover), None)
      if box is not None:
        painter.setPen(QPen(QColor('#2f6f2f'), 1, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect_of(box).adjusted(0, 0, -1, -1))

    # masked notes, and the one the cursor would mask: drawn from the hit rectangles,
    # because those are where the glyphs actually are
    for glyph in page.glyphs:
      if glyph.kind != 'note' or not glyph.has_hit:
        continue
      masked = glyph.key in self._masked
      hovered = glyph.key == self._hover_glyph
      if not (masked or hovered):
        continue
      rect = QRect(int(target.left() + glyph.hit_x0 * scale),
                   int(target.top() + glyph.hit_y0 * scale),
                   max(2, int((glyph.hit_x1 - glyph.hit_x0) * scale)),
                   max(2, int((glyph.hit_y1 - glyph.hit_y0) * scale)))
      if masked:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawRect(rect)
        painter.setPen(QPen(QColor('#b03030'), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)
      else:
        painter.setPen(QPen(QColor('#b03030'), 1, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)

  # ------------------------------------------------------------------ input

  def glyph_at_point(self, point) -> Optional[str]:
    """The key of the note nearest a point, judged by where notes are drawn."""
    page = self.page()
    if page is None or self.render is None:
      return None
    glyph = self.render.nearest_glyph(page.number, point.x() / self._zoom,
                                      point.y() / self._zoom)
    return glyph.key if glyph else None

  def mousePressEvent(self, event):
    if event.button() != Qt.LeftButton:
      return super().mousePressEvent(event)
    if self._mask_mode:
      key = self.glyph_at_point(event.pos())
      if key is not None:
        self.setFocus(Qt.MouseFocusReason)
        self.glyphClicked.emit(key)
      return
    measure = self.measure_at(event.pos())
    if measure is not None:
      self.setFocus(Qt.MouseFocusReason)
      first, last = self.range_from_click(
          measure, extend=bool(event.modifiers() & Qt.ShiftModifier))
      self.rangeSelected.emit(first, last)

  def mouseDoubleClickEvent(self, event):
    measure = self.measure_at(event.pos())
    if measure is not None:
      self.measureActivated.emit(measure)

  def mouseMoveEvent(self, event):
    if self._mask_mode:
      key = self.glyph_at_point(event.pos())
      if key != self._hover_glyph:
        self._hover_glyph = key
        self.update()
      return
    measure = self.measure_at(event.pos())
    if measure != self._hover:
      self._hover = measure
      self.setCursor(Qt.PointingHandCursor if measure is not None else Qt.ArrowCursor)
      self.update()

  def leaveEvent(self, event):
    if self._hover is not None:
      self._hover = None
      self.update()
    super().leaveEvent(event)

  def wheelEvent(self, event):
    if event.modifiers() & Qt.ControlModifier:
      self.zoom_in() if event.angleDelta().y() > 0 else self.zoom_out()
      event.accept()
      return
    super().wheelEvent(event)

  def keyPressEvent(self, event):
    if event.key() in (Qt.Key_Left, Qt.Key_Right) and self.render is not None:
      numbers = sorted({b.number for p in self.render.pages for b in p.measures})
      if numbers:
        current = self._playhead if self._playhead in numbers else numbers[0]
        index = numbers.index(current)
        index = max(0, min(len(numbers) - 1, index + (-1 if event.key() == Qt.Key_Left else 1)))
        # Move the marker as well as reporting it: without this, holding an arrow
        # key would keep re-emitting the same bar instead of walking the piece.
        self._playhead = numbers[index]
        self._range_open = False
        self.show_measure(self._playhead)
        self.update()
        self.rangeSelected.emit(self._playhead, self._playhead)
        return
    if event.key() in (Qt.Key_PageDown, Qt.Key_PageUp):
      self.next_page() if event.key() == Qt.Key_PageDown else self.previous_page()
      return
    super().keyPressEvent(event)
