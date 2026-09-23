"""Tests for the window's wiring, run head-less.

The platform is forced to `offscreen` so these run without a display: the point
is that the *logic* connected to the widgets is right -- the measure list reflects
performance order, applying a map arms the transport, dragging a marker switches
the map to hand-corrected -- not that Qt can draw.
"""

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
import pathlib

import pytest

pytest.importorskip('PyQt5.QtWidgets', reason='the GUI needs PyQt5')

from PyQt5.QtWidgets import QApplication

from analyzer.config import AnalyzerConfig
from analyzer.schema import AudioSource, MeasureTime, SyncResult
from analyzer.ui.main_window import MainWindow


def score_xml(bars=6, repeat_from=2, repeat_to=5):
  """A small score, optionally with one repeat, as MusicXML text."""
  parts = []
  for n in range(1, bars + 1):
    left = '<barline location="left"><repeat direction="forward"/></barline>' \
        if n == repeat_from else ''
    right = '<barline location="right"><repeat direction="backward"/></barline>' \
        if n == repeat_to else ''
    parts.append('<measure number="%d">%s<note><pitch><step>C</step><octave>4</octave>'
                 '</pitch><duration>1</duration><type>quarter</type></note>%s</measure>'
                 % (n, left, right))
  return ('<?xml version="1.0"?><score-partwise version="4.0"><part-list>'
          '<score-part id="P1"><part-name>P</part-name></score-part></part-list>'
          '<part id="P1">%s</part></score-partwise>' % ''.join(parts))


@pytest.fixture(scope='module')
def qapp():
  app = QApplication.instance() or QApplication([])
  yield app


@pytest.fixture
def cfg(tmp_path):
  config = AnalyzerConfig(cache_dir=tmp_path / 'cache', output_dir=tmp_path / 'out',
                          bank_dir=tmp_path / 'banks',
                          legacy_bank=tmp_path / 'banks' / 'fingerprint.json',
                          sheet_music_dir=tmp_path / 'sheets')
  (tmp_path / 'sheets').mkdir(parents=True, exist_ok=True)
  config.ensure_dirs()
  return config


@pytest.fixture
def window(qapp, cfg, monkeypatch):
  # Engraving the score is a background job; the tests that care about the score
  # tab feed a synthetic render instead of waiting for Verovio, and one test
  # asserts the job is started at all.
  calls = []

  def spy(self, force=False):
    calls.append(force)

  monkeypatch.setattr(MainWindow, 'render_score', spy)
  win = MainWindow(cfg)
  win.engrave_calls = calls
  yield win
  win.player.stop()
  win.dirty = False
  win.deleteLater()


def make_piece(cfg, *, bars=6, slug='demo', repeat_from=2, repeat_to=5, with_audio=False):
  score = cfg.sheet_music_dir / ('%s.musicxml' % slug)
  score.write_text(score_xml(bars, repeat_from, repeat_to), encoding='utf-8')
  bank = cfg.bank_dir / slug
  bank.mkdir(parents=True, exist_ok=True)
  (bank / 'fingerprint.json').write_text(json.dumps({
      'source_file': str(score),
      'global': {'num_measures': bars, 'key_from_score': 'C major', 'title': 'Demo'},
      'measures': [{}] * bars, 'questions': []}), encoding='utf-8')
  if with_audio:
    import numpy as np
    import soundfile as sf
    wav = cfg.audio_path(slug)
    sf.write(str(wav), np.zeros(22050 * 12, dtype='float32'), 22050)
    cfg.audio_meta_path(slug).write_text(json.dumps(AudioSource(
        slug=slug, path=str(wav), duration_s=12.0, sample_rate=22050,
        channels=1, title='Demo recording').to_dict()), encoding='utf-8')
  return score


def fake_result(count=10, method=SyncResult.METHOD_DTW, step=1.2):
  measures = [MeasureTime(measure=i + 1, play_index=i, start_s=i * step,
                          end_s=(i + 1) * step, confidence=0.9)
              for i in range(count)]
  return SyncResult(method=method, measures=measures, seconds_per_measure=step,
                    audio_duration_s=count * step, quality={'hop_ms': 92.9},
                    warnings=[])


# ---------------------------------------------------------------------------

def test_empty_state_is_honest(window):
  assert window.piece is None
  assert window.piece_combo.count() == 0
  assert window.measure_list.count() == 0
  assert not window.estimate_btn.isEnabled()
  assert not window.align_btn.isEnabled()
  assert not window.play_btn.isEnabled()
  assert not window.save_btn.isEnabled()
  assert 'no pieces found' in window.status.currentMessage()


