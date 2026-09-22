"""The analyzer window.

The layout follows the order the work is actually done in, top to bottom:

  1. pick a piece (or open a score),
  2. give it a recording -- a YouTube link, fetched once and cached,
  3. line the measures up with that recording, and correct them by dragging,
  4. play any range of measures.

The measure list on the left is the piece's *performance order*, which is not the
same as its printed order whenever the score has repeats; a bar played twice
appears twice, and the second appearance is labelled as such.
"""

from __future__ import annotations

import pathlib
from typing import List, Optional

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices, QKeySequence
from PyQt5.QtWidgets import (QAction, QCheckBox, QComboBox, QFileDialog, QFormLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMainWindow, QMessageBox, QProgressBar,
                             QPushButton, QScrollArea, QSpinBox, QSplitter, QTabWidget,
                             QVBoxLayout, QWidget)

from .. import library, measures, scoreview, syncmap
from ..config import AnalyzerConfig
from ..schema import MeasureTime, SyncResult
from .player import RangePlayer
from .score_widget import ScoreView
from .waveform import WaveformView
from .workers import AlignWorker, DownloadWorker, RenderWorker

# Starting exactly on the downbeat clips the piano's attack, and stopping exactly
# at the barline cuts the final resonance; a little air on both sides is what
# makes a passage sound like music rather than a splice.
PRE_ROLL_S = 0.12
POST_ROLL_S = 0.25


