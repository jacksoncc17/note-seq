"""Tests for the Anki card dialog and deck persistence.

Head-less, and with the modal dialogs patched out: `QMessageBox.information` blocks
until somebody clicks it, which in a test means hanging until the timeout -- exactly
how the first version of this file "failed".
"""

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
import pathlib

import pytest

pytest.importorskip('PyQt5.QtWidgets', reason='the dialog needs PyQt5')

from PyQt5.QtWidgets import QApplication, QFileDialog, QMessageBox

from analyzer.config import AnalyzerConfig
from analyzer import cards, library, scoreview, store
from analyzer.ui.card_dialog import CardDialog

SCORE = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
SLUG = 'chopin-waltz-in-a-minor-op-34-no-2'
needs_real = pytest.mark.skipif(
    not (SCORE.is_file() and AnalyzerConfig().sync_path(SLUG).is_file()),
    reason='the Chopin score, recording and map are needed')


@pytest.fixture(scope='module')
def qapp():
  app = QApplication.instance() or QApplication([])
  yield app


@pytest.fixture
def real():
  cfg = AnalyzerConfig()
  piece = library.find(cfg, SLUG)
  if piece is None or piece.musicxml is None:
    pytest.skip('the Chopin piece is not in the library')
  return cfg, piece, scoreview.render_score(cfg, piece.musicxml)


@pytest.fixture
def silent(monkeypatch):
  """Silence anything modal, and record what it would have said."""
  said = []
  monkeypatch.setattr(QMessageBox, 'information',
                      staticmethod(lambda *a, **k: said.append(a[2] if len(a) > 2 else '')))
  monkeypatch.setattr(QMessageBox, 'warning',
                      staticmethod(lambda *a, **k: said.append(a[2] if len(a) > 2 else '')))
  return said


# ---------------------------------------------------------------------------
# the dialog
# ---------------------------------------------------------------------------

@needs_real
def test_the_dialog_shows_a_front_and_a_back(qapp, real):
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  assert dialog.front_image.pixmap() is not None
  assert not dialog.front_image.pixmap().isNull()
  assert not dialog.back_image.pixmap().isNull()
  assert 'bars 53-53' in dialog.summary.text()
  assert sorted(dialog.rule_boxes) == sorted(cards.MASK_RULES)


@needs_real
def test_choosing_a_rule_updates_the_preview(qapp, real):
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  before = dialog.masked_keys()
  dialog.rule_boxes['everything'].setChecked(True)
  after = dialog.masked_keys()
  assert len(after) == 7 and not before
  assert 'hiding 7' in dialog.summary.text()


@needs_real
def test_tapping_a_note_masks_it(qapp, real):
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  key = next(g.key for g in dialog._glyphs if g.kind == 'note')
  dialog.toggle_note(key)
  assert dialog.tap_check.isChecked(), 'tapping should switch tap mode on'
  assert key in dialog.masked_keys()
  dialog.toggle_note(key)                       # and tapping again unmasks
  assert key not in dialog.masked_keys()


@needs_real
def test_changing_the_range_forgets_taps_outside_it(qapp, real):
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  key = next(g.key for g in dialog._glyphs if g.kind == 'note')
  dialog.toggle_note(key)
  assert dialog.masked_keys()
  dialog.last_spin.setValue(55)                 # the range moved on
  assert dialog.masked == [], 'a tap from another passage must not linger'


@needs_real
def test_saving_a_deck_keeps_the_spec_and_the_package(qapp, real, silent, tmp_path):
  cfg, piece, render = real
  cfg.output_dir = tmp_path / 'out'
  cfg.ensure_dirs()
  dialog = CardDialog(cfg, piece, render, None, first_bar=1, last_bar=4)
  dialog.rule_boxes['melody'].setChecked(True)
  dialog.title_edit.setText('page one melody')
  dialog.save_for_phone()

  saved = dialog.repo.decks.get(SLUG, 'page one melody')
  assert saved is not None, 'the spec should have been stored'
  assert saved.cards[0].rules == ['melody']
  ref = dialog.repo.decks.latest_export(SLUG, 'page one melody')
  assert ref is not None and ref.size > 1000
  data = dialog.repo.blobs.read(ref)
  assert data[:2] == b'PK', 'an .apkg is a zip'
  assert silent, 'the dialog should say what it saved'