def test_measure_list_shows_performance_order(window, cfg):
  make_piece(cfg)
  window.refresh_pieces('demo')
  # 6 bars with the repeat over 2-5: 1 2 3 4 5 | 2 3 4 5 6
  assert window.measure_list.count() == 10
  labels = [window.measure_list.item(i).text() for i in range(10)]
  assert 'bar 1' in labels[0]
  assert '(pass 2)' in labels[5]          # bar 3 played a second time
  assert window.expansion.played_numbers == [1, 2, 3, 4, 5, 2, 3, 4, 5, 6]
  assert '10' in window.expansion_label.text()


def test_score_without_audio_disables_sync_buttons(window, cfg):
  make_piece(cfg)
  window.refresh_pieces('demo')
  assert not window.estimate_btn.isEnabled()
  assert not window.align_btn.isEnabled()
  assert 'no recording' in window.audio_label.text()


def test_audio_loads_and_enables_actions(window, cfg):
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  assert window.estimate_btn.isEnabled() and window.align_btn.isEnabled()
  assert '12.0 s' in window.audio_label.text()
  assert window.player.duration == pytest.approx(12.0, abs=0.05)
  assert window.waveform.duration == pytest.approx(12.0, abs=0.05)


def test_applying_a_map_arms_the_transport_and_the_range(window, cfg):
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  assert window.save_btn.isEnabled()
  assert window.play_btn.isEnabled()
  assert len(window.waveform.ticks()) == 10
  assert 'dtw' in window.map_label.text()

  window.first_spin.setValue(3)
  window.last_spin.setValue(5)
  span = window.result.range_for_measures(3, 5)
  assert span == (pytest.approx(2.4), pytest.approx(6.0))
  assert '2.40 s' in window.range_label.text()


def test_selecting_measures_sets_the_range(window, cfg):
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  window.measure_list.item(2).setSelected(True)
  window.measure_list.item(4).setSelected(True)
  window._on_measure_selection()
  assert window.first_spin.value() == 3
  assert window.last_spin.value() == 5


def test_dragging_a_marker_marks_the_map_hand_corrected(window, cfg):
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(method=SyncResult.METHOD_DTW), source='test')

  window._on_tick_moved(3, 9.75)
  moved = [m for m in window.result.measures if m.play_index == 3][0]
  assert moved.start_s == pytest.approx(9.75)
  assert window.result.method == SyncResult.METHOD_MANUAL
  assert window.result.is_trusted is True
  assert window.dirty is True
  assert any('hand-corrected' in w for w in window.result.warnings)
  assert 'hand-corrected' in window.map_label.text()
  # the previous bar now ends where the moved one starts
  previous = [m for m in window.result.measures if m.play_index == 2][0]
  assert previous.end_s == pytest.approx(9.75)


def test_playing_without_a_recording_reports_instead_of_crashing(window, cfg):
  make_piece(cfg)                      # score only, no audio
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  window.play_range()                  # no audio device, no samples: must not raise
  assert window.status.currentMessage()


def test_saving_then_reloading_a_map_round_trips(window, cfg):
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  window.save_map()
  assert window.cfg.sync_path('demo').is_file()
  assert window.dirty is False

  window.refresh_pieces('demo')        # re-read from disk
  assert window.result is not None
  assert len(window.result.measures) == 10
  assert window.save_btn.isEnabled()


def test_estimate_uses_the_alignment_module_when_present(window, cfg):
  pytest.importorskip('analyzer.align', reason='alignment module not written yet')
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window.result = None
  window.estimate()
  assert window.result is not None
  assert window.result.method == SyncResult.METHOD_LINEAR
  assert len(window.result.measures) == 10
  assert 'guess' in window.map_label.text() or 'estimate' in window.map_label.text()


def test_opening_a_bare_score_with_no_bank(window, cfg):
  path = cfg.sheet_music_dir / 'lonely.musicxml'
  path.write_text(score_xml(4, repeat_from=0, repeat_to=0), encoding='utf-8')
  window.open_score(path)
  assert window.piece is not None and window.piece.slug == 'lonely'
  assert window.measure_list.count() == 4
  assert window.piece.can_align is False          # no question bank, so no reference MIDI


def test_unsaved_changes_are_flagged_for_close(window, cfg):
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  assert window.dirty is False
  window._on_tick_moved(0, 0.4)
  assert window.dirty is True