class MainWindow(QMainWindow):

  def __init__(self, cfg: Optional[AnalyzerConfig] = None, piece_slug: str = ''):
    super().__init__()
    self.cfg = cfg or AnalyzerConfig()
    self.cfg.ensure_dirs()

    self.piece: Optional[library.Piece] = None
    self.expansion: Optional[measures.Expansion] = None
    self.result: Optional[SyncResult] = None
    self.stored: Optional[syncmap.StoredSync] = None
    self.dirty = False
    self._download: Optional[DownloadWorker] = None
    self._align: Optional[AlignWorker] = None
    self._render: Optional[RenderWorker] = None
    self._loop = False

    self.setWindowTitle('Analyzer — align measures with a recording')
    self.resize(1180, 720)

    self.player = RangePlayer(self)
    self.player.positionChanged.connect(self._on_position)
    self.player.finished.connect(self._on_playback_finished)
    self.player.errorOccurred.connect(self._on_player_error)

    self._build_ui()
    self._build_menus()
    self.refresh_pieces(select=piece_slug)

  # ------------------------------------------------------------------ building

  def _build_ui(self) -> None:
    central = QWidget(self)
    self.setCentralWidget(central)
    outer = QVBoxLayout(central)

    outer.addWidget(self._piece_row())
    outer.addWidget(self._recording_row())

    splitter = QSplitter(Qt.Horizontal)
    splitter.addWidget(self._measure_list_panel())

    self.tabs = QTabWidget()
    self.tabs.addTab(self._waveform_panel(), 'Recording')
    self.tabs.addTab(self._score_panel(), 'Score')
    splitter.addWidget(self.tabs)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)
    splitter.setSizes([280, 900])
    outer.addWidget(splitter, 1)

    outer.addWidget(self._sync_row())
    self.status = self.statusBar()
    self.status.showMessage('ready')

  def _piece_row(self) -> QWidget:
    box = QGroupBox('1. Piece')
    row = QHBoxLayout(box)
    self.piece_combo = QComboBox()
    self.piece_combo.setMinimumWidth(320)
    self.piece_combo.currentIndexChanged.connect(self._on_piece_selected)
    row.addWidget(QLabel('piece'))
    row.addWidget(self.piece_combo, 1)

    reload_btn = QPushButton('Reload list')
    reload_btn.clicked.connect(lambda: self.refresh_pieces(self.piece.slug if self.piece else ''))
    row.addWidget(reload_btn)

    open_btn = QPushButton('Open score…')
    open_btn.clicked.connect(self.open_score_dialog)
    row.addWidget(open_btn)

    self.pdf_btn = QPushButton('Open PDF')
    self.pdf_btn.clicked.connect(self.open_pdf)
    row.addWidget(self.pdf_btn)

    self.score_label = QLabel('—')
    self.score_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    row.addWidget(self.score_label, 1)
    return box

  def _recording_row(self) -> QWidget:
    box = QGroupBox('2. Recording')
    row = QHBoxLayout(box)
    self.url_edit = QLineEdit()
    self.url_edit.setPlaceholderText('https://www.youtube.com/watch?v=… (fetched once, then cached)')
    self.url_edit.returnPressed.connect(self.fetch_audio)
    row.addWidget(QLabel('link'))
    row.addWidget(self.url_edit, 1)

    self.fetch_btn = QPushButton('Fetch audio')
    self.fetch_btn.clicked.connect(self.fetch_audio)
    row.addWidget(self.fetch_btn)

    self.refetch_btn = QPushButton('Re-fetch')
    self.refetch_btn.clicked.connect(lambda: self.fetch_audio(force=True))
    row.addWidget(self.refetch_btn)

    self.download_bar = QProgressBar()
    self.download_bar.setMaximumWidth(180)
    self.download_bar.setVisible(False)
    row.addWidget(self.download_bar)

    self.audio_label = QLabel('no recording')
    row.addWidget(self.audio_label, 1)
    return box

  def _measure_list_panel(self) -> QWidget:
    box = QGroupBox('Measures (performance order)')
    layout = QVBoxLayout(box)
    self.measure_list = QListWidget()
    self.measure_list.setSelectionMode(QListWidget.ExtendedSelection)
    self.measure_list.itemSelectionChanged.connect(self._on_measure_selection)
    layout.addWidget(self.measure_list, 1)
    self.expansion_label = QLabel('—')
    self.expansion_label.setWordWrap(True)
    layout.addWidget(self.expansion_label)
    return box

  def _waveform_panel(self) -> QWidget:
    box = QGroupBox('3. Line the bars up with the recording')
    layout = QVBoxLayout(box)

    self.waveform = WaveformView()
    self.waveform.tickMoved.connect(self._on_tick_moved)
    self.waveform.tickClicked.connect(self._on_tick_clicked)
    self.waveform.positionClicked.connect(self._on_position_clicked)
    layout.addWidget(self.waveform, 1)

    transport = QHBoxLayout()
    transport.addWidget(QLabel('from bar'))
    self.first_spin = QSpinBox()
    self.first_spin.setRange(1, 1)
    self.first_spin.valueChanged.connect(self._on_range_changed)
    transport.addWidget(self.first_spin)

    transport.addWidget(QLabel('to bar'))
    self.last_spin = QSpinBox()
    self.last_spin.setRange(1, 1)
    self.last_spin.valueChanged.connect(self._on_range_changed)
    transport.addWidget(self.last_spin)

    self.play_btn = QPushButton('▶ Play range')
    self.play_btn.clicked.connect(self.play_range)
    transport.addWidget(self.play_btn)

    stop_btn = QPushButton('■ Stop')
    stop_btn.clicked.connect(self.player.stop)
    transport.addWidget(stop_btn)

    self.loop_check = QCheckBox('loop')
    self.loop_check.toggled.connect(lambda on: setattr(self, '_loop', on))
    transport.addWidget(self.loop_check)

    self.range_label = QLabel('')
    transport.addWidget(self.range_label, 1)
    layout.addLayout(transport)

    hint = QLabel('drag a green marker to correct it · ← / → nudge the hovered marker '
                  '(hold Shift for fine steps) · space plays the selected range')
    hint.setStyleSheet('color: #7f8c93;')
    hint.setWordWrap(True)
    layout.addWidget(hint)
    return box

  def _score_panel(self) -> QWidget:
    """The engraved score: click a bar to select it, double-click to hear it."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)

    bar = QHBoxLayout()
    self.page_prev = QPushButton('‹ page')
    self.page_prev.clicked.connect(lambda: self.score_view.previous_page())
    bar.addWidget(self.page_prev)

    self.page_label = QLabel('—')
    bar.addWidget(self.page_label)

    self.page_next = QPushButton('page ›')
    self.page_next.clicked.connect(lambda: self.score_view.next_page())
    bar.addWidget(self.page_next)

    bar.addSpacing(12)
    self.zoom_out_btn = QPushButton('−')
    self.zoom_out_btn.setFixedWidth(32)
    self.zoom_out_btn.clicked.connect(lambda: self.score_view.zoom_out())
    bar.addWidget(self.zoom_out_btn)

    self.zoom_label = QLabel('100%')
    bar.addWidget(self.zoom_label)

    self.zoom_in_btn = QPushButton('+')
    self.zoom_in_btn.setFixedWidth(32)
    self.zoom_in_btn.clicked.connect(lambda: self.score_view.zoom_in())
    bar.addWidget(self.zoom_in_btn)

    self.fit_btn = QPushButton('fit width')
    self.fit_btn.clicked.connect(lambda: self.score_view.fit_width(self.score_scroll.viewport().width()))
    bar.addWidget(self.fit_btn)

    self.follow_check = QCheckBox('follow playback')
    self.follow_check.setChecked(True)
    bar.addWidget(self.follow_check)

    bar.addSpacing(12)
    self.rerender_btn = QPushButton('Engrave again')
    self.rerender_btn.setToolTip('Ignore the cached rendering and engrave the MusicXML afresh')
    self.rerender_btn.clicked.connect(lambda: self.render_score(force=True))
    bar.addWidget(self.rerender_btn)

    self.score_status = QLabel('')
    bar.addWidget(self.score_status, 1)
    layout.addLayout(bar)

    self.score_view = ScoreView()
    self.score_view.measureClicked.connect(self._on_score_measure_clicked)
    self.score_view.measureActivated.connect(self._on_score_measure_activated)
    self.score_view.pageChanged.connect(self._on_score_page_changed)

    self.score_scroll = QScrollArea()
    self.score_scroll.setWidget(self.score_view)
    self.score_scroll.setWidgetResizable(True)
    self.score_scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
    layout.addWidget(self.score_scroll, 1)

    hint = QLabel('click a bar to select it · double-click to play it · '
                  'Ctrl+wheel or +/− to zoom · ← / → step bar by bar')
    hint.setStyleSheet('color: #7f8c93;')
    layout.addWidget(hint)
    return box

  def _sync_row(self) -> QWidget:
    box = QGroupBox('4. The map')
    row = QHBoxLayout(box)
    self.estimate_btn = QPushButton('Average-tempo estimate')
    self.estimate_btn.setToolTip('Spread the recording evenly over the bars. A rough starting point.')
    self.estimate_btn.clicked.connect(self.estimate)
    row.addWidget(self.estimate_btn)

    self.align_btn = QPushButton('Align to recording')
    self.align_btn.setToolTip('Fit the bars to what is actually played (chroma + DTW).')
    self.align_btn.clicked.connect(self.align)
    row.addWidget(self.align_btn)

    self.save_btn = QPushButton('Save map')
    self.save_btn.setEnabled(False)
    self.save_btn.clicked.connect(self.save_map)
    row.addWidget(self.save_btn)

    self.map_label = QLabel('no map yet')
    self.map_label.setWordWrap(True)
    row.addWidget(self.map_label, 1)
    return box

  def _build_menus(self) -> None:
    file_menu = self.menuBar().addMenu('&File')
    act = QAction('Open score…', self)
    act.setShortcut(QKeySequence.Open)
    act.triggered.connect(self.open_score_dialog)
    file_menu.addAction(act)

    act = QAction('Open PDF', self)
    act.triggered.connect(self.open_pdf)
    file_menu.addAction(act)

    file_menu.addSeparator()
    act = QAction('Save map', self)
    act.setShortcut(QKeySequence.Save)
    act.triggered.connect(self.save_map)
    file_menu.addAction(act)

    act = QAction('Forget map', self)
    act.triggered.connect(self.forget_map)
    file_menu.addAction(act)

    file_menu.addSeparator()
    act = QAction('Quit', self)
    act.setShortcut(QKeySequence.Quit)
    act.triggered.connect(self.close)
    file_menu.addAction(act)

    view_menu = self.menuBar().addMenu('&View')
    act = QAction('Play range', self)
    act.setShortcut(Qt.Key_Space)
    act.triggered.connect(self.play_range)
    view_menu.addAction(act)
    act = QAction('Stop', self)
    act.triggered.connect(self.player.stop)
    view_menu.addAction(act)

    help_menu = self.menuBar().addMenu('&Help')
    act = QAction('About', self)
    act.triggered.connect(self.show_about)
    help_menu.addAction(act)

  # ------------------------------------------------------------------ pieces

  def refresh_pieces(self, select: str = '') -> None:
    """Reload the piece list and select `select` (or the first piece)."""
    pieces = library.discover(self.cfg)
    self.piece_combo.blockSignals(True)
    self.piece_combo.clear()
    for piece in pieces:
      self.piece_combo.addItem(piece.describe(), piece.slug)
    self.piece_combo.blockSignals(False)

    if not pieces:
      self._set_piece(None)
      self.status.showMessage('no pieces found: build one with '
                              '.venv/bin/python memorization/analyze.py <score.musicxml>')
      return
    index = 0
    if select:
      for i in range(self.piece_combo.count()):
        if self.piece_combo.itemData(i) == select:
          index = i
          break
    self.piece_combo.setCurrentIndex(index)
    self._set_piece(pieces[index])

  def _on_piece_selected(self, index: int) -> None:
    if index < 0:
      return
    slug = self.piece_combo.itemData(index)
    for piece in library.discover(self.cfg):
      if piece.slug == slug:
        self._set_piece(piece)
        return

  def open_score_dialog(self) -> None:
    start = str(self.cfg.sheet_music_dir if self.cfg.sheet_music_dir.is_dir() else pathlib.Path.home())
    path, _ = QFileDialog.getOpenFileName(
        self, 'Open a score', start, 'Scores (*.musicxml *.xml *.mxl);;All files (*)')
    if path:
      self.open_score(pathlib.Path(path))

  def open_score(self, path: pathlib.Path) -> None:
    """Load a score even if it has no question bank yet."""
    try:
      piece = library.piece_from_musicxml(self.cfg, path)
    except Exception as e:
      QMessageBox.warning(self, 'Could not open that score', str(e))
      return
    self._set_piece(piece, from_disk=True)

  def _set_piece(self, piece: Optional[library.Piece], from_disk: bool = False) -> None:
    self.player.stop()
    self.piece = piece
    self.result = None
    self.stored = None
    self.expansion = None
    self.dirty = False
    self.measure_list.clear()
    self.waveform.set_ticks([], [])
    self.waveform.set_selection(None, None)
    self.score_view.set_render(None)
    self.score_status.setText('')
    self.map_label.setText('no map yet')
    self.save_btn.setEnabled(False)

    if piece is None:
      self.score_label.setText('—')
      self.audio_label.setText('no recording')
      self.waveform.set_audio(None)
      self.expansion_label.setText('—')
      self._update_buttons()
      return

    if piece_slug := (piece.slug if from_disk else ''):
      self.status.showMessage('opened %s (not from the piece list)' % piece_slug)

    self.score_label.setText(str(piece.musicxml) if piece.musicxml else 'score missing')
    self.pdf_btn.setEnabled(bool(piece.pdf))

    # measures
    if piece.has_score:
      try:
        self.expansion = measures.expand_measures(piece.musicxml)
      except measures.ScoreError as e:
        self.expansion = None
        self.status.showMessage('could not read the score: %s' % e)
      else:
        self._fill_measure_list()
        last = max((e.number for e in self.expansion.events), default=1)
        self.first_spin.setRange(1, last)
        self.last_spin.setRange(1, last)
        self.last_spin.setValue(min(last, 8))
    else:
      self.expansion_label.setText('no score: nothing to number')

    # recording
    if piece.has_audio:
      self._adopt_audio(piece, load_samples=True)
    else:
      self.waveform.set_audio(None)
      self.audio_label.setText('no recording — paste a link above')
      self.player.stop()

    # stored map
    self.stored = piece.sync
    if self.stored is not None:
      self._apply_result(self.stored.result, source='saved map')
      reason = syncmap.staleness(self.cfg, self.stored,
                                 audio_path=piece.audio.path if piece.audio else None,
                                 musicxml_path=piece.musicxml)
      if reason:
        self.map_label.setText('%s — %s' % (syncmap.summary_line(self.stored), reason))
    elif piece.has_audio:
      self.estimate(auto=True)

    if piece.has_score:
      self.render_score()

    self._update_buttons()

  def _fill_measure_list(self) -> None:
    self.measure_list.clear()
    if self.expansion is None:
      self.expansion_label.setText('—')
      return
    for event in self.expansion.events:
      label = 'bar %-5s' % event.number
      if event.is_repeat_pass:
        label += '  (pass %d)' % event.pass_no
      if event.is_pickup:
        label += '  pickup'
      label += '   #%d' % (event.play_index + 1)
      item = QListWidgetItem(label)
      item.setData(Qt.UserRole, event.play_index)
      item.setData(Qt.UserRole + 1, event.number)
      self.measure_list.addItem(item)
    extra = ''
    if self.expansion.warnings:
      extra = ' · ' + '; '.join(self.expansion.warnings[:2])
    self.expansion_label.setText(self.expansion.summary() + extra)

  # ------------------------------------------------------------------ audio

  def _adopt_audio(self, piece: library.Piece, load_samples: bool) -> None:
    source = piece.audio
    if source is None:
      return
    path = pathlib.Path(source.path)
    ok = self.player.load(path) if load_samples else True
    if ok and load_samples:
      self.waveform.set_audio(self.player.samples, self.player.sample_rate)
    duration = source.duration_s or self.player.duration
    self.audio_label.setText('%s · %.1f s · %d Hz' %
                             (path.name, duration, source.sample_rate or self.player.sample_rate))
    self.waveform.set_playhead(None)

  def fetch_audio(self, force: bool = False) -> None:
    if self.piece is None:
      QMessageBox.information(self, 'No piece', 'Pick a piece first.')
      return
    url = self.url_edit.text().strip()
    if not url:
      QMessageBox.information(self, 'No link', 'Paste a YouTube link first.')
      return
    self._start_download(url, force=force)

  def _start_download(self, url: str, force: bool = False) -> None:
    self.download_bar.setVisible(True)
    self.download_bar.setValue(0)
    self.fetch_btn.setEnabled(False)
    self.status.showMessage('fetching audio…')
    worker = DownloadWorker(self.cfg, self.piece.slug, url, force=force, parent=self)
    worker.progressed.connect(self._on_download_progress)
    worker.fetched.connect(self._on_downloaded)
    worker.failed.connect(self._on_download_failed)
    self._download = worker
    worker.start()

  def _on_download_progress(self, percent: float, message: str) -> None:
    self.download_bar.setValue(int(max(0.0, min(100.0, percent))))
    if message:
      self.status.showMessage('%s (%.0f%%)' % (message, percent))

  def _on_downloaded(self, source) -> None:
    self.download_bar.setVisible(False)
    self.fetch_btn.setEnabled(True)
    if self.piece is not None:
      self.piece.audio = source
      self._adopt_audio(self.piece, load_samples=True)
      self.status.showMessage('fetched %s (%.1f s)' % (pathlib.Path(source.path).name,
                                                       source.duration_s))
      # a new recording invalidates any existing map
      if self.result is None or not self.result.measures:
        self.estimate(auto=True)
      else:
        self.map_label.setText('new recording fetched — align again, or press '
                               'Average-tempo estimate')
    self._update_buttons()

  def _on_download_failed(self, message: str) -> None:
    self.download_bar.setVisible(False)
    self.fetch_btn.setEnabled(True)
    self.status.showMessage('fetch failed')
    QMessageBox.warning(self, 'Could not fetch the audio', message)

  # ------------------------------------------------------------------ the map

  def _align_module(self):
    try:
      from .. import align
      return align
    except ImportError:
      return None

  def played(self) -> List[int]:
    return self.expansion.played_numbers if self.expansion else []

  def estimate(self, auto: bool = False) -> None:
    """Produce a rough map to correct by hand.

    Uses the aligner's own baseline when it is available; otherwise falls back to
    the map layer's uniform division, so the window is never left unable to show
    or play a range just because the alignment module is missing.
    """
    duration = self.player.duration or (self.piece.audio.duration_s if self.piece and self.piece.audio else 0.0)
    if not self.played() or duration <= 0:
      if not auto:
        QMessageBox.information(self, 'Nothing to estimate',
                                'A score and a recording are both needed.')
      return
    align = self._align_module()
    if align is not None:
      result = align.linear_estimate(self.played(), duration)
    else:
      result = syncmap.uniform_map(self.played(), duration)
    self._apply_result(result, source='average-tempo estimate')
    self.map_label.setText('average-tempo estimate: %s. This is a guess — use '
                           '"Align to recording", then drag any bar that is off.'
                           % self._provenance(result))

  def align(self) -> None:
    if self.piece is None or self.expansion is None:
      return
    align = self._align_module()
    if align is None:
      QMessageBox.warning(self, 'Alignment unavailable', 'The alignment module could not be imported.')
      return
    midi = self.piece.reference_midi
    if midi is None:
      QMessageBox.information(
          self, 'No reference performance for this score',
          'Alignment needs a machine-readable performance of the score, which the '
          'question bank provides. Build one first:\n\n'
          '  .venv/bin/python memorization/analyze.py "%s" --name %s\n\n'
          'Until then you can place the bars by hand: press "Average-tempo estimate" '
          'and drag the markers.' % (self.piece.musicxml, self.piece.slug))
      return
    if not self.piece.has_audio:
      QMessageBox.information(self, 'No recording', 'Fetch a recording first.')
      return

    # Re-aligning replaces whatever is on screen, including bars the user has
    # placed by hand -- ask before throwing that work away.
    if self.dirty and self.result is not None and any(
        'hand-corrected' in w for w in self.result.warnings):
      if QMessageBox.question(
          self, 'Replace your corrections?',
          'You have hand-corrected bars. Aligning again will discard those moves. '
          'Continue?') != QMessageBox.Yes:
        return

    self.align_btn.setEnabled(False)
    self.download_bar.setVisible(True)
    self.download_bar.setValue(0)
    self.status.showMessage('aligning…')
    # No baseline: the aligner compares against the average-tempo estimate, which
    # is what `median_abs_change_vs_linear_s` means. Passing the map already on
    # screen made that number report 0.00 s.
    worker = AlignWorker(self.cfg, self.played(), self.piece.audio.path, midi,
                         baseline=None, parent=self)
    worker.progressed.connect(self._on_align_progress)
    worker.aligned.connect(self._on_aligned)
    worker.failed.connect(self._on_align_failed)
    self._align = worker
    worker.start()

  def _on_align_progress(self, percent: float, message: str) -> None:
    self.download_bar.setValue(int(max(0.0, min(100.0, percent))))
    if message:
      self.status.showMessage('%s (%.0f%%)' % (message, percent))

  def _on_aligned(self, result: SyncResult) -> None:
    self.download_bar.setVisible(False)
    self.align_btn.setEnabled(True)
    self._apply_result(result, source='alignment')
    self.status.showMessage('aligned: %s' % self._provenance(result))

  def _on_align_failed(self, message: str) -> None:
    self.download_bar.setVisible(False)
    self.align_btn.setEnabled(True)
    self.status.showMessage('alignment failed')
    QMessageBox.warning(self, 'Could not align', message)

  def _provenance(self, result: SyncResult) -> str:
    bits = ['%s, %.2f s/bar' % (result.method, result.seconds_per_measure)]
    for key, label, fmt in (('normalized_cost', 'match cost', '%.2f'),
                            ('median_abs_change_vs_linear_s', 'moved', '%.2f s'),
                            ('chroma_shift', 'key shift', '%d st')):
      value = result.quality.get(key)
      if value is not None:
        try:
          bits.append('%s %s' % (label, fmt % value))
        except (TypeError, ValueError):
          pass
    return ', '.join(bits)

  def _apply_result(self, result: SyncResult, source: str = '') -> None:
    self.result = result
    ticks = [m.start_s for m in result.measures]
    labels = [str(m.measure) for m in result.measures]
    confidence = [(m.confidence is None or m.confidence >= 0.5) for m in result.measures]
    self.waveform.set_ticks(ticks, labels, confidence)
    self.save_btn.setEnabled(bool(result.measures))

    # Validate whatever we were handed -- produced here, loaded from disk, or
    # hand-edited -- because a map with no-time bars must never look trustworthy.
    problems = syncmap.check_plausibility(result)
    for problem in problems:
      if problem not in result.warnings:
        result.warnings.append(problem)

    if source:
      warning = (' · ' + '; '.join(result.warnings[:2])) if result.warnings else ''
      if self.map_label.text().startswith('average-tempo') and source == 'average-tempo estimate':
        pass
      elif source != 'saved map':
        self.map_label.setText('%s — %s%s' % (source, self._provenance(result), warning))
    if problems:
      self.status.showMessage('⚠ %s' % problems[0])
    self._on_range_changed()
    self._update_buttons()

  def _apply_manual_edit(self) -> None:
    """Recompute the map after the user moves a marker."""
    if self.result is None or not self.result.measures:
      return
    ordered = sorted(self.result.measures, key=lambda m: m.play_index)
    for i, m in enumerate(ordered):
      m.start_s = max(0.0, m.start_s)
      m.end_s = ordered[i + 1].start_s if i + 1 < len(ordered) else max(
          m.start_s, self.player.duration or m.start_s)
    self.result.method = SyncResult.METHOD_MANUAL
    if 'hand-corrected' not in self.result.warnings:
      self.result.warnings.append('hand-corrected: at least one bar was moved by the user')
    self.dirty = True
    self.save_btn.setEnabled(True)

  def _on_tick_moved(self, play_index: int, seconds: float) -> None:
    if self.result is None:
      return
    for m in self.result.measures:
      if m.play_index == play_index:
        m.start_s = seconds
        m.confidence = 1.0
        break
    self._apply_manual_edit()
    self.waveform.set_ticks([m.start_s for m in self.result.measures],
                            [str(m.measure) for m in self.result.measures],
                            [True] * len(self.result.measures))
    self.map_label.setText('hand-corrected — bar %s moved to %.2f s (not saved yet)'
                           % (self._measure_of(play_index), seconds))
    self._on_range_changed()

  def _measure_of(self, play_index: int):
    for m in self.result.measures:
      if m.play_index == play_index:
        return m.measure
    return '?'

  def _on_tick_clicked(self, play_index: int) -> None:
    self._select_play_index(play_index)

  def save_map(self) -> None:
    if self.piece is None or self.result is None or not self.result.measures:
      return
    path = syncmap.save(self.cfg, self.piece.slug, self.result,
                        audio=self.piece.audio, source_musicxml=self.piece.musicxml,
                        played=self.played())
    self.dirty = False
    self.stored = syncmap.load(self.cfg, self.piece.slug)
    self.status.showMessage('saved %s' % path)
    self.map_label.setText('saved: %s' % self._provenance(self.result))

  def forget_map(self) -> None:
    if self.piece is None:
      return
    if self.result is None:
      return
    if QMessageBox.question(self, 'Forget this map?',
                            'Delete the saved measure map for %s?' % self.piece.display_name) \
        != QMessageBox.Yes:
      return
    syncmap.delete(self.cfg, self.piece.slug)
    self.result = None
    self.stored = None
    self.waveform.set_ticks([], [])
    self.waveform.set_selection(None, None)
    self.map_label.setText('no map yet')
    self.save_btn.setEnabled(False)

  # ------------------------------------------------------------------ transport

  def _on_range_changed(self) -> None:
    self._sync_score_selection()
    if self.result is not None and self.result.measures:
      span = self.result.range_for_measures(self.first_spin.value(), self.last_spin.value())
      if span:
        self.waveform.set_selection(*span)
        self.range_label.setText('%.2f s → %.2f s (%.2f s)' % (span[0], span[1], span[1] - span[0]))
        return
    self.waveform.set_selection(None, None)
    self.range_label.setText('')

  def _on_measure_selection(self) -> None:
    items = self.measure_list.selectedItems()
    if not items:
      return
    numbers = [it.data(Qt.UserRole + 1) for it in items]
    index = items[0].data(Qt.UserRole)
    self.first_spin.setValue(min(numbers))
    self.wrong = None
    self.last_spin.setValue(max(numbers))
    if self.result is not None:
      span = self.result.range_for_measures(min(numbers), max(numbers))
      if span:
        self.waveform.set_selection(*span)
    self.show_measure_in_score(min(numbers))
    self.waveform.set_playhead(self.waveform.ticks()[index] if index < len(self.waveform.ticks()) else None)
    self._on_range_changed()

  def _select_play_index(self, play_index: int) -> None:
    if 0 <= play_index < self.measure_list.count():
      self.measure_list.setCurrentRow(play_index)
      self.measure_list.scrollToItem(self.measure_list.item(play_index))

  def _on_position_clicked(self, seconds: float) -> None:
    self.waveform.set_playhead(seconds)

  def play_range(self) -> None:
    if self.result is None or not self.result.measures:
      QMessageBox.information(self, 'No map yet', 'Line the bars up first '
                              '(Average-tempo estimate or Align to recording).')
      return
    span = self.result.range_for_measures(self.first_spin.value(), self.last_spin.value())
    if span is None:
      QMessageBox.information(self, 'Unknown bars',
                              'The map has nothing for bars %d-%d.'
                              % (self.first_spin.value(), self.last_spin.value()))
      return
    duration = self.player.duration
    start = max(0.0, span[0] - PRE_ROLL_S)
    end = min(duration, span[1] + POST_ROLL_S) if duration else span[1] + POST_ROLL_S
    if not self.player.play_range(start, end):
      self.status.showMessage('could not play that range')
      return
    self.status.showMessage('playing bars %d-%d' % (self.first_spin.value(), self.last_spin.value()))

  def _on_position(self, seconds: float) -> None:
    self.waveform.set_playhead(seconds)
    measure = self._measure_at_time(seconds)
    if measure is not None:
      self.score_view.set_playhead_measure(measure)
      if self.follow_check.isChecked() and not self._measure_on_shown_page(measure):
        self.score_view.show_measure(measure)

  def _measure_on_shown_page(self, measure: int) -> bool:
    page = self.score_view.page()
    return bool(page and any(b.number == measure for b in page.measures))

  def _on_playback_finished(self) -> None:
    self.waveform.set_playhead(None)
    if self._loop and self.result is not None and self.result.measures:
      self.play_range()

  def _on_player_error(self, message: str) -> None:
    self.status.showMessage(message)

  # ------------------------------------------------------------------ misc

  # ------------------------------------------------------------------ score

  def render_score(self, force: bool = False) -> None:
    """Engrave the score into page images, off the UI thread.

    The first render of a 200-bar piece takes about fifteen seconds and is then
    cached next to the piece, so this is a worker with progress rather than a
    blocking call.
    """
    if self.piece is None or not self.piece.has_score:
      self.score_status.setText('no score to engrave')
      self.score_view.set_render(None)
      return
    if self._render is not None and self._render.isRunning():
      return
    self.score_status.setText('engraving…')
    self.rerender_btn.setEnabled(False)
    worker = RenderWorker(self.cfg, self.piece.musicxml, force=force, parent=self)
    worker.progressed.connect(self._on_render_progress)
    worker.rendered.connect(self._on_rendered)
    worker.failed.connect(self._on_render_failed)
    self._render = worker
    worker.start()

  def _on_render_progress(self, percent: float, message: str) -> None:
    if message:
      self.score_status.setText('%s (%.0f%%)' % (message, percent))

  def _on_rendered(self, render) -> None:
    self.rerender_btn.setEnabled(True)
    self.score_view.set_render(render)
    pages = len(render.pages)
    measures = render.measure_count
    note = ''
    if render.warnings:
      note = ' · ' + '; '.join(render.warnings[:1])
    self.score_status.setText('%d page(s), %d measures clickable%s' % (pages, measures, note))
    self._on_score_page_changed(self.score_view.page_number)
    self._sync_score_selection()

  def _on_render_failed(self, message: str) -> None:
    self.rerender_btn.setEnabled(True)
    self.score_status.setText('score not engraved')
    self.status.showMessage(message)
    QMessageBox.warning(self, 'Could not engrave the score', message)

  def _on_score_page_changed(self, number: int) -> None:
    total = len(self.score_view.render.pages) if self.score_view.render else 0
    self.page_label.setText('page %d / %d' % (number, total) if total else '—')
    self.zoom_label.setText('%d%%' % round(100 * self.score_view.zoom()))

  def _on_score_measure_clicked(self, measure: int) -> None:
    self.first_spin.setValue(measure)
    self.last_spin.setValue(measure)
    self.status.showMessage('bar %d selected — press Play range to hear it' % measure)

  def _on_score_measure_activated(self, measure: int) -> None:
    self._on_score_measure_clicked(measure)
    self.play_range()

  def _sync_score_selection(self) -> None:
    """Mirror the current range onto the score page."""
    if self.result is None or not self.result.measures:
      self.score_view.set_selection(None, None)
      return
    self.score_view.set_selection(self.first_spin.value(), self.last_spin.value())

  def show_measure_in_score(self, measure: int) -> None:
    if self.score_view.render is not None:
      self.score_view.show_measure(measure)

  def _measure_at_time(self, seconds: float):
    """Which measure the recording is inside at `seconds`."""
    if self.result is None or not self.result.measures:
      return None
    for m in self.result.measures:
      if m.start_s <= seconds < m.end_s:
        return m.measure
    return None

  def _update_buttons(self) -> None:
    has_score = bool(self.expansion and self.expansion.events)
    has_audio = bool(self.piece and self.piece.has_audio)
    self.estimate_btn.setEnabled(has_score and has_audio)
    self.align_btn.setEnabled(has_score and has_audio)
    self.play_btn.setEnabled(bool(self.result and self.result.measures))
    for widget in (self.rerender_btn, self.page_prev, self.page_next,
                   self.zoom_in_btn, self.zoom_out_btn, self.fit_btn):
      widget.setEnabled(has_score)

  def open_pdf(self) -> None:
    if self.piece and self.piece.pdf:
      QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.piece.pdf)))
    else:
      QMessageBox.information(self, 'No PDF', 'No PDF sits next to this score.')

  def show_about(self) -> None:
    QMessageBox.about(
        self, 'About the analyzer',
        '<b>Analyzer</b><p>Maps the measures of a score onto a recording, so you can '
        'play any range of bars.</p>'
        '<p>The score (%s) is authoritative; the recording is cached under '
        '<code>analyzer/cache</code>; the map is stored next to it as JSON.</p>'
        % pathlib.Path(__file__).name)

  def closeEvent(self, event):
    if self.dirty:
      answer = QMessageBox.question(self, 'Unsaved map',
                                    'The measure map has unsaved changes. Save before closing?')
      if answer == QMessageBox.Yes:
        self.save_map()
    self.player.stop()
    for worker in (self._download, self._align, self._render):
      if worker is not None and worker.isRunning():
        worker.wait(2000)
    super().closeEvent(event)