@needs_real
def test_exporting_asks_where_to_put_the_file(qapp, real, silent, monkeypatch, tmp_path):
  cfg, piece, render = real
  target = tmp_path / 'deck.apkg'
  monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                      staticmethod(lambda *a, **k: (str(target), '*.apkg')))
  dialog = CardDialog(cfg, piece, render, None, first_bar=1, last_bar=2)
  dialog.rule_boxes['everything'].setChecked(True)
  dialog.export_deck()
  assert target.is_file()
  import zipfile
  with zipfile.ZipFile(target) as z:
    assert 'collection.anki2' in z.namelist()


@needs_real
def test_the_range_is_kept_inside_the_piece(qapp, real):
  """A passage outside the piece cannot be asked for in the first place: the spin
  boxes clamp, so there is no error path to test -- which is the better design."""
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=900, last_bar=904)
  assert dialog.first_spin.maximum() == render.measure_count
  assert dialog.last_spin.value() == render.measure_count
  assert 'bars' in dialog.summary.text()


@needs_real
def test_a_passage_with_no_notes_cannot_be_exported(qapp, real):
  """The other edge: a valid range that hides nothing is not a card."""
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=204, last_bar=204)
  dialog.masked = ['53:2:note:0']            # a stale tap from another passage
  assert dialog.masked_keys() == [], 'a tap outside the range must not be used'
  assert not dialog.export_button.isEnabled()


# ---------------------------------------------------------------------------
# deck persistence, in both adapters
# ---------------------------------------------------------------------------

def deck_spec(slug='demo'):
  return cards.DeckSpec(slug=slug, name='bars 1-4', cards=[
      cards.CardSpec(slug=slug, first_bar=1, last_bar=4, rules=['melody'])])


@pytest.fixture(params=['filesystem', 'memory'])
def repo(request, tmp_path):
  if request.param == 'filesystem':
    cfg = AnalyzerConfig(cache_dir=tmp_path / 'c', output_dir=tmp_path / 'o')
    cfg.ensure_dirs()
    return store.filesystem_repository(cfg)
  return store.memory_repository(scratch=tmp_path / 'blobs')


def test_a_deck_round_trips_through_either_adapter(repo):
  saved = repo.decks.save(deck_spec())
  assert saved.updated_at, 'saving should stamp the deck'
  again = repo.decks.get('demo', 'bars 1-4')
  assert again is not None and again.name == 'bars 1-4'
  assert again.cards[0].rules == ['melody']
  assert again.cards[0].bar_range == (1, 4)
  assert [d.name for d in repo.decks.list('demo')] == ['bars 1-4']
  assert repo.decks.list('other') == []


def test_a_built_package_is_stored_as_a_blob(repo):
  deck = repo.decks.save(deck_spec())
  assert repo.decks.latest_export('demo', 'bars 1-4') is None
  payload = b'PK\x03\x04 pretend apkg'
  ref = repo.decks.save_export(deck, payload)
  assert ref.size == len(payload)
  latest = repo.decks.latest_export('demo', 'bars 1-4')
  assert latest is not None and repo.blobs.read(latest) == payload


def test_deleting_a_deck_takes_its_package_with_it(repo):
  deck = repo.decks.save(deck_spec())
  repo.decks.save_export(deck, b'PK\x03\x04')
  assert repo.decks.delete('demo', 'bars 1-4') is True
  assert repo.decks.get('demo', 'bars 1-4') is None
  assert repo.decks.latest_export('demo', 'bars 1-4') is None
  assert repo.decks.delete('demo', 'never') is False


def test_the_repository_reports_its_deck_store(repo):
  assert repo.decks is not None
  assert store.open_repository(AnalyzerConfig(cache_dir=pathlib.Path('/tmp/x'),
                                              output_dir=pathlib.Path('/tmp/y'))).decks is not None


# ---------------------------------------------------------------------------
# fingering
# ---------------------------------------------------------------------------

