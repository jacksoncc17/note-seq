"""Sync playing: the score revealed at the pace of the music, never ahead of it.

A reading aid, not a viewer. The recording plays and the page keeps up, but the part
that has not been reached yet is covered -- you cannot read the next bar off the
screen, which is the whole point: it forces the piece to be recalled at tempo rather
than sight-read in advance.

Two things make it work:

* **The reveal unit is the score's own.** `scoreview.page_reveal` knows which metrical
  unit each notehead begins at -- quarters in 3/4, eighths in 6/8, straight from the
  score's durations -- so the page advances note group by note group, and falls back to
  whole bars where a bar's durations do not add up.
* **The clock is the measure map's.** Bars are timed by the recording's own map, and
  the units inside a bar are taken as evenly spaced across it. That is the one estimate
  in the feature: a performance does not announce its beats, so a fermata or a big
  rallentando inside a bar can put the reveal a fraction of a beat out. Whole-bar
  reveal is the honest fallback and is one click away.

The player is the window's, not a second one: one thing plays at a time, and the
transport here is the transport there.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

from .. import library, scoreview, syncmap
from ..config import AnalyzerConfig
from ..scoreview import ScoreRender
from .score_widget import ScoreView
from .player import RangePlayer

# The same rolls the rest of the app uses when it plays a passage: starting exactly on
# the downbeat clips the piano's attack.
PRE_ROLL_S = 0.12
POST_ROLL_S = 0.25

BY_BEAT = 'every beat'
BY_BAR = 'every bar'


class SyncPlayingPanel(QWidget):
  """The recording, plus the score revealed only as far as it has been played."""

  def __init__(self, cfg: AnalyzerConfig, player: RangePlayer, parent=None):
    super().__init__(parent)
    self.cfg = cfg
    self.player = player
    self.piece: Optional[library.Piece] = None
    self.result: Optional[syncmap.SyncResult] = None
    self.render: Optional[ScoreRender] = None
    self.first_bar = 1
    self.last_bar = 1
    self._reveals: Dict[int, scoreview.PageReveal] = {}
    self._playing = False

    layout = QVBoxLayout(self)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addLayout(self._controls())

    self.view = ScoreView()
    self.view.set_curtain([])
    self.scroll = QScrollArea()
    self.scroll.setWidget(self.view)
    self.scroll.setWidgetResizable(True)
    self.scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
    layout.addWidget(self.scroll, 1)

    hint = QLabel('Press ▶ and read: the music you have not reached yet is covered, '
                  'so it cannot be read ahead. Stop to uncover the page again.')
    hint.setStyleSheet('color: #8a8a8a;')
    layout.addWidget(hint)

  def _controls(self) -> QHBoxLayout:
    row = QHBoxLayout()
    self.play_btn = QPushButton('▶ Play')
    self.play_btn.clicked.connect(self.play)
    row.addWidget(self.play_btn)

    self.stop_btn = QPushButton('■')
    self.stop_btn.setFixedWidth(40)
    self.stop_btn.setToolTip('stop and uncover the page')
    self.stop_btn.clicked.connect(self.stop)
    row.addWidget(self.stop_btn)

    self.loop_check = QCheckBox('loop')
    row.addWidget(self.loop_check)

    row.addSpacing(12)
    row.addWidget(QLabel('reveal'))
    self.unit_combo = QComboBox()
    self.unit_combo.addItems([BY_BEAT, BY_BAR])
    self.unit_combo.setToolTip('how finely the page is uncovered as it plays')
    self.unit_combo.currentTextChanged.connect(lambda _text: self._refresh())
    row.addWidget(self.unit_combo)

    self.page_prev = QPushButton('‹')
    self.page_prev.setFixedWidth(32)
    self.page_prev.clicked.connect(lambda: self.view.previous_page())
    row.addWidget(self.page_prev)
    self.page_label = QLabel('—')
    row.addWidget(self.page_label)
    self.page_next = QPushButton('›')
    self.page_next.setFixedWidth(32)
    self.page_next.clicked.connect(lambda: self.view.next_page())
    row.addWidget(self.page_next)

    self.status = QLabel('no recording yet')
    row.addWidget(self.status, 1)
    return row

  # ------------------------------------------------------------------ content

  def set_piece(self, piece: Optional[library.Piece], render: Optional[ScoreRender],
                result: Optional[syncmap.SyncResult]) -> None:
    """Hand over the piece, its engraving and its map; this is all it needs."""
    self.piece = piece
    self.render = render
    self.result = result
    self._reveals = {}
    self._playing = False
    self.view.set_render(render)
    self.view.set_curtain([])
    if render and render.pages:
      self.view.set_page(render.pages[0].number)
      self.page_label.setText('page %d / %d' % (self.view.page_number, len(render.pages)))
    else:
      self.page_label.setText('no engraved score')
    bars = [m.measure for m in (result.measures if result else [])]
    if bars:
      self.first_bar, self.last_bar = min(bars), max(bars)
    self._update_ready()

  def set_range(self, first: int, last: int) -> None:
    self.first_bar, self.last_bar = min(first, last), max(first, last)

  def _update_ready(self) -> None:
    ready = bool(self.piece and self.piece.has_audio and self.result
                 and self.result.measures and self.render)
    self.play_btn.setEnabled(ready)
    if not self.piece:
      self.status.setText('no piece selected')
    elif not self.piece.has_audio:
      self.status.setText('no recording — fetch one on the Recording tab')
    elif not self.result or not self.result.measures:
      self.status.setText('no measure map yet — align this piece first')
    elif not self.render:
      self.status.setText('no engraved score to reveal')
    else:
      self.status.setText('ready · bars %d-%d' % (self.first_bar, self.last_bar))

  # ------------------------------------------------------------------ playing

  def play(self) -> None:
    if not (self.piece and self.result and self.result.measures):
      return
    span = self.result.range_for_measures(self.first_bar, self.last_bar)
    if span is None:
      self.status.setText('the map has nothing for bars %d-%d'
                          % (self.first_bar, self.last_bar))
      return
    duration = self.player.duration
    start = max(0.0, span[0] - PRE_ROLL_S)
    end = min(duration, span[1] + POST_ROLL_S) if duration else span[1] + POST_ROLL_S
    self._playing = True
    if not self.player.play_range(start, end):
      self.status.setText('could not play that range')
      self._playing = False

  def stop(self) -> None:
    self._playing = False
    self.player.stop()
    self.view.set_playhead_measure(None)
    self.view.set_curtain([])                 # uncovered again, for browsing
    self._update_ready()

  def on_position(self, seconds: float) -> None:
    """The clock moved: uncover exactly as far as it has got."""
    if not self._playing:
      return
    self._refresh(seconds)

  def on_finished(self) -> None:
    if not self._playing:
      return
    if self.loop_check.isChecked():
      self.play()
      return
    self._playing = False
    self.view.set_curtain([])
    self._update_ready()

  # ------------------------------------------------------------------ revealing

  def _measure_at(self, seconds: float):
    """(measure, its [start, end)) for the clock, from the map."""
    if not self.result:
      return None, None
    for m in self.result.measures:
      if m.start_s <= seconds < m.end_s:
        return m.measure, (m.start_s, m.end_s)
    # past the end of the last bar, or before the first: hold the nearest
    measures = sorted(self.result.measures, key=lambda m: m.start_s)
    if measures and seconds >= measures[-1].end_s:
      return measures[-1].measure, (measures[-1].start_s, measures[-1].end_s)
    return None, None

  def _reveal_for(self, page_number: int) -> Optional[scoreview.PageReveal]:
    if self.render is None:
      return None
    page = next((p for p in self.render.pages if p.number == page_number), None)
    if page is None:
      return None
    cached = self._reveals.get(page_number)
    if cached is None:
      try:
        cached = scoreview.page_reveal(self.cfg, self.piece.musicxml, page_number, page.width)
      except Exception:
        return None
      self._reveals[page_number] = cached
    return cached

  def _refresh(self, seconds: Optional[float] = None) -> None:
    """Uncover up to the clock's position, or up to nothing if not playing."""
    if not self._playing:
      self._update_ready()
      return
    if seconds is None:
      seconds = self._last_seconds()
    self._seconds = seconds
    measure, span = self._measure_at(seconds)
    if measure is None:
      return
    reveal = self._reveal_for(self.view.page_number)
    if reveal is None or span is None:
      return
    if not any(m.measure == measure for m in reveal.marks):
      # the bar is on another page: turn to it, then reveal
      if self.view.show_measure(measure):
        self.page_label.setText('page %d / %d'
                                % (self.view.page_number, len(self.render.pages)))
        reveal = self._reveal_for(self.view.page_number)
        if reveal is None:
          return
    if self.unit_combo.currentText() == BY_BAR:
      unit = max(0, (reveal.units.get(measure) or 1) - 1)
    else:
      unit = reveal.unit_of(measure, seconds, span)
    self.view.set_playhead_measure(measure)
    self.view.set_curtain(reveal.rects(measure, unit))
    if reveal.coarse and measure in reveal.coarse:
      self.status.setText('bar %d · by the bar (its durations do not add up)'
                          % measure)
    else:
      self.status.setText('bar %d · beat %d of %d · %.1f s'
                          % (measure, unit + 1, max(1, reveal.units.get(measure) or 1),
                             seconds))

  def _last_seconds(self) -> float:
    return getattr(self, '_seconds', 0.0)

  def unfollow(self) -> None:
    """Leaving the tab: keep playing, but stop covering the page."""
    self._playing = False
    self.view.set_curtain([])
