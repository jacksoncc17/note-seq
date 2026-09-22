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
def window(qapp, cfg):
  win = MainWindow(cfg)
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
