"""Sync playing: the score revealed at the pace of the music, never ahead of it.

A reading aid, not a viewer. The recording plays and the page keeps up, but the part
that has not been reached yet is covered -- you cannot read the next bar off the
screen, which is the whole point: it forces the piece to be recalled at tempo rather
than sight-read in advance.

Three things make it work:

* **The reveal unit is the score's own.** `scoreview.page_reveal` knows which metrical
  unit each notehead begins at -- quarters in 3/4, eighths in 6/8, straight from the
  score's durations -- so the page advances note group by note group, and falls back to
  whole bars where a bar's durations do not add up.
* **The clock is the measure map's.** Bars are timed by the recording's own map, and
  the units inside a bar are taken as evenly spaced across it. That is the one estimate
  in the feature: a performance does not announce its beats, so a fermata or a big
  rallentando inside a bar can put the reveal a fraction of a beat out. Whole-bar
  reveal is the honest fallback and is one click away.
* **Or the clock is the player's own, following what it hears.** With "listen to me"
  ticked the page is revealed from the position an online DTW estimates in the incoming
  audio -- a microphone in the room, or a file replayed at speed -- instead of from the
  recording's clock. It is one more clock into the same reveal, not a second code path:
  everything below (page turns, the line view, the curtain, the dials) is unchanged, and
  with the box unticked nothing about it is used. See `follow` for what that estimator
  was measured to do, and `capture` for where the audio comes from.

The player is the window's, not a second one: one thing plays at a time, and the
transport here is the transport there.
"""

from __future__ import annotations

import pathlib
from typing import Dict, Optional, Tuple

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                             QFileDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                             QSpinBox, QVBoxLayout, QWidget)

from .. import capture, follow, library, scoreview, syncmap
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

# Listening to the piano. The follower reads one position per hop (46 ms) and costs
# about 0.3 ms a frame, so a 40 ms tick keeps up with room to spare -- and it runs on
# the GUI thread, which is why the sources hand blocks over rather than blocking.
FOLLOW_TICK_MS = 40
FOLLOW_DRAIN = 4                 # blocks per tick: a slow tick must not fall behind

