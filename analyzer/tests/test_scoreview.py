"""Tests for the engraved score view: geometry, cache, and hit-testing.

The real piece is used where it exists, because the properties that matter here
are about *that* score: every one of its 204 bars must get a page and a rectangle,
and those rectangles must lie inside the page they belong to.
"""

import json
import pathlib
import sys

import pytest

from analyzer.config import AnalyzerConfig
from analyzer import scoreview

CHOPIN = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
needs_verovio = pytest.mark.skipif(
    not CHOPIN.is_file(), reason='the Chopin score is not on this machine')


@pytest.fixture
def cfg(tmp_path):
  config = AnalyzerConfig(cache_dir=tmp_path / 'cache', output_dir=tmp_path / 'out')
  config.ensure_dirs()
  return config


# ---------------------------------------------------------------------------
# pure parsing
# ---------------------------------------------------------------------------

def test_measure_ids_handles_either_attribute_order():
  """MEI does not promise an order, and `xml:id` came first in the version here."""
  id_first = '<measure xml:id="abc" n="7">'
  n_first = '<measure n="8" xml:id="def">'
  ids = scoreview.measure_ids(id_first + n_first)
  assert ids == {'abc': 7, 'def': 8}


def test_measure_ids_ignores_non_numeric_numbers():
  ids = scoreview.measure_ids('<measure n="X1" xml:id="a"><measure n="3" xml:id="b">')
  assert ids == {'b': 3}


def test_attribute_lookup_is_not_fooled_by_stroke_width():
  """`stroke-width="0"` must not answer a request for `width`."""
  assert scoreview._attr('x="1" width="400" stroke-width="0"', 'width') == '400'
  assert scoreview._attr('x="1" height="9"', 'width') is None


def test_viewbox_scale_converts_svg_units_to_pixels():
  svg = '<svg viewBox="0 0 21000 24100">'
  assert scoreview._viewbox_scale(svg, 840) == pytest.approx(0.04)
  assert scoreview._viewbox_scale('<svg width="840px" height="900px">', 420) == pytest.approx(0.5)
  assert scoreview._viewbox_scale('<svg>', 100) == 1.0


# ---------------------------------------------------------------------------
# the render itself
# ---------------------------------------------------------------------------

@needs_verovio
def test_every_measure_gets_a_page_and_a_rectangle(cfg):
  render = scoreview.render_score(cfg, CHOPIN)
  assert len(render.pages) == 9, 'the Chopin engraves to nine pages here'
  assert render.measure_count == 204
  numbers = sorted({b.number for p in render.pages for b in p.measures})
  assert numbers == list(range(1, 205)), 'every bar 1..204 must be clickable'

  for page in render.pages:
    assert pathlib.Path(page.image).is_file(), 'page image %s is missing' % page.image
    assert page.width > 200 and page.height > 200
    for box in page.measures:
      assert 0 <= box.x0 < box.x1 <= page.width + 1, 'bar %d box is off the page' % box.number
      assert 0 <= box.y0 < box.y1 <= page.height + 1, 'bar %d box is off the page' % box.number
      assert box.x1 - box.x0 > 20 and box.y1 - box.y0 > 10, 'bar %d box is implausibly small'


@needs_verovio
def test_bars_are_ordered_left_to_right_within_a_system(cfg):
  render = scoreview.render_score(cfg, CHOPIN)
  page = render.pages[0]
  first_system = [b for b in page.measures if abs(b.y0 - page.measures[0].y0) < 100]
  assert len(first_system) > 1
  assert [b.number for b in first_system] == sorted(b.number for b in first_system)
  assert [b.x0 for b in first_system] == sorted(b.x0 for b in first_system)