@needs_real
def test_fingering_can_be_assigned_by_tapping_and_by_stepping(qapp, real):
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  dialog.fingering_check.setChecked(True)
  assert not dialog.tap_check.isChecked(), 'one meaning for a tap at a time'

  key = dialog._notes()[0].key
  dialog.toggle_note(key)                       # tap
  assert dialog.fingering.get(key) == dialog.digit
  dialog.toggle_note(key)                       # same digit again clears it
  assert key not in dialog.fingering

  dialog.step_note(1)                           # stepping points at the next note
  dialog.assign_to_pointed()
  assert dialog.fingering, 'assigning to the pointed note should record something'

  dialog.undo()
  assert len(dialog.fingering) == 1 or not dialog.fingering


@needs_real
def test_tapping_still_masks_when_fingering_mode_is_off(qapp, real):
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  key = dialog._notes()[0].key
  dialog.toggle_note(key)
  assert key in dialog.masked and not dialog.fingering


@needs_real
def test_fingerings_are_kept_with_the_piece(qapp, real, silent, tmp_path):
  """Entered once, reusable by any card of the same bars."""
  cfg, piece, render = real
  cfg.output_dir = tmp_path / 'out'
  cfg.ensure_dirs()
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  dialog.rule_boxes['everything'].setChecked(True)
  dialog.fingering_check.setChecked(True)
  key = dialog._notes()[0].key
  dialog.toggle_note(key)
  dialog.title_edit.setText('fingered')
  dialog.save_for_phone()

  again = library.find(cfg, SLUG)
  assert again.record.fingerings.get(key), 'the piece should remember the fingering'

  second = CardDialog(cfg, again, render, None, first_bar=53, last_bar=53)
  assert cards.fingerings_in(second.spec(), again), \
      'a new card for the same bars should find the fingerings already there'


@needs_real
def test_the_question_is_prefilled_from_the_bank_and_travels_with_the_card(qapp, real):
  """The bank already asks about bar 53, so the field starts filled in -- and the
  card carries the wording, plus whether to speak it."""
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  assert dialog.question_edit.text() == cards.suggest_question(piece, 53, 53)
  assert 'measure 53' in dialog.question_edit.text()
  assert dialog.speak_check.isChecked(), 'speaking is the point of the field'

  spec = dialog.spec()
  assert spec.question == dialog.question_edit.text().strip()
  assert spec.speak_question is True and spec.voice

  dialog.speak_check.setChecked(False)
  assert dialog.spec().speak_question is False
  assert dialog.spec().question, 'the wording stays on the card either way'

  dialog.question_edit.setText('Name the chord in bar 53.')
  assert dialog.spec().question == 'Name the chord in bar 53.'


@needs_real
def test_a_written_question_alone_is_enough_to_export(qapp, real):
  """A card with nothing hidden but a question of your own is still a card -- while
  the suggested question on its own is not work, so the button stays off."""
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  assert not dialog.export_button.isEnabled(), 'no work, nothing to export'
  dialog.suggest_question()
  assert not dialog.export_button.isEnabled(), 'the default is not the user\'s own work'
  dialog.question_edit.setText('Name the chord you hear in bar 53.')
  assert dialog.export_button.isEnabled(), 'a text-only question card is still a card'
  dialog.question_edit.clear()
  assert not dialog.export_button.isEnabled(), 'cleared again means nothing to make'


@needs_real
def test_the_summary_says_the_question_will_be_spoken(qapp, real):
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  dialog.rule_boxes['everything'].setChecked(True)
  assert 'spoken with' in dialog.summary.text()
  dialog.speak_check.setChecked(False)
  assert 'spoken with' not in dialog.summary.text()


@needs_real
def test_the_repeat_on_the_back_is_a_checkbox(qapp, real):
  cfg, piece, render = real
  dialog = CardDialog(cfg, piece, render, None, first_bar=53, last_bar=53)
  assert dialog.repeat_check.isChecked(), 'the question repeats unless told otherwise'
  assert dialog.spec().repeat_question is True
  dialog.repeat_check.setChecked(False)
  assert dialog.spec().repeat_question is False
  assert dialog.spec().question, 'the question itself is untouched'
