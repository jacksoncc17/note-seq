"""The waveform strip with one draggable marker per measure.

This is the part of the app that makes an automatic alignment trustworthy. A
DTW alignment is right most of the time and wrong in ways nobody can predict
(long fermatas, ambiguous repeats, a page turn), so the mapping is *shown* as
markers on the audio and can be corrected by dragging. The widget therefore
knows nothing about alignment: it draws an envelope, draws markers, and reports
when the user moves one.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np
from PyQt5.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QSizePolicy, QWidget


_TICK_HIT_PX = 6          # how close a click must be to grab a marker
_MIN_LABEL_SPACING_PX = 34


class WaveformView(QWidget):
  """An audio envelope with measure markers and a selected passage.

  Signals:
    tickMoved: (play_index, seconds) after the user drags a marker.
    tickClicked: (play_index) on a single click of a marker.
    positionClicked: (seconds) when the user clicks empty space.
  """

  tickMoved = pyqtSignal(int, float)
  tickClicked = pyqtSignal(int)
  positionClicked = pyqtSignal(float)

  def __init__(self, parent=None):
    super().__init__(parent)
    self.setMinimumHeight(150)
    self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    self.setMouseTracking(True)
    self.setFocusPolicy(Qt.StrongFocus)

    self._samples: Optional[np.ndarray] = None
    self._peaks: Optional[np.ndarray] = None         # (n_columns, 2) in -1..1
    self._duration = 0.0
    self._ticks: List[float] = []
    self._labels: List[str] = []
    self._enabled: List[bool] = []                   # which markers may be dragged
    self._selection: Optional[tuple] = None
    self._playhead: Optional[float] = None
    self._dragging: Optional[int] = None
    self._hover: Optional[int] = None

  # ------------------------------------------------------------------ content

  def set_audio(self, samples: Optional[Sequence[float]], sample_rate: int = 22050) -> None:
    """Show the envelope of `samples` (mono), or nothing when None."""
    if samples is None or len(samples) == 0 or sample_rate <= 0:
      self._samples, self._peaks, self._duration = None, None, 0.0
    else:
      data = np.asarray(samples, dtype='float32')
      if data.ndim > 1:
        data = data.mean(axis=1)
      self._samples = data
      self._duration = float(len(data)) / float(sample_rate)
      self._recompute_peaks()
    self.update()

  @property
  def duration(self) -> float:
    return self._duration

  def set_ticks(self, seconds: Sequence[float], labels: Optional[Sequence[str]] = None,
                draggable: Optional[Sequence[bool]] = None) -> None:
    """Place the measure markers. `seconds` are absolute times in the recording."""
    self._ticks = [float(s) for s in seconds]
    self._labels = list(labels) if labels else [str(i + 1) for i in range(len(self._ticks))]
    self._enabled = list(draggable) if draggable else [True] * len(self._ticks)
    self.update()

  def set_selection(self, start_s: Optional[float], end_s: Optional[float]) -> None:
    self._selection = None if start_s is None or end_s is None else (start_s, end_s)
    self.update()

  def set_playhead(self, seconds: Optional[float]) -> None:
    self._playhead = seconds
    self.update()

  def ticks(self) -> List[float]:
    return list(self._ticks)

  # ------------------------------------------------------------------ geometry

  def _plot_rect(self) -> QRect:
    return QRect(0, 6, max(1, self.width()), max(1, self.height() - 22))

  def _x_of(self, seconds: float) -> float:
    rect = self._plot_rect()
    if self._duration <= 0:
      return rect.left()
    return rect.left() + rect.width() * max(0.0, min(1.0, seconds / self._duration))

  def _seconds_of(self, x: float) -> float:
    rect = self._plot_rect()
    if rect.width() <= 0 or self._duration <= 0:
      return 0.0
    return max(0.0, min(1.0, (x - rect.left()) / rect.width())) * self._duration

  def _nearest_tick(self, pos: QPoint) -> Optional[int]:
    best, best_dx = None, _TICK_HIT_PX + 1
    for i, seconds in enumerate(self._ticks):
      dx = abs(self._x_of(seconds) - pos.x())
      if dx < best_dx:
        best, best_dx = i, dx
    return best if best_dx <= _TICK_HIT_PX else None

  def _recompute_peaks(self) -> None:
    rect = self._plot_rect()
    columns = max(1, rect.width())
    data = self._samples
    if data is None or len(data) == 0:
      self._peaks = None
      return
    edges = np.linspace(0, len(data), columns + 1).astype(int)
    peaks = np.zeros((columns, 2), dtype='float32')
    for c in range(columns):
      lo, hi = edges[c], max(edges[c] + 1, edges[c + 1])
      chunk = data[lo:hi]
      if len(chunk):
        peaks[c, 0] = float(chunk.min())
        peaks[c, 1] = float(chunk.max())
    self._peaks = peaks

  # ------------------------------------------------------------------ painting

  def resizeEvent(self, event):
    super().resizeEvent(event)
    self._recompute_peaks()

  def paintEvent(self, event):
    painter = QPainter(self)
    painter.fillRect(self.rect(), QColor('#111417'))
    rect = self._plot_rect()

    # baseline
    mid = rect.center().y() + 3
    painter.setPen(QPen(QColor('#2c3439'), 1))
    painter.drawLine(rect.left(), mid, rect.right(), mid)

    if self._selection is not None:
      x0 = self._x_of(self._selection[0])
      x1 = self._x_of(self._selection[1])
      painter.fillRect(QRect(int(x0), rect.top(), max(1, int(x1 - x0)), rect.height()),
                       QColor(90, 170, 255, 40))

    if self._peaks is not None:
      painter.setPen(QPen(QColor('#6fa8c9'), 1))
      half = rect.height() / 2.0 - 4
      for c in range(self._peaks.shape[0]):
        top = mid - self._peaks[c, 1] * half
        bottom = mid - self._peaks[c, 0] * half
        painter.drawLine(int(rect.left() + c), int(top),
                         int(rect.left() + c), int(bottom + 1))
    else:
      painter.setPen(QColor('#5a666d'))
      painter.drawText(rect, Qt.AlignCenter, 'no recording loaded')

    self._draw_ticks(painter, rect)

    if self._playhead is not None:
      x = self._x_of(self._playhead)
      painter.setPen(QPen(QColor('#ffd166'), 2))
      painter.drawLine(int(x), rect.top(), int(x), rect.bottom())

    painter.end()

  def _draw_ticks(self, painter: QPainter, rect: QRect) -> None:
    if not self._ticks:
      return
    font = QFont()
    font.setPointSizeF(max(6.5, font.pointSizeF() - 2))
    painter.setFont(font)
    last_label_x = -1e9
    for i, seconds in enumerate(self._ticks):
      x = self._x_of(seconds)
      if x < rect.left() - 1 or x > rect.right() + 1:
        continue
      draggable = i < len(self._enabled) and self._enabled[i]
      colour = QColor('#8be28b') if draggable else QColor('#77848b')
      if i == self._hover:
        colour = QColor('#ffffff')
      painter.setPen(QPen(colour, 1, Qt.DashLine if draggable else Qt.SolidLine))
      painter.drawLine(int(x), rect.top(), int(x), rect.bottom())
      # only every label that fits, so a dense map stays readable
      if x - last_label_x >= _MIN_LABEL_SPACING_PX:
        painter.setPen(QColor('#c9d6dd'))
        label = self._labels[i] if i < len(self._labels) else str(i + 1)
        painter.drawText(int(x) + 2, rect.bottom() + 14, label)
        last_label_x = x

  # ------------------------------------------------------------------ input

  def mousePressEvent(self, event):
    if event.button() != Qt.LeftButton:
      return super().mousePressEvent(event)
    index = self._nearest_tick(event.pos())
    if index is not None and index < len(self._enabled) and self._enabled[index]:
      self._dragging = index
      self.tickClicked.emit(index)
    elif index is not None:
      self.tickClicked.emit(index)
    else:
      self.positionClicked.emit(self._seconds_of(event.pos().x()))

  def mouseMoveEvent(self, event):
    if self._dragging is not None:
      self.tickMoved.emit(self._dragging, self._seconds_of(event.pos().x()))
      return
    hover = self._nearest_tick(event.pos())
    if hover != self._hover:
      self._hover = hover
      self.setCursor(Qt.SizeHorCursor if hover is not None else Qt.ArrowCursor)
      self.update()

  def mouseReleaseEvent(self, event):
    self._dragging = None
    super().mouseReleaseEvent(event)

  def keyPressEvent(self, event):
    """Arrow keys nudge the hovered marker; this is how you fix a bar exactly."""
    if self._hover is None or self._hover >= len(self._ticks):
      return super().keyPressEvent(event)
    step = 0.01 if event.modifiers() & Qt.ShiftModifier else 0.1
    if event.key() in (Qt.Key_Left, Qt.Key_Right):
      delta = -step if event.key() == Qt.Key_Left else step
      self.tickMoved.emit(self._hover, max(0.0, self._ticks[self._hover] + delta))
    else:
      super().keyPressEvent(event)