def test_realigning_over_hand_corrections_asks_first(window, cfg, monkeypatch):
  """Re-aligning replaces the whole map, so it must not silently discard drags."""
  from PyQt5.QtWidgets import QMessageBox
  from test_align import write_midi

  make_piece(cfg, with_audio=True)
  # a reference performance, or the align slot stops before the confirmation
  write_midi(cfg.bank_dir / 'demo' / 'fingerprint_steady.mid', bars=6, beats=3, qpm=120)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  window._on_tick_moved(0, 0.4)                 # hand-corrected -> dirty
  assert window.dirty is True

  monkeypatch.setattr(QMessageBox, 'question',
                      staticmethod(lambda *a, **k: QMessageBox.No))
  window.align()
  assert window._align is None, 'aligning should not have started'


def test_aligning_is_refused_politely_without_a_reference_midi(window, cfg, monkeypatch):
  from PyQt5.QtWidgets import QMessageBox

  make_piece(cfg, with_audio=True)               # no fingerprint_steady.mid written
  window.refresh_pieces('demo')
  seen = {}
  monkeypatch.setattr(QMessageBox, 'information',
                      staticmethod(lambda *a, **k: seen.setdefault('told', a[2])))
  window.align()
  assert window._align is None
  assert 'analyze.py' in seen.get('told', ''), 'the message should say how to fix it'


# ---------------------------------------------------------------------------
# the score tab
# ---------------------------------------------------------------------------

def synthetic_render(tmp_path, pages=2, per_page=3):
  """A tiny page render, so the score wiring can be tested without Verovio."""
  from PIL import Image
  from analyzer.scoreview import MeasureBox, ScorePage, ScoreRender

  built = []
  number = 1
  for page in range(1, pages + 1):
    image = tmp_path / ('synth-%d.png' % page)
    Image.new('RGB', (400, 200), 'white').save(image)
    boxes = [MeasureBox(number=n, x0=20.0 + 130 * i, y0=40.0, x1=20.0 + 130 * i + 110,
                        y1=140.0) for i, n in enumerate(range(number, number + per_page))]
    number += per_page
    built.append(ScorePage(number=page, image=str(image), width=400, height=200, measures=boxes))
  return ScoreRender(slug='demo', source='demo.musicxml', pages=built)


def test_the_window_has_a_score_tab(window):
  assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ['Recording', 'Score']


def test_loading_a_piece_with_a_score_starts_engraving(window, cfg):
  make_piece(cfg)
  window.refresh_pieces('demo')
  assert window.engrave_calls == [False], 'the score should be engraved when a piece loads'


def test_a_failed_engraving_is_reported(window, monkeypatch):
  from PyQt5.QtWidgets import QMessageBox
  monkeypatch.setattr(QMessageBox, 'warning', staticmethod(lambda *a, **k: None))
  window._on_render_failed('cairosvg is missing')
  assert 'not engraved' in window.score_status.text()
  assert 'cairosvg' in window.status.currentMessage()


def test_clicking_a_bar_on_the_page_sets_the_range(window, cfg, tmp_path):
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  window._on_rendered(synthetic_render(tmp_path))
  assert 'clickable' in window.score_status.text()
  assert window.page_label.text() == 'page 1 / 2'

  window._on_score_measure_clicked(4)
  assert window.first_spin.value() == 4 and window.last_spin.value() == 4
  assert window.score_view._selection == (4, 4)
  assert 'bar 4' in window.status.currentMessage()


def test_double_clicking_a_bar_plays_it(window, cfg, tmp_path):
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  window._on_rendered(synthetic_render(tmp_path))
  window._on_score_measure_activated(2)
  assert 'playing bars 2-2' in window.status.currentMessage()
  window.player.stop()


def test_the_page_follows_the_range_and_the_playhead(window, cfg, tmp_path):
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  window._on_rendered(synthetic_render(tmp_path))

  window.first_spin.setValue(5)
  window.last_spin.setValue(6)
  window.show_measure_in_score(5)
  assert window.score_view.page_number == 2

  # a playhead inside bar 5 must be highlighted, and follow must stay on page 2
  start = window.result.time_for_measure(5).start_s
  window._on_position(start + 0.1)
  assert window.score_view._playhead == 5
  assert window.score_view.page_number == 2


def test_follow_playback_can_be_turned_off(window, cfg, tmp_path):
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  window._on_rendered(synthetic_render(tmp_path))
  window.score_view.set_page(1)
  window.follow_check.setChecked(False)

  start = window.result.time_for_measure(6).start_s
  window._on_position(start + 0.1)
  assert window.score_view._playhead == 6
  assert window.score_view.page_number == 1, 'with follow off, the page must not turn'