MIC = 'microphone'
TRACK = 'the practice track'
FILE = 'a file…'


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

    # Listening to the piano: off unless the box is ticked, and not even built when
    # the feature is switched off in the config (see `AnalyzerConfig.follow_enabled`).
    self._following = False
    self._follower: Optional[follow.OnlineFollower] = None
    self._source: Optional[capture.AudioSource] = None
    self.follow_file: Optional[pathlib.Path] = None
    self._choosing_file = False
    self.follow_check: Optional[QCheckBox] = None
    self.follow_source: Optional[QComboBox] = None
    self.follow_device: Optional[QComboBox] = None
    self.follow_status: Optional[QLabel] = None
    self._follow_timer = QTimer(self)
    self._follow_timer.setInterval(FOLLOW_TICK_MS)
    self._follow_timer.timeout.connect(self._follow_tick)

    layout = QVBoxLayout(self)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addLayout(self._controls())
    if self.cfg.follow_enabled:
      layout.addLayout(self._follow_controls())

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

  # ------------------------------------------------------------------ listening

  def _follow_controls(self) -> QHBoxLayout:
    """The second row: what to listen to, and whether the page believes it.

    Built only when `AnalyzerConfig.follow_enabled`; with the feature switched off
    there is no box to tick, no device to open, and no timer behind any of it.
    """
    row = QHBoxLayout()
    self.follow_check = QCheckBox('listen to me')
    self.follow_check.setToolTip(
      'reveal the page from the position heard in the incoming audio -- your piano '
      'through the microphone -- instead of from the recording\'s clock')
    self.follow_check.toggled.connect(self._follow_toggled)
    row.addWidget(self.follow_check)

    self.follow_source = QComboBox()
    self.follow_source.addItems([MIC, TRACK, FILE])
    self.follow_source.setToolTip(
      'what to listen to. The microphone is the real thing and is followed against '
      'the piece\'s recording. The practice track is that same pipeline fed a file, '
      'so it can be seen working with no piano in the room. A file is anything else '
      'you want to try, followed against the recording.')
    self.follow_source.currentTextChanged.connect(self._follow_source_changed)
    row.addWidget(self.follow_source)

    # Which microphone. Only meaningful for the live source, so it is hidden for the
    # others rather than left there looking as if it did something. Device names are long
    # and mostly boilerplate, so the box is capped and elides its text; the whole name is
    # on the entries themselves, which is where it is read from.
    self.follow_device = QComboBox()
    self.follow_device.setMaximumWidth(320)
    self.follow_device.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    self.follow_device.setMinimumContentsLength(18)
    default = capture.default_microphone()
    self.follow_device.addItem('the default device' + (' (%s)' % capture.device_label(default)
                                                       if default else ''), None)
    self.follow_device.setItemData(0, default or 'the system default capture device',
                                   Qt.ToolTipRole)
    for name in capture.microphones():
      self.follow_device.addItem(capture.device_label(name), name)
      self.follow_device.setItemData(self.follow_device.count() - 1, name, Qt.ToolTipRole)
    self.follow_device.setToolTip('which capture device to listen to')
    self.follow_device.setVisible(False)
    row.addWidget(self.follow_device)

    self.follow_status = QLabel('not listening')
    self.follow_status.setStyleSheet('color: #8a8a8a;')
    row.addWidget(self.follow_status, 1)
    # The signal only fires on a change, and the source has already been set by now: the
    # device box was built hidden for the file sources and must be shown for this one.
    self._follow_source_changed(self.follow_source.currentText())
    return row

  def _follow_source_changed(self, text: str) -> None:
    """A file source needs a file; choosing one is the act of selecting it."""
    if self.follow_device is not None:
      self.follow_device.setVisible(text == MIC)
    if text != FILE or self._choosing_file or self._following:
      return
    self._choosing_file = True
    try:
      start = self.follow_file
      if start is None and self.piece is not None and self.piece.has_audio:
        start = self.cfg.audio_path(self.piece.slug)
      chosen, _filter = QFileDialog.getOpenFileName(
        self, 'follow this file', str(start.parent if start else pathlib.Path.home()),
        'audio (*.wav *.flac *.mp3 *.ogg *.m4a);;everything (*)')
    finally:
      self._choosing_file = False
    if chosen:
      self.follow_file = pathlib.Path(chosen)
      return
    # nothing chosen: go back to what was selected before rather than sit on a
    # source that cannot start
    self.follow_source.blockSignals(True)
    self.follow_source.setCurrentText(MIC)
    self.follow_source.blockSignals(False)
    if self.follow_device is not None:
      self.follow_device.setVisible(True)
    if self.follow_status is not None:
      self.follow_status.setText('not listening')

  def _follow_toggled(self, on: bool) -> None:
    if on:
      if not self.start_following():
        self.follow_check.blockSignals(True)      # it did not start: say so, don't lie
        self.follow_check.setChecked(False)
        self.follow_check.blockSignals(False)
    else:
      self.stop_following()

  def start_following(self, source: Optional[capture.AudioSource] = None,
                      reference: Optional[str] = None) -> bool:
    """Reveal the page from what is heard, rather than from the recording's clock.

    The reference is the piece's own recording for anything a real piano might play,
    and the rendered practice track when the practice track is what is being fed in --
    the one case where a rendering is the right thing to compare against. `source` and
    `reference` are there so tests (and a script) can hand over work already done.
    """
    if not self.cfg.follow_enabled:
      if self.follow_status is not None:
        self.follow_status.setText('listening is switched off (ANALYZER_FOLLOW)')
      return False
    if not (self.piece and self.render and self.result and self.result.measures):
      if self.follow_status is not None:
        self.follow_status.setText('nothing to follow: %s' % self.status.text())
      return False
    kind = self.follow_source.currentText() if self.follow_source is not None else MIC
    if reference is None:
      reference = 'track' if kind == TRACK else 'recording'
    if self.follow_status is not None:
      self.follow_status.setText('building the reference (%s)…' % reference)
      QApplication.processEvents()          # a first-time render takes seconds
    try:
      chroma, grid, path = follow.reference_for(self.cfg, self.piece, source=reference)
    except Exception as e:                  # no map, no audio, no cache: all the same
      self.follow_status.setText('could not listen: %s' % e)
      return False
    try:
      if source is None:
        source = self._follow_source_for(kind, path)
      self._follower = follow.OnlineFollower(chroma, grid, feature=follow.DEFAULT_FEATURE)
      # Somebody practising bars 40-43 does not start at bar 1, and the follower's band
      # only extends forwards, so the passage start is where it is told to look.
      self._follower.reset(grid.start_of(self.first_bar) or 0.0)
      source.start()
    except (capture.CaptureError, follow.FollowError) as e:
      self._follower = None
      if self.follow_status is not None:
        self.follow_status.setText(str(e))
      return False
    self._source = source
    self._following = True
    self._follow_timer.start()
    # What is being listened to cannot change mid-listen: the reference and the audio
    # are matched by the choice, so it is made again by stopping and starting.
    if self.follow_source is not None:
      self.follow_source.setEnabled(False)
    if self.follow_device is not None:
      self.follow_device.setEnabled(False)
    if self.follow_status is not None:
      self.follow_status.setText('listening to %s' % source.describe())
    return True

  def _follow_source_for(self, kind: str, reference_path) -> capture.AudioSource:
    """The source the controls describe: a microphone, or a file replayed at speed."""
    if kind == MIC:
      device = self.follow_device.currentData() if self.follow_device is not None else None
      return capture.MicSource(device=device)
    if kind == TRACK:
      return capture.FileSource(reference_path)   # what the reference was built from
    if self.follow_file is None:
      raise capture.CaptureError('no file chosen to follow')
    return capture.FileSource(self.follow_file)

  def _follow_tick(self) -> None:
    """One timer tick: hand over the audio, take the position, reveal the page.

    The reveal is the same `_refresh` the player drives, so page turns, the line view,
    the curtain and the dials all behave exactly as they do when the clock is the
    recording's -- the only difference is where the clock reading comes from.
    """
    if not self._following or self._source is None or self._follower is None:
      return
    position = None
    for _ in range(FOLLOW_DRAIN):
      block = self._source.read()
      if block is None:
        break
      position = self._follower.feed(block)
    if position is None:
      position = self._follower.position()
    self._seconds = position.reference_s
    self._refresh(position.reference_s)
    if self.follow_status is not None:
      self.follow_status.setText(self._follow_note(position))
    if self._source.error:
      self.stop_following(self._source.error)
    elif position.bar > self.last_bar:
      self.stop_following('the page has reached the end of the passage')

  def _follow_note(self, position: follow.FollowPosition) -> str:
    """What is being listened to, where it thinks it is, and how sure it is.

    The certainty is reported rather than acted on: it is low on silence and on
    unpitched noise, and it is not always low where the position is wrong, so a page
    that slips is better explained by the two dials than by a refusal to move.
    """
    bits = [self._source.describe() if self._source is not None else 'listening',
            'bar %d · beat %d' % (position.bar, position.beat),
            '%.1f s in' % position.reference_s,
            'certainty %.2f' % position.quality]
    if position.unsure:
      bits.append('not sure of this bar')
    return ' · '.join(bits)

  def stop_following(self, note: str = '') -> None:
    """Stop listening; the page stays where it was revealed to."""
    self._follow_timer.stop()
    source, self._source = self._source, None
    if source is not None:
      source.stop()
    self._follower = None
    self._following = False
    if self.follow_source is not None:
      self.follow_source.setEnabled(True)
    if self.follow_device is not None:
      self.follow_device.setEnabled(True)
    if self.follow_check is not None and self.follow_check.isChecked():
      self.follow_check.blockSignals(True)
      self.follow_check.setChecked(False)
      self.follow_check.blockSignals(False)
    if self.follow_status is not None:
      self.follow_status.setText(note or 'not listening')
    self._update_ready()

  # ------------------------------------------------------------------ content

  def set_piece(self, piece: Optional[library.Piece], render: Optional[ScoreRender],
                result: Optional[syncmap.SyncResult]) -> None:
    """Hand over the piece, its engraving and its map; this is all it needs."""
    if self._following:                       # a new piece is not what was being heard
      self.stop_following()
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
    if self.cfg.follow_enabled:
      lines.append('Or tick “listen to me” and the page is revealed from what is heard '
                   'instead: your piano through the microphone (followed against the '
                   'recording), or a file fed in at speed. The dials still work, and '
                   '■ stops listening.')
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
    if self._following:
      self.stop_following()
    self._playing = False
    self.player.stop()
    self.view.set_playhead_measure(None)
    self.view.set_curtain([])                 # uncovered again, for browsing
    self._update_ready()

  def on_position(self, seconds: float) -> None:
    """The clock moved: uncover exactly as far as it has got.

    While listening, this is the recording's clock -- and the recording being played
    is not necessarily being heard (though it may be, through the speakers). The
    follower's reading is then the one that means something, so this does nothing.
    """
    if not self._playing or self._following:
      return
    self._refresh(seconds)

  def on_finished(self) -> None:
    if not self._playing or self._following:
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
    if not (self._playing or self._following):
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
    """Leaving the tab: keep playing, but stop covering the page -- and stop listening.

    A microphone left open is not a small thing, so this is where it is closed whether
    the transport was running or not.
    """
    if self._following:
      self.stop_following()
    self._playing = False
    self.view.set_curtain([])
