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
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from .. import library, scoreview, syncmap
from ..config import AnalyzerConfig
from ..scoreview import ScoreRender
from .score_widget import ScoreView
from .player import MAX_SPEED, MIN_SPEED, RangePlayer

# The same rolls the rest of the app uses when it plays a passage: starting exactly on
# the downbeat clips the piano's attack.
PRE_ROLL_S = 0.12
POST_ROLL_S = 0.25

BY_BEAT = 'every beat'
BY_BAR = 'every bar'

ONE_LINE = 'one line'
WHOLE_PAGE = 'whole page'

# Below this the map is telling us it is not sure of a bar's boundary -- the DTW fit
# was erratic there and the marker was clamped to something plausible. The page follows
# the map, so it is worth saying so at the moment it matters rather than leaving the
# player to wonder why the page slipped.
UNSURE_CONFIDENCE = 0.5

SPEED_MIN, SPEED_MAX = 0.25, 4.0
SPEED_STEP = 0.05
NUDGE_MIN_MS, NUDGE_MAX_MS = -3000, 3000
NUDGE_STEP_MS = 25
PLAY_STEP = 0.05


def warped_clock(seconds: float, start: float, speed: float = 1.0, nudge_ms: float = 0.0,
                 end: Optional[float] = None) -> float:
  """The clock the page is revealed by, from the clock the music is playing by.

  The display follows the measure map, and a map is an estimate: DTW fits bar boundaries
  to a recording to within a few hundred milliseconds at best, and on this score twenty
  bars had to be clamped because the fit there was erratic. Where the map is slow the
  music gets ahead of the page, and no care in the reveal can fix that -- the times it
  is handed are wrong. So the two clocks are separated here and can be lined up by hand:

    * **page speed** scales how fast the page advances relative to the music, for a map
      that is uniformly too slow (or too fast);
    * **nudge** shifts the page in milliseconds, for a constant lag -- a map that is
      late everywhere, or the small delay of a 40 ms position tick.

  The warp is anchored at the start of the passage being played, so moving either dial
  does not change which bar you are on at that moment, and the result is clamped to the
  passage: at 2 the page reaches the last bar and waits there while the music finishes,
  rather than running off the end.
  """
  if speed <= 0:
    speed = 1.0
  warped = float(start) + (float(seconds) - float(start)) * float(speed)
  warped += float(nudge_ms) / 1000.0
  if end is not None:
    warped = min(warped, float(end))
  return max(0.0, warped)


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
    self._bands: Dict[int, list] = {}
    self._playing = False
    self._range: Optional[Tuple[float, Optional[float]]] = None
    self._seconds = 0.0
    self._confidence: Dict[int, Optional[float]] = {}

    layout = QVBoxLayout(self)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addLayout(self._controls())

    self.view = ScoreView()
    self.view.set_curtain([])
    self.view.set_playhead_outline(self.outline_check.isChecked())
    self.scroll = QScrollArea()
    self.scroll.setWidget(self.view)
    self.scroll.setWidgetResizable(True)
    self.scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
    layout.addWidget(self.scroll, 1)

    self.hint = QLabel()
    self.hint.setWordWrap(True)
    self.hint.setStyleSheet('color: #8a8a8a;')
    layout.addWidget(self.hint)
    self._set_hint()

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

    # Off by default: the box traces the staves, not the music, so its lower edge runs
    # through the dynamics -- and the curtain already marks where the music has got to.
    self.outline_check = QCheckBox('bar outline')
    self.outline_check.setToolTip('draw a box around the bar being played')
    self.outline_check.toggled.connect(lambda on: self.view.set_playhead_outline(on))
    row.addWidget(self.outline_check)

    row.addSpacing(12)
    row.addWidget(QLabel('view'))
    self.view_combo = QComboBox()
    self.view_combo.addItems([ONE_LINE, WHOLE_PAGE])
    self.view_combo.setToolTip('a single line of music at a time, or the whole page')
    self.view_combo.currentTextChanged.connect(lambda _text: self._view_changed())
    row.addWidget(self.view_combo)

    row.addSpacing(12)
    row.addWidget(QLabel('reveal'))
    self.unit_combo = QComboBox()
    self.unit_combo.addItems([BY_BEAT, BY_BAR])
    self.unit_combo.setToolTip('how finely the page is uncovered as it plays')
    self.unit_combo.currentTextChanged.connect(lambda _text: self._refresh())
    row.addWidget(self.unit_combo)

    # The map is an estimate, so the page can run behind the music or ahead of it. These
    # two line the page up with what is actually heard; see `warped_clock`.
    # The music's own speed, which is the other half of practising slowly: the page
    # dial lines the page up with the music, this one slows the music down. Pitch is
    # preserved, and the map and the reveal both stay in the recording's seconds.
    row.addSpacing(12)
    row.addWidget(QLabel('play speed'))
    self.play_speed_spin = QDoubleSpinBox()
    self.play_speed_spin.setRange(MIN_SPEED, MAX_SPEED)
    self.play_speed_spin.setSingleStep(PLAY_STEP)
    self.play_speed_spin.setDecimals(2)
    self.play_speed_spin.setValue(1.0)
    self.play_speed_spin.setSuffix('x')
    self.play_speed_spin.setToolTip('play the recording slower (or faster) without '
                                    'changing the pitch; the page follows at the same pace')
    self.play_speed_spin.valueChanged.connect(self._play_speed_changed)
    row.addWidget(self.play_speed_spin)
    self.reset_speed_btn = QPushButton('1x')
    self.reset_speed_btn.setFixedWidth(34)
    self.reset_speed_btn.setToolTip('back to the recording\'s own pace')
    self.reset_speed_btn.clicked.connect(lambda: self.play_speed_spin.setValue(1.0))
    row.addWidget(self.reset_speed_btn)

    row.addSpacing(12)
    row.addWidget(QLabel('page speed'))
    self.speed_spin = QDoubleSpinBox()
    self.speed_spin.setRange(SPEED_MIN, SPEED_MAX)
    self.speed_spin.setSingleStep(SPEED_STEP)
    self.speed_spin.setDecimals(2)
    self.speed_spin.setValue(1.0)
    self.speed_spin.setSuffix('x')
    self.speed_spin.setToolTip('raise this if the music runs ahead of the page, '
                               'lower it if the page runs ahead of the music')
    self.speed_spin.valueChanged.connect(lambda _value: self._refresh())
    row.addWidget(self.speed_spin)

    self.nudge_spin = QSpinBox()
    self.nudge_spin.setRange(NUDGE_MIN_MS, NUDGE_MAX_MS)
    self.nudge_spin.setSingleStep(NUDGE_STEP_MS)
    self.nudge_spin.setSuffix(' ms')
    self.nudge_spin.setToolTip('shift the page in time; negative reveals it earlier')
    self.nudge_spin.valueChanged.connect(lambda _value: self._refresh())
    row.addWidget(self.nudge_spin)

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
    self._bands = {}
    self._playing = False
    self._range = None
    self._confidence = {m.measure: m.confidence for m in (result.measures if result else [])}
    self._set_hint()
    self.view.set_render(render)
    self.view.set_curtain([])
    if render and render.pages:
      self.view.set_page(render.pages[0].number)
      self._show_line(None)
    else:
      self.page_label.setText('no engraved score')
    bars = [m.measure for m in (result.measures if result else [])]
    if bars:
      self.first_bar, self.last_bar = min(bars), max(bars)
    self._update_ready()

  def _set_hint(self) -> None:
    """What the dials are for, and any warning about the map the page follows."""
    lines = ['Press ▶ and read: the music you have not reached yet is covered, so it '
             'cannot be read ahead. Stop to uncover the page again.',
             'The page follows the measure map, which is an estimate: if the music '
             'runs ahead of the page, raise “page speed”; if the page is ahead of the '
             'music, lower it. “nudge” shifts the page in time.']
    if self.result is not None:
      lines.extend(self.result.warnings[:1])
    self.hint.setText('\n'.join(lines))

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
    # the dials are anchored here, so turning one mid-passage does not shift the bar
    self._range = (start, end)
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

  def _reveal_seconds(self, seconds: float) -> float:
    """Where to reveal up to, for a clock reading: the music's own times, adjusted."""
    start, end = self._range or (0.0, None)
    return warped_clock(seconds, start, speed=self.speed_spin.value(),
                        nudge_ms=self.nudge_spin.value(), end=end)

  def _refresh(self, seconds: Optional[float] = None) -> None:
    """Uncover up to the clock's position, or up to nothing if not playing."""
    if not self._playing:
      self._update_ready()
      return
    if seconds is None:
      seconds = self._last_seconds()
    self._seconds = seconds
    reveal_at = self._reveal_seconds(seconds)
    measure, span = self._measure_at(reveal_at)
    if measure is None:
      return
    reveal = self._reveal_for(self.view.page_number)
    if reveal is None or span is None:
      return
    if not any(m.measure == measure for m in reveal.marks):
      # the bar is on another page: turn to it, then reveal
      if self.view.show_measure(measure):
        reveal = self._reveal_for(self.view.page_number)
        if reveal is None:
          return
    reveal = self._show_line(measure) or reveal
    if self.unit_combo.currentText() == BY_BAR:
      unit = max(0, (reveal.units.get(measure) or 1) - 1)
    else:
      unit = reveal.unit_of(measure, reveal_at, span)
    self.view.set_playhead_measure(measure)
    self.view.set_curtain(self.curtain_rects(reveal, measure, unit))
    if reveal.coarse and measure in reveal.coarse:
      note = ' · by the bar (its durations do not add up)'
    else:
      note = ' · beat %d of %d' % (unit + 1, max(1, reveal.units.get(measure) or 1))
    self.status.setText('bar %d%s · %.1f s%s%s'
                        % (measure, note, reveal_at, self._unsure_note(measure),
                           self._settings_note()))

  def _line_view(self) -> bool:
    return self.view_combo.currentText() == ONE_LINE

  def _view_changed(self) -> None:
    """Switching between one line and the whole page re-lays the view out."""
    self._reveals = self._reveals                # kept: the reveal does not depend on it
    if not self._line_view():
      self.view.set_line(None)
    self._show_line(self._playing_measure())
    self._refresh()

  def _playing_measure(self) -> Optional[int]:
    """The bar the display is showing, if any, so a layout change can keep it."""
    measure, _span = self._measure_at(self._reveal_seconds(self._seconds))
    return measure

  def _line_bands(self, reveal: scoreview.PageReveal, page_number: int):
    """The band to show for each line of this page, measured from the page's own ink.

    A staff box is not the music: on this score the ink reaches 73 px above the staves
    and 82 px below them, so cutting the band to the staves (plus a couple of notehead
    heights, as this first did) clips real music off every line.
    """
    cached = self._bands.get(page_number)
    if cached is None:
      page = next((p for p in self.render.pages if p.number == page_number), None)
      if page is None:
        return []
      cached = scoreview.line_bands(reveal, page.image)
      self._bands[page_number] = cached
    return cached

  def _show_line(self, measure: Optional[int]) -> Optional[scoreview.PageReveal]:
    """Put the system holding `measure` on screen, and say what the reveal is.

    One line at a time is both cleaner to read and simpler to conceal: with no line
    above or below it there is no question about what to do with them.
    """
    reveal = self._reveal_for(self.view.page_number)
    if reveal is None:
      return None
    if measure is None:
      # nothing playing: the first line of the page, so the view is never empty
      measure = min((m.measure for m in reveal.marks), default=None)
    if measure is None:
      return None
    band = reveal.system_band(measure)
    if band is None:
      return reveal
    if self._line_view():
      bands = self._line_bands(reveal, self.view.page_number)
      number = reveal.system_number(measure) or 1
      shown = bands[number - 1] if 0 < number <= len(bands) else band
      self.view.set_line(*shown)
      number = reveal.system_number(measure)
      total = len(reveal.systems)
      self.page_label.setText('page %d / %d · line %s / %d'
                              % (self.view.page_number, len(self.render.pages),
                                 number, total))
    else:
      self.view.set_line(None)
      self.page_label.setText('page %d / %d'
                              % (self.view.page_number, len(self.render.pages)))
    return reveal

  def curtain_rects(self, reveal: scoreview.PageReveal, measure: int, unit: int):
    """What to cover, cut down to the line being shown when only one is on screen."""
    rects = reveal.rects(measure, unit)
    line = self.view.line()
    if line is None:
      return rects
    top, bottom = line
    out = []
    for x0, y0, x1, y1 in rects:
      y0, y1 = max(y0, top), min(y1, bottom)
      if y1 > y0:
        out.append((x0, y0, x1, y1))
    return out

  def _unsure_note(self, measure: int) -> str:
    """Flag a bar the map is not sure of: this is where the page can slip."""
    confidence = self._confidence.get(measure)
    if confidence is None or confidence >= UNSURE_CONFIDENCE:
      return ''
    return ' · map unsure here (%.2f)' % confidence

  def _play_speed_changed(self, value: float) -> None:
    """Hand the speed to the player, which carries on from where it is."""
    self.player.set_speed(value)
    self._refresh()

  def _settings_note(self) -> str:
    """Say when the page is being nudged off the map, so a wrong setting is visible."""
    speed = self.speed_spin.value()
    nudge = self.nudge_spin.value()
    playing = self.play_speed_spin.value()
    note = ''
    if abs(playing - 1.0) > 0.001:
      note += ' · play x%.2f' % playing
    if abs(speed - 1.0) > 0.001 or nudge:
      note += ' · page %s%+.0f ms' % ('x%.2f ' % speed if abs(speed - 1.0) > 0.001 else '',
                                      nudge)
    return note

  def _last_seconds(self) -> float:
    return getattr(self, '_seconds', 0.0)

  def unfollow(self) -> None:
    """Leaving the tab: keep playing, but stop covering the page."""
    self._playing = False
    self.view.set_curtain([])