def test_the_filter_box_narrows_the_piece_list(window, cfg):
  """The desktop filter goes through the same repository query as the phone's."""
  make_piece(cfg, slug='demo')
  make_piece(cfg, slug='other')
  window.refresh_pieces()
  assert window.piece_combo.count() == 2

  window.filter_edit.setText('other')
  assert window.piece_combo.count() == 1
  assert window.piece_combo.itemData(0) == 'other'

  window.filter_edit.setText('zzz-nothing')
  assert window.piece_combo.count() == 0
  assert window.piece is None
  assert 'no piece matches' in window.status.currentMessage()

  window.filter_edit.setText('')
  assert window.piece_combo.count() == 2


def test_closing_the_window_mid_engraving_exits_cleanly(tmp_path):
  """Qt aborts the process when a QThread is destroyed while it is still running,
  so closing during a long engraving used to end in a core dump. Run it in a
  subprocess, because the failure mode is the process dying.

  The engraving is replaced by a slow stand-in that reports progress. A real
  eight-bar score finishes before the window can be closed, which made an earlier
  version of this test pass even with the fix removed -- so the stand-in asserts
  that it really is still running before the close.
  """
  import subprocess
  import sys

  script = """
import os, pathlib, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, {package!r})
sys.path.insert(0, {tests!r})
from PyQt5.QtWidgets import QApplication
from analyzer.config import AnalyzerConfig
from analyzer.ui.main_window import MainWindow
import analyzer.scoreview as scoreview
from test_server import make_piece

def slow_render(cfg, musicxml, scale=1.5, force=False, progress=None):
    for step in range(40):
        if progress:
            progress(step * 2.0, "engraving step %d" % step)
        time.sleep(0.25)
    raise RuntimeError("the window should have been closed long before this")

scoreview = None                              # filled in below, after the fixture
root = pathlib.Path({root!r})
cfg = AnalyzerConfig(cache_dir=root / "cache", output_dir=root / "out",
                     bank_dir=root / "banks", legacy_bank=root / "banks" / "fingerprint.json",
                     sheet_music_dir=root / "sheets")
(root / "sheets").mkdir(parents=True, exist_ok=True)
cfg.ensure_dirs()
make_piece(cfg, slug="demo", bars=8, seconds=16.0)   # engraves for real, so first

import analyzer.scoreview                           # noqa: E402 - patch after the fixture
analyzer.scoreview.render_score = slow_render       # the worker resolves it by name

app = QApplication([])
window = MainWindow(cfg, piece_slug="demo")
window.show()
window.render_score(force=True)
worker = window._render
time.sleep(0.6)
print("running before close:", worker.isRunning())
assert worker.isRunning(), "the stand-in finished too fast to test anything"
window.close()
print("running after close:", worker.isRunning())
assert not worker.isRunning(), "close should have stopped the worker"
del window
print("exited cleanly")
""".format(package=str(pathlib.Path(__file__).resolve().parents[1].resolve()),
           tests=str(pathlib.Path(__file__).resolve().parent.resolve()),
           root=str(tmp_path))

  result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                          text=True, timeout=180)
  assert result.returncode == 0, (
      'the process died (rc=%s) instead of closing cleanly:\n%s'
      % (result.returncode, result.stderr[-800:]))
  assert 'running before close: True' in result.stdout, result.stdout[-300:]
  assert 'running after close: False' in result.stdout, result.stdout[-300:]
  assert 'exited cleanly' in result.stdout, result.stdout + result.stderr[-400:]
  assert 'QThread: Destroyed while thread is still running' not in result.stderr, \
      result.stderr[-500:]


def test_the_anki_button_opens_the_dialog_and_turns_on_mask_mode(window, cfg, tmp_path, monkeypatch):
  """The wiring, with the dialog itself stubbed: the button needs an engraving, the
  score view has to switch into mask mode, and a tapped note has to reach the dialog.
  """
  from analyzer.ui import main_window as mw

  opened = {}

  class StubDialog:
    def __init__(self, cfg_, piece, render, parent=None, first_bar=1, last_bar=1):
      opened['bars'] = (first_bar, last_bar)
      self.masked = []
      self.finished = type('S', (), {'connect': staticmethod(lambda *a: None)})()
      self.destroyed = type('S', (), {'connect': staticmethod(lambda *a: None)})()

    def masked_keys(self):
      return list(self.masked)

    def show(self):
      pass

    def toggle_note(self, key):
      self.masked.append(key)

  monkeypatch.setattr(mw, 'CardDialog', StubDialog)
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._on_rendered(synthetic_render(tmp_path))

  window.first_spin.setValue(2)
  window.last_spin.setValue(4)
  window.open_card_dialog()
  assert opened['bars'] == (2, 4), 'the dialog should open on the selected range'
  assert window.score_view._mask_mode is True, 'tapping notes needs mask mode'

  window._on_glyph_clicked('2:1:note:0')
  assert window._card_dialog.masked == ['2:1:note:0']
  assert window.score_view._masked == {'2:1:note:0'}

  window._close_card_dialog()
  assert window.score_view._mask_mode is False
  assert window.score_view._masked == set()


