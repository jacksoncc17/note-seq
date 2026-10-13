"""What the page reveal knows, and what it must never let you read ahead.

The load-bearing property is negative and easy to get wrong quietly: **nothing that has
not been played may stay visible.** Three versions of this were written before it held,
each wrong in a way that looked fine:

* the bars whose measures span a page break put the boundary in the wrong place;
* a measure's layers shared one clock, so every bar after the first chord in it had the
  wrong beats (chords are events in MEI, not stacks on the previous note);
* the notes were assigned to systems by where their *ink* fell, which drops exactly the
  ledger-line notes -- and left 319 future noteheads visible on four pages.

So this checks the whole grid: every bar, every beat, on every page, against every
notehead in the engraving.
"""

import pathlib

import pytest

from analyzer import library, scoreview
from analyzer.config import AnalyzerConfig

SCORE = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
SLUG = 'chopin-waltz-in-a-minor-op-34-no-2'
needs_real = pytest.mark.skipif(
    not (SCORE.is_file() and AnalyzerConfig().sync_path(SLUG).is_file()),
    reason='the Chopin score and its rendering are needed')


@pytest.fixture(scope='module')
def real():
  cfg = AnalyzerConfig()
  piece = library.find(cfg, SLUG)
  render = scoreview.cached_render(cfg, piece.musicxml)
  if render is None:
    pytest.skip('the score has not been rendered')
  return cfg, piece, render


def _inside(box, rects) -> bool:
  x0, y0, x1, y1 = box
  return any(r[0] <= x0 + 0.5 and r[2] >= x1 - 0.5 and r[1] <= y0 + 0.5 and r[3] >= y1 - 0.5
             for r in rects)


def _overlap(box, rects) -> float:
  x0, y0, x1, y1 = box
  return sum(max(0.0, min(x1, r[2]) - max(x0, r[0]))
             for r in rects if r[1] < y1 and r[3] > y0)


# ---------------------------------------------------------------------------
# what the score says about its own pace
# ---------------------------------------------------------------------------

@needs_real
def test_every_notehead_knows_which_beat_it_begins_at(real):
  cfg, piece, render = real
  page = render.pages[1]
  reveal = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
  assert reveal.unit == 4, 'this score is in 3/4, so the unit is the quarter'
  assert reveal.marks, 'no noteheads were placed'
  assert all(m.unit >= 0 for m in reveal.marks)
  # 3/4, so three quarters to a bar -- except the twelve bars of this score that hold
  # three and a half (below), which get a fourth unit and deserve one.
  counts = {m.measure: reveal.units[m.measure] for m in reveal.marks}
  assert set(counts.values()) <= {3, 4}, counts
  assert sum(1 for u in counts.values() if u == 4) < 0.25 * len(counts), counts


@needs_real
def test_the_beat_a_note_begins_at_is_its_onset_not_its_position_on_the_page(real):
  """Chords are the trap: three chords in a 3/4 bar are beats 1, 2 and 3, not all 1."""
  cfg, piece, render = real
  page = render.pages[1]
  reveal = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
  bar = min(m.measure for m in reveal.marks) + 8          # bar 40 of this score: chords
  units = sorted({m.unit for m in reveal.marks if m.measure == bar})
  assert units == [0, 1, 2], 'bar %d should have notes on all three beats, got %s' % (bar, units)


@needs_real
def test_a_tuplet_does_not_shift_the_rest_of_the_bar(real):
  """Durations in the MEI are sounding values, already scaled for tuplets, so beats
  after a triplet still land on their own beat rather than drifting."""
  cfg, piece, render = real
  checked = 0
  for page in render.pages:
    reveal = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
    bad = [m for m in reveal.marks if m.unit >= reveal.units.get(m.measure, 0)]
    assert not bad, 'notes past the end of their bar: %s' % bad[:3]
    checked += len(reveal.marks)
  assert checked > 1000, 'only %d noteheads were placed' % checked


@needs_real
def test_the_odd_bars_of_this_score_are_the_scores_own(real):
  """Twelve bars here hold three and a half quarters under a 3/4 signature -- a
  fermata bar, or an editor's half beat. The reveal believes the score rather than the
  signature, so those bars have a fourth unit; this pins the count so that a change in
  it is noticed rather than shrugged at."""
  import zipfile
  import xml.etree.ElementTree as ET
  with zipfile.ZipFile(SCORE) as archive:
    root = ET.fromstring(archive.read('score.xml').decode('utf-8'))
  def local(tag): return tag.split('}')[-1]
  divisions, lengths = 480, []
  for measure in root.iter():
    if local(measure.tag) != 'measure':
      continue
    total = 0
    for child in measure:
      tag = local(child.tag)
      if tag == 'attributes':
        found = next((c for c in child if local(c.tag) == 'divisions'), None)
        if found is not None:
          divisions = int(found.text)
      elif tag == 'note' and not any(local(c.tag) == 'chord' for c in child):
        raw = next((c.text for c in child if local(c.tag) == 'duration'), None)
        total += int(raw or 0)
      elif tag == 'forward':
        raw = next((c.text for c in child if local(c.tag) == 'duration'), None)
        total += int(raw or 0)
      elif tag == 'backup':
        raw = next((c.text for c in child if local(c.tag) == 'duration'), None)
        total -= int(raw or 0)
    lengths.append(total / float(divisions))
  odd = [quarter for quarter in lengths if abs(quarter - 3.0) > 0.01]
  assert len(odd) == 12 and set(odd) == {3.5}, odd


