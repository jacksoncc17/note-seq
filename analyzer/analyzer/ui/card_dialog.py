"""The Anki card dialog: choose what to hide, then export a deck.

Desktop-only on purpose. Authoring a card means looking at the score, deciding
which notes a student should have to recall, and checking the result -- work that
wants a big screen and a mouse. The phone's job is to study the cards afterwards,
in AnkiDroid, where the scheduling lives.

The dialog does not draw anything itself: the preview is built by `cards.py`, the
same code that builds the exported cards, so what is shown is what will be
exported rather than a second implementation that can drift.
"""

from __future__ import annotations

import pathlib
from typing import Dict, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QDialogButtonBox,
                             QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QPushButton, QRadioButton,
                             QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from .. import cards, library, store
from ..config import AnalyzerConfig
from ..scoreview import ScoreRender


class CardDialog(QDialog):
  """Build one card for a bar range and export it, or a deck of many."""

  def __init__(self, cfg: AnalyzerConfig, piece: library.Piece, render: ScoreRender,
               parent=None, first_bar: int = 1, last_bar: int = 1):
    super().__init__(parent)
    self.cfg = cfg
    self.piece = piece
    self.render = render
    self.repo = store.open_repository(cfg)
    self.masked: List[str] = []
    self.fingering: Dict[str, str] = {}
    self.digit = '1'
    self.undo_stack: List[tuple] = []
    self.pointed = 0                       # index into the passage's notes
    self.built: Optional[cards.BuiltCard] = None
    self.exported: List[pathlib.Path] = []

    self.setWindowTitle('Anki cards for %s' % piece.display_name)
    self.resize(1100, 780)
    self._glyphs = render.glyphs_in_measures(first_bar, last_bar)

    layout = QHBoxLayout(self)
    layout.addWidget(self._left_column(first_bar, last_bar), 0)
    layout.addWidget(self._right_column(), 1)
    self.question_edit.setText(self._suggested_question())
    self._preview()

  # ------------------------------------------------------------------ widgets

  def _left_column(self, first_bar: int, last_bar: int) -> QWidget:
    column = QWidget()
    box = QVBoxLayout(column)

    span = QGroupBox('Passage')
    form = QFormLayout(span)
    self.first_spin = QSpinBox()
    self.last_spin = QSpinBox()
    for spin in (self.first_spin, self.last_spin):
      spin.setRange(1, max(1, self.render.measure_count or 1))
    self.first_spin.setValue(min(first_bar, last_bar))
    self.last_spin.setValue(max(first_bar, last_bar))
    self.first_spin.valueChanged.connect(self._range_changed)
    self.last_spin.valueChanged.connect(self._range_changed)
    form.addRow('first bar', self.first_spin)
    form.addRow('last bar', self.last_spin)
    self.title_edit = QLineEdit()
    self.title_edit.setPlaceholderText('optional label for this card')
    form.addRow('label', self.title_edit)
    box.addWidget(span)

    rules = QGroupBox('Hide (rules)')
    rules_layout = QVBoxLayout(rules)
    self.rule_boxes: Dict[str, QCheckBox] = {}
    for name, description in sorted(cards.MASK_RULES.items()):
      check = QCheckBox('%s — %s' % (name, description))
      check.stateChanged.connect(self._preview)
      self.rule_boxes[name] = check
      rules_layout.addWidget(check)
    box.addWidget(rules)

    tapped = QGroupBox('Hide (tapped notes)')
    tapped_layout = QVBoxLayout(tapped)
    self.tap_check = QCheckBox('tap notes on the score to hide them')
    tapped_layout.addWidget(self.tap_check)
    self.tapped_label = QLabel('none')
    self.tapped_label.setWordWrap(True)
    tapped_layout.addWidget(self.tapped_label)
    clear = QPushButton('clear tapped notes')
    clear.clicked.connect(self._clear_tapped)
    tapped_layout.addWidget(clear)
    box.addWidget(tapped)

    question = QGroupBox('Question')
    question_layout = QVBoxLayout(question)
    self.question_edit = QLineEdit()
    self.question_edit.setPlaceholderText('asked on the front, e.g. what is the chord in bar 53?')
    self.question_edit.textChanged.connect(self._preview)
    question_layout.addWidget(self.question_edit)

    spoken = QHBoxLayout()
    self.speak_check = QCheckBox('speak it')
    self.speak_check.setChecked(True)
    self.speak_check.stateChanged.connect(self._preview)
    spoken.addWidget(self.speak_check)
    spoken.addWidget(QLabel('voice'))
    self.voice_edit = QLineEdit(getattr(self.cfg, 'tts_voice', '') or 'af_heart')
    self.voice_edit.setMaximumWidth(120)
    spoken.addWidget(self.voice_edit)
    suggest = QPushButton('suggest')
    suggest.setToolTip('take a question about these bars from the question bank')
    suggest.clicked.connect(self.suggest_question)
    spoken.addWidget(suggest)
    spoken.addStretch(1)
    question_layout.addLayout(spoken)
    box.addWidget(question)

    fingering = QGroupBox('Fingering')
    fingering_layout = QVBoxLayout(fingering)
    self.fingering_check = QCheckBox('tap notes to set the fingering')
    self.fingering_check.stateChanged.connect(self._fingering_mode_changed)
    fingering_layout.addWidget(self.fingering_check)

    digits = QHBoxLayout()
    self.digit_group = QButtonGroup(self)
    for digit in cards.FINGERING_DIGITS:
      button = QRadioButton(digit)
      button.setChecked(digit == '1')
      button.toggled.connect(lambda on, d=digit: self._digit_chosen(d) if on else None)
      self.digit_group.addButton(button)
      digits.addWidget(button)
    digits.addStretch(1)
    fingering_layout.addLayout(digits)

    step = QHBoxLayout()
    step.addWidget(QLabel('note'))
    self.note_prev = QPushButton('◀')
    self.note_prev.setFixedWidth(36)
    self.note_prev.clicked.connect(lambda: self.step_note(-1))
    step.addWidget(self.note_prev)
    self.note_next = QPushButton('▶')
    self.note_next.setFixedWidth(36)
    self.note_next.clicked.connect(lambda: self.step_note(1))
    step.addWidget(self.note_next)
    self.note_label = QLabel('—')
    step.addWidget(self.note_label, 1)
    fingering_layout.addLayout(step)

    self.fingering_label = QLabel('none')
    self.fingering_label.setWordWrap(True)
    fingering_layout.addWidget(self.fingering_label)
    row = QHBoxLayout()
    assign = QPushButton('assign to this note')
    assign.clicked.connect(self.assign_to_pointed)
    row.addWidget(assign)
    undo = QPushButton('undo last tap')
    undo.clicked.connect(self.undo)
    row.addWidget(undo)
    clear_fingering = QPushButton('clear fingerings')
    clear_fingering.clicked.connect(self.clear_fingerings)
    row.addWidget(clear_fingering)
    fingering_layout.addLayout(row)
    box.addWidget(fingering)

    sound = QGroupBox('Sound')
    sound_layout = QVBoxLayout(sound)
    self.audio_check = QCheckBox('include the recording of these bars')
    self.audio_check.setChecked(True)
    self.audio_check.stateChanged.connect(self._preview)
    sound_layout.addWidget(self.audio_check)
    self.audio_side = QLineEdit('back')
    self.audio_side.setMaximumWidth(90)
    sound_layout.addWidget(QLabel('on the card:'))
    sound_layout.addWidget(self.audio_side)
    box.addWidget(sound)

    tags = QGroupBox('Tags')
    tags_layout = QVBoxLayout(tags)
    self.tags_edit = QLineEdit()
    self.tags_edit.setPlaceholderText('comma separated, e.g. exam, section-B')
    tags_layout.addWidget(self.tags_edit)
    box.addWidget(tags)

    box.addStretch(1)
    return column

  def _right_column(self) -> QWidget:
    column = QWidget()
    box = QVBoxLayout(column)

    self.summary = QLabel('')
    self.summary.setWordWrap(True)
    box.addWidget(self.summary)

    preview = QGroupBox('Front and back, as they will be exported')
    preview_layout = QHBoxLayout(preview)
    for name in ('front', 'back'):
      holder = QVBoxLayout()
      holder.addWidget(QLabel(name))
      image = QLabel('')
      image.setMinimumSize(300, 240)
      image.setAlignment(Qt.AlignCenter)
      image.setStyleSheet('background: #f6f4ef; border: 1px solid #ccc;')
      setattr(self, '%s_image' % name, image)
      holder.addWidget(image)
      preview_layout.addLayout(holder)
    box.addWidget(preview, 1)

    self.fields_label = QLabel('')
    self.fields_label.setWordWrap(True)
    self.fields_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    box.addWidget(self.fields_label)

    buttons = QDialogButtonBox()
    self.export_button = buttons.addButton('Export .apkg…', QDialogButtonBox.ActionRole)
    self.export_button.clicked.connect(self.export_deck)
    self.save_button = buttons.addButton('Save to the phone library',
                                         QDialogButtonBox.ActionRole)
    self.save_button.clicked.connect(self.save_for_phone)
    self.close_button = buttons.addButton('Close', QDialogButtonBox.RejectRole)
    self.close_button.clicked.connect(self.reject)
    box.addWidget(buttons)
    return column

  # ------------------------------------------------------------------ state

  def spec(self) -> cards.CardSpec:
    rules = [name for name, check in self.rule_boxes.items() if check.isChecked()]
    tags = [t.strip() for t in self.tags_edit.text().split(',') if t.strip()]
    return cards.CardSpec(
        slug=self.piece.slug, first_bar=self.first_spin.value(),
        last_bar=self.last_spin.value(), title=self.title_edit.text().strip(),
        masks=list(self.masked), rules=rules, tags=tags,
        fingering={k: v for k, v in self.fingering.items() if v},
        question=self.question_edit.text().strip(),
        speak_question=self.speak_check.isChecked(),
        voice=self.voice_edit.text().strip(),
        audio_side=self.audio_side.text().strip() if self.audio_check.isChecked() else 'none')

  def _range_changed(self) -> None:
    self.masked = []
    self._glyphs = self.render.glyphs_in_measures(self.first_spin.value(),
                                                  self.last_spin.value())
    self._preview()

  def _fingering_mode_changed(self, state) -> None:
    """Tapping means one thing at a time: masking notes, or setting a fingering."""
    if self.fingering_check.isChecked() and self.tap_check.isChecked():
      self.tap_check.setChecked(False)
    self._pointed_changed()

  def _suggested_question(self) -> str:
    """What the question field starts as, and what "unchanged" means."""
    return cards.suggest_question(self.piece, self.first_spin.value(), self.last_spin.value())

  def question_changed(self) -> bool:
    """Whether the question is the user's own, rather than the offered default.

    A card whose only content is the suggested question is the card the passage
    would make anyway, so it does not by itself count as work; an empty field is
    no question at all.
    """
    text = self.question_edit.text().strip()
    return bool(text) and text != self._suggested_question().strip()

  def suggest_question(self) -> None:
    self.question_edit.setText(self._suggested_question())

  def _digit_chosen(self, digit: str) -> None:
    self.digit = digit

  def step_note(self, delta: int) -> None:
    """Walk the passage note by note, for when the mouse is fiddly."""
    notes = self._notes()
    if not notes:
      return
    self.pointed = max(0, min(len(notes) - 1, self.pointed + delta))
    self._pointed_changed()

  def _notes(self):
    return [g for g in self._glyphs if g.kind == 'note' and g.has_hit]

  def _pointed_changed(self) -> None:
    notes = self._notes()
    if not notes:
      self.note_label.setText('no notes in this passage')
      return
    glyph = notes[min(self.pointed, len(notes) - 1)]
    self.note_label.setText('%s  (bar %d, %s hand, note %d)%s'
                            % (glyph.key, glyph.measure, 'right' if glyph.staff == 1 else 'left',
                               glyph.index + 1,
                               '  → %s' % self.fingering.get(glyph.key, '')
                               if glyph.key in self.fingering else ''))
    if self.parent() is not None and hasattr(self.parent(), 'score_view'):
      self.parent().score_view.set_hover_glyph(glyph.key)

  def pointed_key(self) -> Optional[str]:
    notes = self._notes()
    return notes[min(self.pointed, len(notes) - 1)].key if notes else None

  def assign_to_pointed(self) -> None:
    key = self.pointed_key()
    if key:
      self.set_fingering(key, self.digit)

  def set_fingering(self, key: str, digit: str) -> None:
    previous = self.fingering.get(key)
    self.fingering[key] = digit
    self.undo_stack.append(('fingering', key, previous))
    self._preview()

  def undo(self) -> None:
    """Undo the last tap or fingering, whichever came last."""
    if not self.undo_stack:
      self.status_message('nothing to undo')
      return
    kind, key, previous = self.undo_stack.pop()
    if kind == 'fingering':
      if previous is None:
        self.fingering.pop(key, None)
      else:
        self.fingering[key] = previous
    else:
      if key in self.masked:
        self.masked.remove(key)
      else:
        self.masked.append(key)
    self._preview()

  def clear_fingerings(self) -> None:
    for key in list(self.fingering):
      self.undo_stack.append(('fingering', key, self.fingering[key]))
    self.fingering = {}
    self._preview()

  def status_message(self, text: str) -> None:
    self.summary.setText(text)

  def toggle_tapped(self, key: str) -> None:
    """A note was tapped: mask it, or give it the chosen fingering."""
    if self.fingering_check.isChecked():
      if self.fingering.get(key) == self.digit:
        self.fingering.pop(key, None)          # tapping the same digit clears it
      else:
        self.fingering[key] = self.digit
      self.undo_stack.append(('fingering', key, None))
    else:
      if key in self.masked:
        self.masked.remove(key)
      else:
        self.masked.append(key)
      self.undo_stack.append(('mask', key, None))
    self._preview()

  def _clear_tapped(self) -> None:
    self.masked = []
    self._preview()

  def toggle_note(self, key: str) -> None:
    """A note was tapped on the score."""
    if self.fingering_check.isChecked():
      self.toggle_tapped(key)
      return
    if not self.tap_check.isChecked():
      self.tap_check.setChecked(True)
    self.toggle_tapped(key)

  def masked_keys(self) -> List[str]:
    return cards.masked_keys(self.spec(), self._glyphs)

  # ------------------------------------------------------------------ preview

  def _preview(self) -> None:
    """Rebuild both sides with the same code that will export them."""
    spec = self.spec()
    try:
      hidden = cards.masked_keys(spec, self._glyphs)
    except cards.CardError as e:
      self.summary.setText('masking problem: %s' % e)
      return
    self.tapped_label.setText(', '.join(self.masked) if self.masked else 'none')
    if self.fingering:
      self.fingering_label.setText(' '.join(
          '%s:%s' % (key, value) for key, value in sorted(self.fingering.items())))
    else:
      self.fingering_label.setText('none')
    try:
      front = cards.passage_image(self.cfg, self.piece, self.render, spec, hidden,
                                 scale=1.2, hide=True)
      back = cards.passage_image(self.cfg, self.piece, self.render, spec, hidden,
                                scale=1.2, hide=False)
    except cards.CardError as e:
      self.summary.setText('cannot build this card: %s' % e)
      return
    for name, image in (('front', front), ('back', back)):
      import io
      buffer = io.BytesIO()
      image.save(buffer, format='PNG')
      pixmap = QPixmap()
      pixmap.loadFromData(buffer.getvalue(), 'PNG')
      target = getattr(self, '%s_image' % name)
      target.setPixmap(pixmap.scaled(target.width() or 320, 400, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation))

    fields = cards.memorisation_fields(self.piece, spec.first_bar, spec.last_bar)
    self.fields_label.setText('back of the card: ' + ' · '.join(
        '%s: %s' % (key, value) for key, value in fields.items() if value))
    hidden_notes = [k for k in hidden if ':note:' in k]
    spoken = ''
    if spec.question.strip() and spec.speak_question:
      spoken = ' · spoken with %s' % (spec.voice or getattr(self.cfg, 'tts_voice', 'the default voice'))
    self.summary.setText(
        'bars %d-%d · %d notes · hiding %d (%s) · fingerings %d%s'
        % (spec.bar_range[0], spec.bar_range[1], len(self._glyphs), len(hidden),
           ', '.join(sorted(spec.rules)) or 'none by rule', len(self.fingering), spoken))
    self._pointed_changed()
    has_work = bool(hidden) or bool(self.fingering) or self.question_changed()
    self.export_button.setEnabled(has_work)
    self.save_button.setEnabled(has_work)

  # ------------------------------------------------------------------ output

  def _deck_name(self) -> str:
    return self.title_edit.text().strip() or (
        'bars %d-%d' % (self.first_spin.value(), self.last_spin.value()))

  def remember_fingerings(self) -> None:
    """Keep the fingerings with the piece: enter them once, reuse on other cards."""
    record = getattr(self.piece, 'record', None)
    if record is None or not self.fingering:
      return
    merged = dict(record.fingerings or {})
    merged.update(self.fingering)
    record.fingerings = merged
    self.repo.pieces.upsert(record)
    self.piece.record = self.repo.pieces.get(record.slug) or record

  def export_deck(self) -> None:
    """Build the deck and offer to save the .apkg."""
    deck = cards.DeckSpec(slug=self.piece.slug, name=self._deck_name(),
                          cards=[self.spec()])
    try:
      import tempfile
      package = cards.build_deck(self.cfg, self.piece, deck,
                                 workspace=pathlib.Path(tempfile.mkdtemp(prefix='cards-')))
    except cards.CardError as e:
      QMessageBox.warning(self, 'Could not build the deck', str(e))
      return
    target, _filter = QFileDialog.getSaveFileName(
        self, 'Save the deck', str(pathlib.Path.home() / ('%s.apkg' % package.stem)),
        'Anki deck (*.apkg)')
    if not target:
      return
    pathlib.Path(target).write_bytes(package.read_bytes())
    self.exported.append(pathlib.Path(target))
    for note in package.parent.glob('*.warnings.txt'):
      QMessageBox.information(self, 'Deck exported with warnings',
                              note.read_text(encoding='utf-8')[:600])
    QMessageBox.information(
        self, 'Deck exported',
        'Wrote %s.\n\nImport it into AnkiDroid (or Anki desktop): Anki schedules\n'
        'the cards from then on.' % target)

  def save_for_phone(self) -> None:
    """Keep the deck beside the piece and package it, so the phone can fetch it."""
    deck = cards.DeckSpec(slug=self.piece.slug, name=self._deck_name(),
                          cards=[self.spec()])
    try:
      import tempfile
      package = cards.build_deck(self.cfg, self.piece, deck,
                                 workspace=pathlib.Path(tempfile.mkdtemp(prefix='cards-')))
    except cards.CardError as e:
      QMessageBox.warning(self, 'Could not build the deck', str(e))
      return
    self.repo.decks.save(deck)
    self.remember_fingerings()
    ref = self.repo.decks.save_export(deck, package.read_bytes())
    self.exported.append(pathlib.Path(ref.key))
    QMessageBox.information(
        self, 'Deck saved',
        'Saved "%s" for %s (%d KB).\n\nThe practice client offers it for download, '
        'so AnkiDroid can open it on the phone.' % (
            deck.name, self.piece.display_name, ref.size // 1024))