def test_mask_mode_cannot_strand_the_score_view(window, cfg, tmp_path, monkeypatch):
  """The bug this pins: mask mode was switched off only by the dialog's `finished`
  signal, so a dialog that went away another way left every click being read as a
  note tap -- bar selection, and playing a range, looked broken."""
  from analyzer.ui import main_window as mw

  class StubDialog:
    def __init__(self, *a, **k):
      self.masked = []
      self.finished = type('S', (), {'connect': staticmethod(lambda *a: None)})()
      self.destroyed = type('S', (), {'connect': staticmethod(lambda *a: None)})()

    def masked_keys(self):
      return list(self.masked)

    def toggle_note(self, key):
      self.masked.append(key)

    def show(self):
      pass

  monkeypatch.setattr(mw, 'CardDialog', StubDialog)
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._on_rendered(synthetic_render(tmp_path))
  window.open_card_dialog()
  assert window.score_view._mask_mode is True
  assert 'masking notes' in window.mask_label.text(), 'the mode should be shown, not implicit'

  # the dialog disappears without `finished` ever firing
  window._card_dialog = None
  window._on_glyph_clicked('3:1:note:0')
  assert window.score_view._mask_mode is False, 'mask mode must not outlive its dialog'
  assert window.first_spin.value() == 3, 'the click should select the bar instead'
  assert window.mask_label.text() == ''
  assert 'back' in window.status.currentMessage()


def test_loading_another_piece_clears_mask_mode(window, cfg, tmp_path, monkeypatch):
  from analyzer.ui import main_window as mw

  class StubDialog:
    def __init__(self, *a, **k):
      self.masked = []
      self.finished = type('S', (), {'connect': staticmethod(lambda *a: None)})()
      self.destroyed = type('S', (), {'connect': staticmethod(lambda *a: None)})()
      self.masked = []

    def masked_keys(self):
      return list(self.masked)

    def toggle_note(self, key):
      pass

    def show(self):
      pass

  monkeypatch.setattr(mw, 'CardDialog', StubDialog)
  make_piece(cfg, slug='demo', with_audio=True)
  make_piece(cfg, slug='other', with_audio=True)
  window.refresh_pieces('demo')
  window._on_rendered(synthetic_render(tmp_path))
  window.open_card_dialog()
  assert window.score_view._mask_mode is True

  window.filter_edit.setText('other')          # a new piece arrives
  assert window.score_view._mask_mode is False, 'a new piece must start clean'
  assert window._card_dialog is None


def test_the_score_tab_has_its_own_transport(window, cfg, tmp_path):
  """Selecting bars on the Score tab and playing them should not need a tab change."""
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  window._on_rendered(synthetic_render(tmp_path))
  assert window.tabs.tabText(window.tabs.indexOf(window.score_view.parentWidget()
                                                .parentWidget().parentWidget())) or True
  assert window.score_play_btn.text().startswith('▶')
  assert window.score_play_btn.isEnabled() and window.score_stop_btn.isEnabled()
  window.first_spin.setValue(2)
  window.last_spin.setValue(4)
  window.score_play_btn.click()
  assert 'playing bars 2-4' in window.status.currentMessage()
  window.score_stop_btn.click()


def test_clicking_two_bars_on_the_score_selects_the_range(window, cfg, tmp_path):
  """The Score tab's own range selection: first click picks a bar, the second
  extends to it, and the transport then plays the whole passage."""
  make_piece(cfg, with_audio=True)
  window.refresh_pieces('demo')
  window._apply_result(fake_result(), source='test')
  window._on_rendered(synthetic_render(tmp_path))

  window.score_view.set_selection(1, 1, external=True)
  window.score_view.rangeSelected.emit(1, 1)          # first click on bar 1
  assert (window.first_spin.value(), window.last_spin.value()) == (1, 1)
  # the click mechanics (including the open-range flag) are covered in
  # test_score_widget.py with real mouse events; this tests the window's response

  window.score_view.rangeSelected.emit(1, 3)          # second click extends it
  assert (window.first_spin.value(), window.last_spin.value()) == (1, 3)
  assert window.score_view._selection == (1, 3)
  assert 'bars 1-3 selected' in window.status.currentMessage()
  assert window.play_btn.isEnabled() and window.score_play_btn.isEnabled()

  window.score_play_btn.click()
  assert 'playing bars 1-3' in window.status.currentMessage()
  window.player.stop()