@needs_real
def test_the_reveal_is_remembered_with_the_engraving(real):
  """It is worked out while the MEI is in hand, which is the only moment it can be:
  Verovio regenerates element ids on every engraving."""
  import time
  cfg, piece, render = real
  page = render.pages[1]
  scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
  start = time.perf_counter()
  again = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
  elapsed = time.perf_counter() - start
  assert again.marks, 'the second call must answer too'
  assert elapsed < 0.1, 'a remembered reveal took %.2fs -- it engraved again' % elapsed


# ---------------------------------------------------------------------------
# what must not be readable
# ---------------------------------------------------------------------------

@needs_real
def test_the_music_ahead_is_never_left_visible(real):
  """The whole point of the feature, checked over every bar and beat on every page."""
  cfg, piece, render = real
  leaks = []
  positions = 0
  for page in render.pages:
    reveal = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
    for bar in sorted({m.measure for m in reveal.marks}):
      for unit in range(max(1, reveal.units.get(bar, 1))):
        rects = reveal.rects(bar, unit)
        positions += 1
        for mark in reveal.marks:
          if (mark.measure, mark.unit) <= (bar, unit):
            continue
          if not _inside((mark.x0, mark.y0, mark.x1, mark.y1), rects):
            leaks.append((bar, unit, mark.measure, mark.unit))
  assert positions > 500, 'only %d positions were checked' % positions
  assert not leaks, '%d future noteheads stayed visible, e.g. %s' % (len(leaks), leaks[:4])


@needs_real
def test_the_reveal_reaches_further_as_the_music_plays(real):
  """Not just covered: uncovered progressively, in the order it is played."""
  cfg, piece, render = real
  page = render.pages[1]
  reveal = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
  bar = sorted({m.measure for m in reveal.marks})[3]
  edges = [reveal.rects(bar, unit)[0][0] for unit in range(reveal.units[bar])]
  assert edges == sorted(edges), 'the boundary went backwards: %s' % edges
  assert edges[0] < edges[-1], 'the boundary barely moved across the bar'


@needs_real
def test_the_line_being_played_keeps_its_own_music(real):
  """The curtain must not eat the bar it is revealing: those noteheads are visible."""
  cfg, piece, render = real
  page = render.pages[3]
  reveal = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
  bar = sorted({m.measure for m in reveal.marks})[5]
  rects = reveal.rects(bar, reveal.units[bar] - 1)          # the last beat: all of it played
  marks = [m for m in reveal.marks if m.measure == bar]
  hidden = [m for m in marks if _inside((m.x0, m.y0, m.x1, m.y1), rects)]
  assert not hidden, '%d of the bar being played were covered' % len(hidden)


@needs_real
def test_anything_covered_is_only_ever_covered_by_a_little(real):
  """A played notehead may be clipped at the boundary -- covering wins over clipping,
  because the future is what must not be readable -- but not swallowed repeatedly."""
  cfg, piece, render = real
  cut = 0
  total = 0
  for page in render.pages:
    reveal = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
    for bar in sorted({m.measure for m in reveal.marks}):
      for unit in range(max(1, reveal.units.get(bar, 1))):
        rects = reveal.rects(bar, unit)
        for mark in reveal.marks:
          if (mark.measure, mark.unit) > (bar, unit) or mark.x1 <= mark.x0:
            continue
          total += 1
          if _overlap((mark.x0, mark.y0, mark.x1, mark.y1), rects) > 0.5 * (mark.x1 - mark.x0):
            cut += 1
  assert total > 50000, total
  assert cut < 0.001 * total, '%d of %d played noteheads were more than half hidden' % (cut, total)


@needs_real
def test_nothing_is_covered_before_playback_starts(real):
  cfg, piece, render = real
  page = render.pages[0]
  reveal = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
  first = min(reveal.marks, key=lambda m: (m.measure, m.unit))
  assert reveal.rects(first.measure, first.unit)[0][0] > first.x0, \
      'the very first note of the piece must be visible'
  whole = reveal.rects(None, 0)
  assert whole == [(0.0, 0.0, reveal.width, reveal.height)], \
      'with nothing playing the whole page is covered'


@needs_real
def test_the_page_is_split_into_the_lines_a_player_reads(real):
  """Systems come from the staff boxes; the full extents overlap and would merge into
  one band covering the whole page."""
  cfg, piece, render = real
  page = render.pages[1]
  reveal = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
  assert len(reveal.systems) >= 3, reveal.systems
  bands = sorted(reveal.systems)
  for (top, bottom), (next_top, _next_bottom) in zip(bands, bands[1:]):
    assert bottom < next_top, 'bands overlap: %s' % reveal.systems
    assert bottom - top > 0.05 * reveal.height, 'a band is suspiciously thin'