@needs_verovio
def test_the_cache_is_used_without_engraving_again(cfg, monkeypatch):
  scoreview.render_score(cfg, CHOPIN)
  manifest = scoreview.cache_dir(cfg, CHOPIN) / 'pages.json'
  assert manifest.is_file()
  stamp = manifest.stat().st_mtime

  def explode(*args, **kwargs):
    raise AssertionError('the cache should have been used, not engraving again')

  monkeypatch.setattr('cairosvg.svg2png', explode)
  again = scoreview.render_score(cfg, CHOPIN)
  assert again.measure_count == 204
  assert manifest.stat().st_mtime == stamp, 'the cache was rewritten'


@needs_verovio
def test_the_cache_is_dropped_when_the_score_changes(cfg, tmp_path):
  import shutil
  copy = tmp_path / 'piece.mxl'
  shutil.copy(CHOPIN, copy)
  render = scoreview.render_score(cfg, copy)
  assert render.measure_count == 204
  assert scoreview.cached_render(cfg, copy) is not None

  import os
  os.utime(copy, (copy.stat().st_atime, copy.stat().st_mtime + 60))
  assert scoreview.cached_render(cfg, copy) is None, 'a touched score must re-engrave'


@needs_verovio
def test_a_different_raster_scale_is_a_different_cache(cfg):
  scoreview.render_score(cfg, CHOPIN, scale=1.0)
  assert scoreview.cached_render(cfg, CHOPIN, scale=1.0) is not None
  assert scoreview.cached_render(cfg, CHOPIN, scale=2.0) is None


def test_a_missing_score_is_reported(cfg, tmp_path):
  with pytest.raises(scoreview.ScoreRenderError):
    scoreview.render_score(cfg, tmp_path / 'nope.musicxml')


def test_missing_tools_give_an_install_hint(monkeypatch):
  monkeypatch.setitem(sys.modules, 'verovio', None)
  with pytest.raises(scoreview.ScoreRenderError) as caught:
    scoreview._require_engines()
  assert 'pip install verovio' in str(caught.value)


# ---------------------------------------------------------------------------
# hit-testing and the lookups the UI needs
# ---------------------------------------------------------------------------

def synthetic_render():
  boxes_a = [scoreview.MeasureBox(number=n, x0=100.0 * (n - 1), y0=10.0,
                                  x1=100.0 * (n - 1) + 90, y1=60.0) for n in (1, 2, 3)]
  boxes_b = [scoreview.MeasureBox(number=n, x0=100.0 * (n - 4), y0=10.0,
                                  x1=100.0 * (n - 4) + 90, y1=60.0) for n in (4, 5, 6)]
  return scoreview.ScoreRender(
      slug='demo', source='demo.musicxml',
      pages=[scoreview.ScorePage(number=1, image='a.png', width=400, height=100, measures=boxes_a),
             scoreview.ScorePage(number=2, image='b.png', width=400, height=100, measures=boxes_b)])


def test_a_point_inside_a_box_selects_that_bar():
  render = synthetic_render()
  assert render.measure_at(1, 5.0, 30.0) == 1
  assert render.measure_at(1, 195.0, 30.0) == 2
  assert render.measure_at(2, 195.0, 30.0) == 5


def test_a_point_between_bars_selects_the_nearest_on_that_system():
  render = synthetic_render()
  # x=95 sits in the gap before bar 1's box ends at 90... but on the same row
  assert render.measure_at(1, 95.0, 30.0) == 1
  # just above the staff, still the same system
  assert render.measure_at(1, 195.0, 2.0) == 2


def test_lookups_by_measure_number():
  render = synthetic_render()
  page, box = render.box_of(5)
  assert page.number == 2 and box.number == 5
  assert render.page_of(6).number == 2
  assert render.box_of(99) is None
  assert render.page_of(99) is None
  assert render.measure_at(1, 10.0, 10_000.0) is not None      # never crashes


def test_round_trip_through_a_cache_dict(tmp_path):
  render = synthetic_render()
  payload = render.to_dict()
  restored = scoreview.ScoreRender.from_dict(json.loads(json.dumps(payload)))
  assert restored.measure_count == render.measure_count
  assert restored.box_of(3)[1] == render.box_of(3)[1]
  assert [p.number for p in restored.pages] == [1, 2]
