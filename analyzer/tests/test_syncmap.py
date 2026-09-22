"""Tests for storing and validating the measure -> time map."""

import json
import pathlib

import pytest

from analyzer.config import AnalyzerConfig
from analyzer.schema import AudioSource, MeasureTime, SyncResult
from analyzer import syncmap


@pytest.fixture
def cfg(tmp_path):
  config = AnalyzerConfig(cache_dir=tmp_path / 'cache', output_dir=tmp_path / 'output')
  config.ensure_dirs()
  return config


def make_result(method=SyncResult.METHOD_LINEAR, n=4):
  measures = [MeasureTime(measure=i + 1, play_index=i,
                          start_s=i * 1.5, end_s=(i + 1) * 1.5)
              for i in range(n)]
  return SyncResult(method=method, measures=measures, seconds_per_measure=1.5,
                    audio_duration_s=6.0, warnings=['a warning'])


def test_round_trip(cfg):
  result = make_result(SyncResult.METHOD_DTW)
  result.quality['chroma_shift'] = 2
  path = syncmap.save(cfg, 'demo', result, played=[1, 2, 3, 4])
  assert path == cfg.sync_path('demo') and path.is_file()

  stored = syncmap.load(cfg, 'demo')
  assert stored is not None
  assert stored.slug == 'demo'
  assert stored.method == SyncResult.METHOD_DTW
  assert stored.is_trusted is True
  assert [m.measure for m in stored.result.measures] == [1, 2, 3, 4]
  assert stored.result.measures[1].start_s == pytest.approx(1.5)
  assert stored.result.quality['chroma_shift'] == 2
  assert stored.result.warnings == ['a warning']
  assert stored.played == [1, 2, 3, 4]
  assert stored.updated_at.endswith('Z')


def test_write_is_atomic(cfg):
  syncmap.save(cfg, 'demo', make_result())
  # no temporary file left behind after a successful write
  assert list(cfg.sync_dir.glob('*.tmp')) == []


def test_missing_map_is_none(cfg):
  assert syncmap.load(cfg, 'nothing-here') is None


def test_corrupt_map_is_none_rather_than_an_exception(cfg):
  path = cfg.sync_path('broken')
  path.write_text('{not json at all', encoding='utf-8')
  assert syncmap.load(cfg, 'broken') is None


def test_map_with_no_measures_is_none(cfg):
  path = cfg.sync_path('empty')
  path.write_text(json.dumps({'method': 'linear', 'measures': []}), encoding='utf-8')
  assert syncmap.load(cfg, 'empty') is None


def test_audio_fingerprint_notices_a_different_recording(cfg, tmp_path):
  wav = cfg.audio_path('demo')
  wav.write_bytes(b'first recording')
  audio = AudioSource(slug='demo', path=str(wav), url='https://example.invalid/x',
                      duration_s=6.0, sample_rate=22050, channels=1)
  syncmap.save(cfg, 'demo', make_result(), audio=audio)

  stored = syncmap.load(cfg, 'demo')
  assert stored.audio['sha256']
  assert syncmap.staleness(cfg, stored) is None

  wav.write_bytes(b'a different performance entirely')
  reason = syncmap.staleness(cfg, stored)
  assert reason and 'recording has changed' in reason


def test_staleness_notices_a_modified_score(cfg, tmp_path):
  score = tmp_path / 'piece.musicxml'
  score.write_text('<score/>', encoding='utf-8')
  syncmap.save(cfg, 'demo', make_result(), source_musicxml=score)
  stored = syncmap.load(cfg, 'demo')
  assert stored.source_size == len('<score/>')
  assert syncmap.staleness(cfg, stored) is None

  import os
  os.utime(score, (score.stat().st_atime, score.stat().st_mtime + 60))
  reason = syncmap.staleness(cfg, stored)
  assert reason and 'modified' in reason


def test_delete(cfg):
  syncmap.save(cfg, 'demo', make_result())
  assert syncmap.delete(cfg, 'demo') is True
  assert syncmap.load(cfg, 'demo') is None
  assert syncmap.delete(cfg, 'demo') is False


def test_summary_line_mentions_the_method(cfg):
  result = make_result(SyncResult.METHOD_DTW)
  result.quality['median_abs_change_vs_linear_s'] = 0.42
  stored = syncmap.StoredSync(slug='demo', result=result)
  line = syncmap.summary_line(stored)
  assert 'dtw' in line and '4 measures' in line and '0.42' in line


# ---------------------------------------------------------------------------
# plausibility: a map that cannot describe a real performance
# ---------------------------------------------------------------------------

def _map(durations, start=0.0, method=SyncResult.METHOD_DTW):
  measures, t = [], start
  for i, d in enumerate(durations):
    measures.append(MeasureTime(measure=i + 1, play_index=i, start_s=t, end_s=t + d))
    t += d
  return SyncResult(method=method, measures=measures, seconds_per_measure=1.5,
                    audio_duration_s=t)


def test_a_sane_map_raises_no_complaints():
  assert syncmap.check_plausibility(_map([1.5] * 20)) == []


def test_empty_map_is_reported():
  assert syncmap.check_plausibility(SyncResult(method='linear')) == ['the map is empty']


def test_zero_length_bars_are_reported():
  problems = syncmap.check_plausibility(_map([1.5] * 5 + [0.0] + [1.5] * 5))
  assert any('no time at all' in p and 'bar 6' in p for p in problems)


def test_very_short_bars_are_reported():
  problems = syncmap.check_plausibility(_map([1.5] * 5 + [0.2] + [1.5] * 5))
  assert any('far shorter' in p for p in problems)


def test_out_of_order_markers_are_reported():
  result = _map([1.5] * 4)
  result.measures[2].start_s = 0.1        # jumps backwards
  assert any('out of order' in p for p in syncmap.check_plausibility(result))


def test_a_bar_ending_before_it_starts_is_reported():
  result = _map([1.5] * 4)
  result.measures[1].end_s = result.measures[1].start_s - 1.0
  assert any('ends before it starts' in p for p in syncmap.check_plausibility(result))


def test_a_sudden_swing_in_bar_length_is_reported():
  problems = syncmap.check_plausibility(_map([1.5] * 5 + [6.0] + [1.5] * 5))
  assert any('swings' in p for p in problems)
