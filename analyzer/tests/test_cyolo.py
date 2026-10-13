"""Tests for the CyoloScoreFollowing export.

The format is somebody else's, so the tests pin the parts of it that were *read off*
their loader and that are easy to get subtly wrong: which coordinates are padded and
which are not, that the onsets are seconds (their loader multiplies by 20 fps), and that
the records are plain dicts (their loader iterates them by key, which a numpy structured
record does not support).

Nothing here needs torch or their checkout: writing the file is independent of reading
it. `tools/cyolo_eval.py` is what runs their model over it.
"""

import pathlib

import pytest

from analyzer import cyolo, library
from analyzer.config import AnalyzerConfig

SCORE = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
SLUG = 'chopin-waltz-in-a-minor-op-34-no-2'
needs_real = pytest.mark.skipif(
    not (SCORE.is_file() and AnalyzerConfig().sync_path(SLUG).is_file()),
    reason='the Chopin score and its map are needed')


@pytest.fixture
def real(tmp_path):
  # the real config: the export reads the piece's own engraving and map, and only the
  # file it writes goes to tmp_path
  cfg = AnalyzerConfig()
  piece = library.find(cfg, SLUG)
  return cfg, piece


def _load(path):
  import numpy as np
  return np.load(str(path), allow_pickle=True)


@needs_real
def test_the_export_has_the_keys_and_shapes_their_loader_reads(real, tmp_path):
  import numpy as np
  cfg, piece = real
  summary = cyolo.export_piece(cfg, piece, tmp_path, timing='recording')
  data = _load(summary['npz'])

  assert set(data.files) == {'sheets', 'coords', 'bars', 'systems', 'synthesized'}
  assert data['sheets'].dtype == np.uint8
  assert data['sheets'].ndim == 3
  pages, height, width = data['sheets'].shape
  assert pages == summary['pages'] == 9, 'this score engraves to nine pages'
  assert (data['sheets'] == 255).mean() > 0.9, 'paper is 255 and most of a page is paper'
  assert len(data['coords']) == summary['notes'] > 1000
  assert len(data['bars']) == summary['bars']
  assert len(data['systems']) == summary['systems']


@needs_real
def test_coords_are_unpadded_but_bars_and_systems_are_not(real, tmp_path):
  """Their loader adds the pad to all three, and their own files already have it in the
  bars and systems -- so reproducing that is what makes our piece look like theirs to
  the model. Notes are the other way round, because their loader's shift for notes is
  correct."""
  cfg, piece = real
  summary = cyolo.export_piece(cfg, piece, tmp_path, timing='recording')
  data = _load(summary['npz'])
  _pages, height, width = data['sheets'].shape
  pad = summary['pad']
  assert pad > 0

  note_x = [float(c['note_x']) for c in data['coords']]
  assert min(note_x) >= 0 and max(note_x) < width, \
      'note x must be inside the unpadded page, because their loader pads it'
  assert min(float(b['x']) for b in data['bars']) >= pad - 1, \
      'bar x already includes the pad, as their own files do'
  assert max(float(b['x']) + float(b['w']) for b in data['bars']) <= height + 1


@needs_real
def test_the_onsets_are_seconds_in_the_order_the_music_is_played(real, tmp_path):
  cfg, piece = real
  summary = cyolo.export_piece(cfg, piece, tmp_path, timing='recording')
  data = _load(summary['npz'])
  onsets = sorted(float(c['onset']) for c in data['coords'])
  assert onsets[0] >= 0
  assert onsets[-1] < piece.audio.duration_s, 'no note may start after the recording ends'
  bar_length = (onsets[-1] - onsets[0]) / 203.0
  assert 1.0 < bar_length < 2.5, 'about a second and a half a bar, got %.2f' % bar_length


@needs_real
def test_every_note_carries_plain_python_values(real, tmp_path):
  """Their loader does `for key in entry: entry[key]`, which works on a dict and not on a
  numpy structured record; and a numpy scalar inside would be pickled by the numpy that
  wrote it, which the numpy 1.x in their environment cannot unpickle."""
  cfg, piece = real
  summary = cyolo.export_piece(cfg, piece, tmp_path, timing='recording')
  data = _load(summary['npz'])
  for row in list(data['coords'])[:50] + list(data['bars'])[:20] + list(data['systems'])[:5]:
    assert isinstance(dict(row), dict)
    for key, value in dict(row).items():
      assert isinstance(value, (int, float, str)), '%s is %r' % (key, type(value))
      assert dict(row)[key] == value


@needs_real
def test_a_piece_with_no_map_cannot_export_the_recording_timing(real, tmp_path):
  cfg, piece = real
  piece.sync = None
  with pytest.raises(cyolo.CyoloError) as caught:
    cyolo.export_piece(cfg, piece, tmp_path, timing='recording')
  assert 'no measure map' in str(caught.value)


def test_a_piece_with_no_score_is_refused(tmp_path):
  cfg = AnalyzerConfig()
  cfg.cache_dir = tmp_path / 'cache'
  cfg.output_dir = tmp_path / 'out'
  piece = library.Piece(slug='ghost', musicxml=None, title='Ghost')
  with pytest.raises(cyolo.CyoloError):
    cyolo.export_piece(cfg, piece, tmp_path, timing='recording')


@needs_real
def test_an_unknown_timing_is_refused(real, tmp_path):
  cfg, piece = real
  with pytest.raises(cyolo.CyoloError) as caught:
    cyolo.export_piece(cfg, piece, tmp_path, timing='whenever')
  assert 'synthetic' in str(caught.value)


@needs_real
def test_the_synthetic_timing_uses_the_practice_track(real, tmp_path):
  """The variant that isolates the score: clean rendered audio, exact bar grid."""
  import shutil

  from analyzer import align
  if shutil.which('fluidsynth') is None or align.find_soundfont() is None:
    pytest.skip('rendering a practice track needs fluidsynth and a soundfont')
  cfg, piece = real
  summary = cyolo.export_piece(cfg, piece, tmp_path, timing='synthetic')
  data = _load(summary['npz'])
  assert bool(data['synthesized']) is True
  onsets = sorted(float(c['onset']) for c in data['coords'])
  # the practice track is one uniform tempo, so the bars are evenly spaced
  per_bar = [b - a for a, b in zip(onsets, onsets[1:]) if b - a > 0.001]
  assert per_bar, 'no distinct onsets'
  assert max(per_bar) < 1.6, 'nothing should be more than a bar apart at 1.50 s per bar'
