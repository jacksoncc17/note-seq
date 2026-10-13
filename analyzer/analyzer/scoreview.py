"""The engraved score, with a page and a rectangle for every measure.

The point of this module is that the *authoritative* MusicXML is what gets drawn,
so the bars on screen are the bars the map refers to. Optical recognition of the
user's PDF would only introduce disagreement; the PDF stays available as the
edition they actually read.

The pipeline, and why each step is what it is:

1. **Verovio** engraves the MusicXML to SVG, one page at a time. It is the only
   tool here that hands back *structure*: with `svgBoundingBoxes` it emits a
   bounding box for every element, so a measure can be located exactly
   (`<g class="measure">` contains the staff boxes; the measure *number* comes
   from the matching MEI `xml:id`).
2. **cairosvg** rasterises it to PNG. Qt cannot do this step: QtSvg fails to
   resolve Verovio's `<use xlink:href>` references into the `<defs>` glyph layer
   (glyphs are `<g><path>` there, not `<symbol>`), and on this machine it did not
   merely lose glyphs -- it segfaulted the process. cairosvg resolves them
   correctly; measured on a Chopin page, removing every glyph reference drops the
   ink from 8.8% to 7.3%, which is the notation layer, and removing the `<defs>`
   layer changes nothing, so nothing is drawn twice.
3. **Qt** just draws the PNG. No SVG interpreter in the display path at all.

Everything is cached under `cfg.render_dir/<slug>/`, keyed by the score's size and
mtime, so opening a piece is instant after the first render.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import math
import pathlib
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .config import AnalyzerConfig
from . import library


Progress = Optional[Callable[[float, str], None]]

CACHE_VERSION = 4      # 4 adds each bar's full ink extent, for cropping cards
RASTER_SCALE = 1.5          # PNG pixels per SVG pixel; ~1260 px wide pages
ENGRAVING_CACHE_SIZE = 8    # page engravings kept in memory; see page_svg_and_ids

# Verovio's internal ticks per quarter note. Durations in the MEI are given in these
# (`dur.ppq`), already scaled for tuplets: a triplet of eighths on this score is three
# 160s, which is 480, which is one quarter.
QUARTER_PPQ = 480

# Engraving a page is the slow part of both drawing the score and making a card, and
# the same page is asked for repeatedly -- so the last few are kept, with the musical
# positions of what they drew (which is only knowable at engraving time, when the MEI
# is in hand). Masking edits the SVG string, so one engraving serves every set of masks.
_Engraving = Tuple[str, Dict[str, str], 'PageReveal']
_PAGE_ENGRAVINGS: "collections.OrderedDict[str, _Engraving]" = collections.OrderedDict()


class ScoreRenderError(RuntimeError):
  """Raised when the score cannot be engraved, with a user-facing message."""


def _report(progress: Progress, percent: float, message: str) -> None:
  if progress is None:
    return
  try:
    progress(float(percent), message)
  except Exception:
    pass


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MeasureBox:
  """Where one measure sits, in PNG pixels on its page.

  Two rectangles, because they answer different questions. `x0..y1` is the staff:
  what to highlight for a bar, and what a click is tested against. `full_*` is the
  union of *everything* drawn in the bar -- leger-line notes above the staves,
  dynamics below them, slurs, tempo text -- which is what a crop must cover. Using
  the staff box for a crop clips the music: measured on this score, every passage
  cropped that way had ink running off its top and bottom edges.
  """

  number: int
  x0: float
  y0: float
  x1: float
  y1: float
  full_x0: float = 0.0
  full_y0: float = 0.0
  full_x1: float = 0.0
  full_y1: float = 0.0

  @property
  def has_full(self) -> bool:
    return self.full_x1 > self.full_x0 and self.full_y1 > self.full_y0

  @property
  def extent(self):
    """Everything drawn in the bar: the staff box if the full one is unknown."""
    if self.has_full:
      return (self.full_x0, self.full_y0, self.full_x1, self.full_y1)
    return (self.x0, self.y0, self.x1, self.y1)

  def contains(self, x: float, y: float, pad: float = 0.0) -> bool:
    return (self.x0 - pad) <= x <= (self.x1 + pad) and (self.y0 - pad) <= y <= (self.y1 + pad)

  def to_dict(self) -> Dict[str, float]:
    out = {'number': self.number, 'x0': round(self.x0, 1), 'y0': round(self.y0, 1),
           'x1': round(self.x1, 1), 'y1': round(self.y1, 1)}
    if self.has_full:
      out.update({'fx0': round(self.full_x0, 1), 'fy0': round(self.full_y0, 1),
                  'fx1': round(self.full_x1, 1), 'fy1': round(self.full_y1, 1)})
    return out


@dataclass(frozen=True)
class GlyphBox:
  """One drawable element: a notehead, a rest, an accidental.

  Notes are identified by where they sit in the score -- measure, staff, and
  ordinal within that staff -- rather than by Verovio's element id, because those
  ids are regenerated on every engraving and a saved mask has to survive one.
  """

  measure: int
  staff: int
  index: int
  kind: str                      # 'note' | 'rest' | 'accid'
  x0: float
  y0: float
  x1: float
  y1: float
  # Where the glyph is *drawn*, for hit-testing. Verovio's `note bounding-box` is a
  # staff-position box rather than a glyph box -- measured on this score, its median
  # ink coverage is 1.4% and 109 of 221 boxes are empty -- so tapping against it
  # misses the note. These come from the notehead's own transform, which is where
  # the glyph really is (median ink 10.4%).
  hit_x0: Optional[float] = None
  hit_y0: Optional[float] = None
  hit_x1: Optional[float] = None
  hit_y1: Optional[float] = None

  @property
  def key(self) -> str:
    """A stable handle: `53:2:note:4` is the 5th notehead of the left hand in bar 53.

    The kind is part of the key because ordinals are counted per kind -- without
    it a note and the stem beside it would share a key.
    """
    return '%d:%d:%s:%d' % (self.measure, self.staff, self.kind, self.index)

  @property
  def centre(self):
    return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

  @property
  def has_full(self) -> bool:
    return self.full_x1 > self.full_x0 and self.full_y1 > self.full_y0

  @property
  def extent(self):
    """Everything drawn in the bar: the staff box if the full one is unknown."""
    if self.has_full:
      return (self.full_x0, self.full_y0, self.full_x1, self.full_y1)
    return (self.x0, self.y0, self.x1, self.y1)

  def contains(self, x: float, y: float, pad: float = 0.0) -> bool:
    return (self.x0 - pad) <= x <= (self.x1 + pad) and (self.y0 - pad) <= y <= (self.y1 + pad)

  @property
  def has_hit(self) -> bool:
    return None not in (self.hit_x0, self.hit_y0, self.hit_x1, self.hit_y1)

  @property
  def hit_centre(self) -> Tuple[float, float]:
    """The point a tap should be judged against: the drawn glyph if known."""
    if self.has_hit:
      return ((self.hit_x0 + self.hit_x1) / 2.0, (self.hit_y0 + self.hit_y1) / 2.0)
    return self.centre

  def to_dict(self) -> Dict[str, object]:
    out: Dict[str, object] = {'m': self.measure, 's': self.staff, 'i': self.index,
                              'k': self.kind, 'x0': round(self.x0, 1),
                              'y0': round(self.y0, 1), 'x1': round(self.x1, 1),
                              'y1': round(self.y1, 1)}
    if self.has_hit:
      out['hx0'] = round(self.hit_x0, 1)
      out['hy0'] = round(self.hit_y0, 1)
      out['hx1'] = round(self.hit_x1, 1)
      out['hy1'] = round(self.hit_y1, 1)
    return out

  @classmethod
  def from_dict(cls, data: Dict[str, object]) -> 'GlyphBox':
    def optional(key):
      value = data.get(key)
      return float(value) if value is not None else None
    return cls(measure=int(data['m']), staff=int(data['s']), index=int(data['i']),
               kind=str(data.get('k', 'note')), x0=float(data['x0']), y0=float(data['y0']),
               x1=float(data['x1']), y1=float(data['y1']),
               hit_x0=optional('hx0'), hit_y0=optional('hy0'),
               hit_x1=optional('hx1'), hit_y1=optional('hy1'))


@dataclass
class ScorePage:
  """One engraved page: its image, and the measures and glyphs on it."""

  number: int
  image: str                     # path to the PNG
  width: int
  height: int
  measures: List[MeasureBox] = field(default_factory=list)
  glyphs: List[GlyphBox] = field(default_factory=list)

  def to_dict(self) -> Dict[str, object]:
    return {'number': self.number, 'image': self.image, 'width': self.width,
            'height': self.height, 'measures': [m.to_dict() for m in self.measures],
            'glyphs': [g.to_dict() for g in self.glyphs]}


@dataclass
class ScoreRender:
  """Every page of a piece, plus lookups the UI needs."""

  slug: str
  source: str
  pages: List[ScorePage] = field(default_factory=list)
  warnings: List[str] = field(default_factory=list)

  def __len__(self) -> int:
    return len(self.pages)

  @property
  def measure_count(self) -> int:
    return len(self._index())

  def _index(self) -> Dict[int, Tuple[ScorePage, MeasureBox]]:
    out: Dict[int, Tuple[ScorePage, MeasureBox]] = {}
    for page in self.pages:
      for box in page.measures:
        out.setdefault(box.number, (page, box))
    return out

  def page_of(self, measure: int) -> Optional[ScorePage]:
    found = self._index().get(measure)
    return found[0] if found else None

  def box_of(self, measure: int) -> Optional[Tuple[ScorePage, MeasureBox]]:
    return self._index().get(measure)

  def glyphs_in_measures(self, first: int, last: int) -> List[GlyphBox]:
    """Every glyph between two printed bar numbers, inclusive."""
    low, high = min(first, last), max(first, last)
    return [g for page in self.pages for g in page.glyphs if low <= g.measure <= high]

  def glyph_by_key(self, key: str) -> Optional[GlyphBox]:
    for page in self.pages:
      for glyph in page.glyphs:
        if glyph.key == key:
          return glyph
    return None

  def nearest_glyph(self, page_number: int, x: float, y: float, kinds=('note',),
                    staff: Optional[int] = None, measure: Optional[int] = None
                    ) -> Optional[GlyphBox]:
    """The glyph a tap was closest to, judged by where glyphs are *drawn*.

    Nearest-by-centre rather than containment, because containment depends on the
    box being right and these boxes are only approximate: every tap then picks
    something sensible instead of nothing.
    """
    page = next((p for p in self.pages if p.number == page_number), None)
    if page is None:
      return None
    found = [g for g in page.glyphs
             if g.kind in kinds
             and (staff is None or g.staff == staff)
             and (measure is None or g.measure == measure)]
    if not found:
      return None
    return min(found, key=lambda g: (g.hit_centre[0] - x) ** 2 + (g.hit_centre[1] - y) ** 2)

  def glyph_at(self, page_number: int, x: float, y: float, pad: float = 0.0) -> Optional[GlyphBox]:
    """The smallest glyph under a point, so a tap on a notehead picks the note
    rather than the measure behind it."""
    page = next((p for p in self.pages if p.number == page_number), None)
    if page is None:
      return None
    found = [g for g in page.glyphs if g.contains(x, y, pad)]
    if not found:
      return None
    return min(found, key=lambda g: (g.x1 - g.x0) * (g.y1 - g.y0))

  def measure_at(self, page_number: int, x: float, y: float) -> Optional[int]:
    """Which measure a click at (x, y) on a page belongs to.

    Falls back to the nearest measure on the same system, so a click between two
    bars or just above the staff still selects the obvious bar rather than
    nothing at all.
    """
    page = next((p for p in self.pages if p.number == page_number), None)
    if page is None or not page.measures:
      return None
    for box in page.measures:
      if box.contains(x, y):
        return box.number
    # nearest by horizontal distance, restricted to boxes overlapping this row
    same_row = [b for b in page.measures if b.y0 - 40 <= y <= b.y1 + 40]
    candidates = same_row or page.measures
    nearest = min(candidates, key=lambda b: min(abs(x - b.x0), abs(x - b.x1)))
    return nearest.number

  def to_dict(self) -> Dict[str, object]:
    return {'version': CACHE_VERSION, 'slug': self.slug, 'source': self.source,
            'warnings': self.warnings, 'pages': [p.to_dict() for p in self.pages]}

  @classmethod
  def from_dict(cls, data: Dict[str, object]) -> 'ScoreRender':
    pages = []
    for raw in data.get('pages') or []:
      boxes = [MeasureBox(number=int(m['number']), x0=float(m['x0']), y0=float(m['y0']),
                          x1=float(m['x1']), y1=float(m['y1']),
                          full_x0=float(m.get('fx0') or 0.0), full_y0=float(m.get('fy0') or 0.0),
                          full_x1=float(m.get('fx1') or 0.0), full_y1=float(m.get('fy1') or 0.0))
               for m in (raw.get('measures') or [])]
      glyphs = [GlyphBox.from_dict(g) for g in (raw.get('glyphs') or [])]
      pages.append(ScorePage(number=int(raw['number']), image=str(raw['image']),
                             width=int(raw['width']), height=int(raw['height']),
                             measures=boxes, glyphs=glyphs))
    return cls(slug=str(data.get('slug', '')), source=str(data.get('source', '')),
               pages=pages, warnings=list(data.get('warnings') or []))


# ---------------------------------------------------------------------------
# reading the score
# ---------------------------------------------------------------------------

def _score_xml(path) -> str:
  """MusicXML text, unpacking a compressed archive when there is one.

  Decided by content, not by extension: a `.mxl` renamed to `.musicxml` (or the
  reverse) used to fail with a baffling "no measures were found", because the reader
  trusted the name.
  """
  path = pathlib.Path(path)
  with open(path, 'rb') as handle:
    magic = handle.read(4)
  if magic[:2] == b'PK' or path.suffix.lower() in ('.mxl', '.zip'):
    with zipfile.ZipFile(path) as z:
      names = [n for n in z.namelist()
               if n.lower().endswith(('.xml', '.musicxml')) and 'META-INF' not in n]
      if not names:
        raise ScoreRenderError('%s contains no MusicXML part' % path.name)
      return z.read(names[0]).decode('utf-8', 'replace')
  return path.read_text(encoding='utf-8', errors='replace')


def _attr(tag: str, key: str) -> Optional[str]:
  # exact attribute match: `stroke-width` must not satisfy a request for `width`
  m = re.search(r'(?<![-\w])%s="([^"]*)"' % key, tag)
  return m.group(1) if m else None


def measure_ids(mei: str) -> Dict[str, int]:
  """Map MEI `xml:id` to measure number.

  Attribute order in MEI output is not fixed (`xml:id` came before `n` in the
  version checked here), so each tag is parsed independently rather than with one
  regex that assumes an order.
  """
  out: Dict[str, int] = {}
  for tag in re.findall(r'<measure\b[^>]*>', mei):
    number, xml_id = _attr(tag, 'n'), _attr(tag, 'xml:id')
    if not xml_id or number is None:
      continue
    digits = re.match(r'\s*(\d+)', number)
    if digits:
      out[xml_id] = int(digits.group(1))
  return out


# Elements Verovio actually draws, for working out how far a bar's ink reaches.
# Deliberately excludes containers such as layer/staff/measure/system.
DRAWN_KINDS = ('note', 'rest', 'accid', 'stem', 'beam', 'dots', 'slur', 'tie', 'dynam',
               'hairpin', 'tempo', 'text', 'tuplet', 'tupletNum', 'mordent', 'trill',
               'grpSym', 'keySig', 'meterSig', 'clef')


def _measure_geometry(svg: str, ids: Dict[str, int]) -> Tuple[List[MeasureBox], List[str]]:
  """Measure boxes from a rendered page, in SVG user units."""
  warnings: List[str] = []
  boxes: List[MeasureBox] = []
  pattern = re.compile(r'<g id="(\w+)" class="measure">(.*?)(?=<g id="\w+" class="measure">|</svg>)',
                       re.S)
  for match in pattern.finditer(svg):
    xml_id, body = match.group(1), match.group(2)
    number = ids.get(xml_id)
    if number is None:
      continue
    rects = []
    for attrs in re.findall(r'<g id="bbox-\w+" class="staff bounding-box">\s*<rect\s([^>]*)>', body):
      values = {k: _attr(attrs, k) for k in ('x', 'y', 'width', 'height')}
      if all(v is not None for v in values.values()):
        rects.append(tuple(float(values[k]) for k in ('x', 'y', 'width', 'height')))
    if not rects:
      # a measure with no notes at all still needs a place to be clicked
      warnings.append('measure %d has no staff geometry on this page' % number)
      continue
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    # The extent is the union of the elements that are *drawn*, an allow-list on
    # purpose: Verovio also emits boxes for containers (layer, staff, measure), and
    # unioning those swept in whole systems -- one bar came out ten times too tall.
    drawn = list(rects)
    for kind in DRAWN_KINDS:
      for element in re.findall(
          r'<g id="\w+" class="%s">(.*?)(?=<g id="\w+" class="[a-zA-Z]+">|$)' % kind,
          body, re.S):
        rect = re.search(r'<rect\s([^>]*)>', element)
        if rect is None:
          continue
        values = {k: _attr(rect.group(1), k) for k in ('x', 'y', 'width', 'height')}
        if any(v is None for v in values.values()):
          continue
        drawn.append((float(values['x']), float(values['y']),
                      float(values['width']), float(values['height'])))
    full = None
    if drawn:
      full = (min(r[0] for r in drawn), min(r[1] for r in drawn),
              max(r[0] + r[2] for r in drawn), max(r[1] + r[3] for r in drawn))
    boxes.append(MeasureBox(number=number, x0=x0, y0=y0, x1=x1, y1=y1,
                            full_x0=full[0] if full else 0.0,
                            full_y0=full[1] if full else 0.0,
                            full_x1=full[2] if full else 0.0,
                            full_y1=full[3] if full else 0.0))
  return boxes, warnings


NOTEHEAD_WINDOW = (-120.0, -220.0, 320.0, 160.0)   # around the glyph's origin, in SVG units


def _glyph_hit(element: str) -> Optional[Tuple[float, float, float, float]]:
  """The drawn extent of a notehead, from its own `<use>` transform.

  The window is asymmetric because the glyph is drawn from its origin outwards; the
  numbers were chosen by measuring ink coverage for 221 noteheads on a real page and
  keeping the best of five candidates (72% of them contain ink, against 51% for the
  naive window and 1.4% median for Verovio's own box).
  """
  use = re.search(r'<use[^>]*transform="translate\(([-\d.]+),\s*([-\d.]+)\)', element)
  if use is None:
    return None
  x, y = float(use.group(1)), float(use.group(2))
  dx0, dy0, dx1, dy1 = NOTEHEAD_WINDOW
  return (x + dx0, y + dy0, x + dx1, y + dy1)


def glyph_elements(svg: str, ids: Dict[str, int]) -> List[Tuple[GlyphBox, str]]:
  """The drawable elements of a page, in stable coordinates.

  Ordinals are assigned per (measure, staff) in document order, which is musical
  order, so `53:2:4` means the same note after a re-engraving -- the thing that
  lets a saved mask keep pointing at the note the user tapped.
  """
  glyphs: List[Tuple[GlyphBox, str]] = []
  measure_pattern = re.compile(
      r'<g id="(\w+)" class="measure">(.*?)(?=<g id="\w+" class="measure">|</svg>)', re.S)
  for match in measure_pattern.finditer(svg):
    number = ids.get(match.group(1))
    if number is None:
      continue
    body = match.group(2)
    staves = re.findall(r'<g id="\w+" class="staff">(.*?)(?=<g id="\w+" class="staff">|$)',
                        body, re.S)
    for staff_index, staff_body in enumerate(staves, start=1):
      counters: Dict[str, int] = {}
      for kind in ('note', 'rest', 'accid', 'stem', 'beam', 'dots'):
        for element_id, element in re.findall(
            r'<g id="(\w+)" class="%s">(.*?)(?=<g id="\w+" class="[a-zA-Z]+">|$)' % kind,
            staff_body, re.S):
          hit = _glyph_hit(element) if kind == 'note' else None
          rect = re.search(r'<rect\s([^>]*)>', element)
          if rect is None:
            continue
          values = {k: _attr(rect.group(1), k) for k in ('x', 'y', 'width', 'height')}
          if any(v is None for v in values.values()):
            continue
          counters[kind] = counters.get(kind, 0) + 1
          glyphs.append((GlyphBox(
              measure=number, staff=staff_index, index=counters[kind] - 1, kind=kind,
              x0=float(values['x']), y0=float(values['y']),
              x1=float(values['x']) + float(values['width']),
              y1=float(values['y']) + float(values['height']),
              hit_x0=hit[0] if hit else None, hit_y0=hit[1] if hit else None,
              hit_x1=hit[2] if hit else None, hit_y1=hit[3] if hit else None),
              element_id))
  return glyphs


def page_svg_and_ids(cfg: AnalyzerConfig, musicxml, page_number: int,
                     scale: float = RASTER_SCALE,
                     xml_text: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
  """Engrave one page again, and say which element is which glyph.

  Masking works on the SVG rather than on the finished pixels: an element can be
  hidden with `display: none`, which the rasteriser honours, so a masked notehead
  simply is not drawn. The alternative -- painting a rectangle over the note's
  bounding box -- does not work, because Verovio's note box is tighter than the
  drawn glyph (77 of 221 note boxes on one page contain no ink at all), so the patch
  lands beside the note and the note stays visible.

  The SVG and the element ids must come from the *same* engraving: Verovio
  regenerates ids every time, so asking a second instance for the MEI produces ids
  that match nothing and the mask silently does nothing at all.

  Engraving is the slowest step in making a card (about 300 ms a page on this score),
  and the same page is asked for again and again -- both sides of a card, the score
  view, and every keystroke in the card dialog. The result is therefore kept, along
  with the musical position of everything it drew (see `page_reveal`), since the MEI
  that says where the notes are only exists during the engraving. Callers must treat
  what they get back as read-only; `hide_elements` and `hide_classes` both return a
  new string rather than changing this one.

  `scale` is accepted and ignored: Verovio's own scale is pinned at 40 so every
  engraving lays the score out identically, and size comes from the rasteriser. It
  stays in the signature because callers pass the raster scale they will use.

  Returns:
    (svg, {glyph key: element id}) for that page.
  """
  svg, ids, _reveal = _engraved_page(cfg, musicxml, page_number, xml_text)
  return svg, ids


def _engraved_page(cfg: AnalyzerConfig, musicxml, page_number: int,
                   xml_text: Optional[str] = None):
  """(svg, ids, reveal) for one page, engraving it only if it is not remembered."""
  # `xml_text` lets a caller engrave a modified score (fingerings added) without any
  # file existing: Verovio takes the document as a string
  text = xml_text if xml_text is not None else _score_xml(pathlib.Path(musicxml))
  key = '%s|%s|%s' % (musicxml, page_number, hashlib.sha1(text.encode('utf-8')).hexdigest())
  remembered = _PAGE_ENGRAVINGS.get(key)
  if remembered is not None:
    _PAGE_ENGRAVINGS.move_to_end(key)
    return remembered
  engine = verovio_module().toolkit()
  # Verovio's own `scale` must stay at the value the cached geometry was engraved
  # with: it changes the *layout* (how many bars fit on a system), so raising it to
  # get a bigger image would put every measure somewhere else and the crop
  # rectangles would no longer line up. Bigger pixels come from the rasteriser.
  engine.setOptions({'pageWidth': 2100, 'scale': 40,
                     'adjustPageHeight': True, 'svgBoundingBoxes': True,
                     'header': 'none', 'footer': 'none'})
  engine.loadData(text)
  svg = engine.renderToSVG(page_number)
  mei = engine.getMEI()
  numbers = measure_ids(mei)                      # MEI id -> measure number
  ids = element_ids(svg, numbers)                 # glyph key -> element id
  reveal = _reveal_of(svg, numbers, mei, page_number)
  drawn = (svg, ids, reveal)
  _PAGE_ENGRAVINGS[key] = drawn
  while len(_PAGE_ENGRAVINGS) > ENGRAVING_CACHE_SIZE:
    _PAGE_ENGRAVINGS.popitem(last=False)
  return drawn


# ---------------------------------------------------------------------------
# what has not been played yet
# ---------------------------------------------------------------------------

# The concealment margins are measured in notehead heights, which is the one distance
# an engraving keeps about the same on any instrument (a notehead is about one staff
# space tall). On this score the median notehead is 11.5 px at the render's scale, and
# a bar's staff box is 15 noteheads tall.
#
# How far left of a notehead its own accidentals and dots reach -- measured on this
# score: median 0.96 noteheads, largest 1.19. Anything smaller than this leaks a sharp
# or a flat into the concealed part of the page, which is the one thing the
# concealment exists to prevent.
REVEAL_ACCIDENTALS = 1.25
# ... and how much clear space to leave after the last thing played, so the boundary
# does not look like a note was sliced in half.
REVEAL_GAP = 0.35


@dataclass
class BeatMark:
  """One drawn element that begins at a musical position.

  `unit` counts in the time signature's own denominator -- quarters in 3/4, eighths in
  6/8 -- which is the finest thing a player reads by. It comes from the score's own
  durations, not from anything guessed off the picture.
  """

  measure: int
  unit: int
  x0: float
  y0: float
  x1: float
  y1: float
  kind: str = 'note'


@dataclass
class PageReveal:
  """Where the music on one page begins, for concealing what has not been reached.

  Everything is in the page image's own pixels, so a caller can paint over it.

  `coarse` lists the measures whose durations did not add up to their own bar -- odd
  tuplets, a damaged measure -- where only the bar itself can be trusted as a position.
  That is the fallback the feature falls back to: a whole-measure reveal.
  """

  page: int
  width: float
  height: float
  unit: int = 4                                          # the meter's denominator
  space: float = 0.0                                     # a notehead's height, in pixels
  marks: List[BeatMark] = field(default_factory=list)
  systems: List[Tuple[float, float]] = field(default_factory=list)
  units: Dict[int, int] = field(default_factory=dict)     # measure -> units in the bar
  coarse: List[int] = field(default_factory=list)

  def unit_of(self, measure: int, seconds: float, span: Tuple[float, float]) -> int:
    """How far into a bar the clock is, from that bar's own [start, end).

    A performance does not announce its beats, so the units inside a bar are taken to
    be evenly spaced across the time the map gives that bar. That is the one estimate
    in this feature, and it is why the reveal can be switched to whole bars.
    """
    count = self.units.get(measure) or 0
    start, end = span
    if count <= 1 or end <= start:
      return 0
    fraction = (float(seconds) - start) / (end - start)
    return max(0, min(count - 1, int(fraction * count)))

  def rects(self, measure: Optional[int], unit: int) -> List[Tuple[float, float, float, float]]:
    """The rectangles to paint over: the rest of this line, and every line below it.

    Reading order is what makes this concealment rather than filtering. Within the
    system being played, everything from the next unplayed note onwards goes; and
    everything below that system goes entirely, down to the foot of the page. Music
    already played is left exactly as it is -- it is the future that must not be
    readable.

    The horizontal boundary sits between two rules, whichever is further right, so
    that it neither shows the next note nor slices the last one:

      * never left of `REVEAL_GAP` past the last thing reached;
      * never right of `REVEAL_ACCIDENTALS` before the next thing, so the sharps and
        flats that belong to it -- engraved well to its left -- go with it.

    Vertically a curtain reaches from the bottom of the system *above* (so nothing
    that belongs to the line being played is left sticking out) down to the top of the
    next one, and the remaining systems are covered outright.
    """
    whole = [(0.0, 0.0, self.width, self.height)]
    if measure is None or not self.systems or not self.marks:
      return whole
    index = self._system_index(measure)
    if index is None:
      return whole
    top, bottom = self.systems[index]
    # Which line a mark is on is decided by its *measure*, not by where its ink falls:
    # a high note is drawn above the staff box and a low one below, so testing the ink
    # against the band drops exactly the ledger-line notes -- which is how a future
    # notehead stayed visible in the first version of this.
    on_line = [m for m in self.marks if self._system_index(m.measure) == index]
    played = [m for m in on_line if (m.measure, m.unit) <= (measure, unit)]
    ahead = [m for m in on_line if (m.measure, m.unit) > (measure, unit)]

    height = self.space or (bottom - top) / 15.0
    # One boundary per line, and it covers the future rather than protecting the past:
    # the two hands do not agree about x order -- in bar 7 of this score the left hand
    # reaches beat 3 at x=203 while the right hand is still on beat 2 at x=188 -- so a
    # boundary placed after the last *played* note lets a future note show. It is put
    # `REVEAL_ACCIDENTALS` before the next thing instead, which can clip the right edge
    # of a notehead that has just been played, and that is the right way round: the
    # feature exists so the next notes cannot be read.
    if ahead:
      edge = min(m.x0 for m in ahead) - REVEAL_ACCIDENTALS * height
    elif played:
      edge = max(m.x1 for m in played) + REVEAL_GAP * height
    else:
      edge = 0.0
    edge = max(0.0, min(edge, self.width))

    # Vertically the curtain is drawn between the *ink*, not the staff boxes: a high
    # note of the next system is written above its staves, so a curtain ending at the
    # next staff box lets it stick out into view -- measured on this score, 319 of the
    # noteheads on four pages did exactly that.
    ink = self.system_ink()
    above = ink[index - 1][1] if index > 0 else 0.0
    if index + 1 < len(self.systems):
      below = ink[index + 1][0] - 1.5 * height
      # never so high that it eats the line being played
      below = max(below, ink[index][1] + 0.5 * height)
    else:
      below = self.height
    below = min(max(below, 0.0), self.height)
    out = [(edge, max(0.0, above), self.width, below)]
    if index + 1 < len(self.systems):
      out.append((0.0, below, self.width, self.height))
    return out

  def system_ink(self) -> List[Tuple[float, float]]:
    """The topmost and bottommost drawn music of each system, in page pixels.

    A caller showing one line at a time wants this: the air it can show above and
    below a system is bounded by where the neighbouring line's music starts.
    """
    ink = [[self.height, 0.0] for _ in self.systems]
    for mark in self.marks:
      index = self._system_index(mark.measure)
      if index is None:
        continue
      ink[index][0] = min(ink[index][0], mark.y0)
      ink[index][1] = max(ink[index][1], mark.y1)
    return [(top, bottom if bottom > 0 else top) for top, bottom in ink]

  def system_band(self, measure: int) -> Optional[Tuple[float, float]]:
    """The band of staves a measure is written on -- one line of the page.

    What a caller wants to show a single system at a time, rather than a whole page
    with the lines above and below the one being played.
    """
    index = self._system_index(measure)
    if index is None or index >= len(self.systems):
      return None
    return self.systems[index]

  def system_number(self, measure: int) -> Optional[int]:
    """Which line of the page this measure is on, counting from 1."""
    index = self._system_index(measure)
    return None if index is None else index + 1

  def _system_index(self, measure: int) -> Optional[int]:
    """Which line of the page this measure is written on."""
    bounds = self._measure_bounds(measure)
    if bounds is None:
      return None
    for index, (top, bottom) in enumerate(self.systems):
      if bounds[1] >= top - 1 and bounds[3] <= bottom + 1:
        return index
    # a measure whose marks sit outside every band still belongs to the nearest one
    return min(range(len(self.systems)),
               key=lambda i: abs(self.systems[i][0] - bounds[1])) if self.systems else None

  def _measure_bounds(self, measure: int):
    xs = [(m.x0, m.y0, m.x1, m.y1) for m in self.marks if m.measure == measure]
    if not xs:
      return None
    return (min(b[0] for b in xs), min(b[1] for b in xs),
            max(b[2] for b in xs), max(b[3] for b in xs))


def line_bands(reveal: PageReveal, image_path, pad: float = 4.0):
  """The band to show for each system: all of its music, and none of the neighbours'.

  A staff box is not the music. Measured on this score, the ink reaches 73 px above the
  staves and 82 px below them -- tempo marks and slurs above, dynamics and low ledger
  notes below -- so a band cut to the staves (plus a couple of notehead heights, which is
  what this first did) clips real music off the top and the bottom of every line.

  The page's rows are therefore divided between the lines *first*, at the midpoint
  between each pair of staff boxes, and each line's band is the ink inside its own
  share. Dividing first is what makes the result trustworthy: a row of ink belongs to the
  line whose staves are nearer to it, every inked row lands in exactly one band, and no
  band can reach into the next line's share however far a slur or a dynamic hangs. The
  line below is the future, and showing it is the one thing this must not do.

  Returns:
    One `(top, bottom)` per system in `reveal.systems`, in page pixels, in the same
    order -- and non-overlapping, with every inked row of the page inside one of them.
  """
  import numpy as np
  from PIL import Image

  systems = reveal.systems
  if not systems:
    return []
  with Image.open(image_path) as drawn:
    grey = np.asarray(drawn.convert('L'))
  inked = np.where((grey < 200).any(axis=1))[0]
  height = grey.shape[0]
  if not len(inked):
    return [(top, bottom) for top, bottom in systems]

  def middle(a: float, b: float) -> int:
    return int(max(0.0, min(float(height - 1), (a + b) / 2.0)))

  bands = []
  for index, (top, bottom) in enumerate(systems):
    ceiling = middle(systems[index - 1][1], top) if index > 0 else 0
    floor = middle(bottom, systems[index + 1][0]) if index + 1 < len(systems) else height - 1
    mine = inked[(inked >= ceiling) & (inked <= floor)]
    if not len(mine):
      bands.append((float(ceiling), float(floor)))
      continue
    bands.append((max(float(ceiling), float(mine[0]) - pad),
                  min(float(floor), float(mine[-1]) + pad)))
  return bands


def _systems_of(boxes: List[MeasureBox], height: int) -> List[Tuple[float, float]]:
  """The horizontal bands of a page: the staves a reader takes in one line at a time.

  Clustered on the *staff* boxes, not the full extents. A system's bars share a staff
  box, and the next system's is clearly below it; but the full extents of neighbouring
  bars overlap freely -- bar 34 of this score reports one reaching to y=0, the top of
  the page -- which would merge every system into one band and hide the whole page.
  """
  if not boxes:
    return [(0.0, float(height))]
  bands: List[List[float]] = []
  for box in sorted(boxes, key=lambda b: b.y0):
    if bands and box.y0 <= bands[-1][1] - 1:
      bands[-1][1] = max(bands[-1][1], box.y1)
    else:
      bands.append([box.y0, box.y1])
  return [(max(0.0, top), min(float(height), bottom)) for top, bottom in bands]


def _reveal_of(svg: str, numbers: Dict[str, int], mei: str, page_number: int) -> PageReveal:
  """Where every drawn note on this page begins, musically and on paper.

  The MEI is only in hand during the engraving, so this is worked out then and kept
  with the engraving: the join between the two -- SVG element id to musical position --
  cannot be made afterwards, because Verovio regenerates ids on every engraving.
  """
  unit_ppq, denominator = _meter_of(mei)
  positions, units, coarse = _positions_of(mei, numbers, unit_ppq)
  boxes, _warnings = _measure_geometry(svg, numbers)
  marks: List[BeatMark] = []
  for glyph, element_id in glyph_elements(svg, numbers):
    if glyph.kind not in ('note', 'rest'):
      continue
    where = positions.get(element_id)
    if where is None:
      continue
    number, unit = where
    marks.append(BeatMark(measure=number, unit=unit, x0=glyph.x0, y0=glyph.y0,
                          x1=glyph.x1, y1=glyph.y1, kind=glyph.kind))
  # A notehead is about one staff space tall whatever the instrument, so its height is
  # the unit the concealment margins are measured in.
  heads = sorted(m.y1 - m.y0 for m in marks if m.kind == 'note' and m.y1 > m.y0)
  space = heads[len(heads) // 2] if heads else 0.0
  return PageReveal(page=page_number, width=0.0, height=_viewbox_height(svg),
                    unit=denominator, space=space, marks=marks,
                    systems=_systems_of(boxes, int(_viewbox_height(svg))),
                    units=units, coarse=coarse)


def _meter_of(mei: str) -> Tuple[float, int]:
  """(ticks per denominator unit, the denominator) from the score's meter.

  The denominator is what the feature reveals by: the note value the bar is counted in.
  """
  for match in re.finditer(r'<meterSig\b[^>]*>', mei):
    unit = re.search(r'unit="(\d+)"', match.group(0))
    if unit:
      denominator = max(1, int(unit.group(1)))
      return (QUARTER_PPQ * 4.0 / denominator, denominator)
  return (float(QUARTER_PPQ), 4)


def _positions_of(mei: str, ids: Dict[str, int], unit_ppq: float):
  """{element id: (measure, unit)} for everything with a duration, plus what is odd.

  Two things about MEI that this has to get right, both of which were wrong first time
  and both of which are silent if you get them wrong:

  * **Durations are sounding values**, already scaled for tuplets: three eighths of a
    triplet are 160 ticks each, so onsets are a plain running sum. (480 ticks to the
    quarter -- Verovio's internal resolution.)
  * **A `<chord>` is an event, not a container of simultaneous notes**, and its notes
    are drawn from *their own* ids, not the chord's. So the chord advances the clock
    once by its duration, and every note inside it takes that same onset -- reading it
    as "stack on the previous note" (which is what MusicXML's `<chord/>` means) gives
    every chord in a bar the same position.

  Each layer is clocked separately, since layers are simultaneous, not successive.
  """
  positions: Dict[str, Tuple[int, int]] = {}
  units: Dict[int, int] = {}
  coarse: List[int] = []
  XML_ID = '{http://www.w3.org/XML/1998/namespace}id'
  try:
    root = ET.fromstring(mei)
  except ET.ParseError:
    return positions, units, coarse
  for measure in root.iter():
    if _local_name(measure.tag) != 'measure':
      continue
    number = ids.get(measure.get(XML_ID) or '')
    if number is None:
      continue
    longest = 0.0

    def unit_at(onset: float) -> int:
      """The unit a note *begins* in -- floor, not nearest.

      Rounding looks harmless and is not: the last third of a triplet in the third beat
      of a 3/4 bar sits at 2.67 quarters, which rounds to unit 3 -- a beat that does not
      exist, and which left a future notehead visible past the end of every such bar.
      """
      return max(0, int(onset // unit_ppq))

    def record(element, onset: float, clock: list):
      """One sounding event: give it a position and let time pass."""
      element_id = element.get(XML_ID)
      if element_id:
        positions[element_id] = (number, unit_at(onset))
      clock[0] = onset + _ppq(element)

    def walk(container, clock: list):
      for child in container:
        tag = _local_name(child.tag)
        if tag == 'chord':
          onset = clock[0]
          for note in child:
            if _local_name(note.tag) == 'note':
              record(note, onset, clock)
          clock[0] = onset + _ppq(child)
          element_id = child.get(XML_ID)
          if element_id:
            positions[element_id] = (number, unit_at(onset))
        elif tag == 'note':
          record(child, clock[0], clock)
        elif tag in ('rest', 'space', 'mRest'):
          record(child, clock[0], clock)
        elif tag in ('beam', 'tuplet', 'ftrem', 'btrem', 'graceGrp'):
          walk(child, clock)
        # clefs, keys, directions and dynamics take no time

    for staff in measure:
      if _local_name(staff.tag) != 'staff':
        continue
      for layer in staff:
        if _local_name(layer.tag) != 'layer':
          continue
        clock = [0.0]
        walk(layer, clock)
        longest = max(longest, clock[0])
    # The bar's length is the longer of what its layers add up to and what its notes
    # actually use, so no note can sit past the end of its own bar however odd the
    # measure is -- which is what keeps the concealment total.
    used = max((unit for (measure, unit) in positions.values() if measure == number),
               default=-1)
    span = max(1, int(math.ceil(longest / unit_ppq - 1e-6)), used + 1)
    units[number] = span
    if longest <= 0:
      coarse.append(number)
  return positions, units, coarse


def _ppq(element) -> float:
  """An element's sounding duration in ticks; 0 when it has none."""
  raw = element.get('dur.ppq')
  if raw is None:
    return 0.0
  try:
    return max(0.0, float(raw))
  except ValueError:
    return 0.0


def _local_name(tag: str) -> str:
  return tag.split('}')[-1]


def page_reveal(cfg: AnalyzerConfig, musicxml, page_number: int, width: int,
                xml_text: Optional[str] = None) -> PageReveal:
  """What this page would reveal, and what it must keep hidden, at `width` pixels.

  The marks are scaled from the engraving into the page image's own pixels with the
  same factor the render cache used, so a caller can paint straight onto the image.
  """
  svg, _ids, reveal = _engraved_page(cfg, musicxml, page_number, xml_text)
  factor = _viewbox_scale(svg, width)
  return dataclasses.replace(
      reveal,
      width=float(width), height=reveal.height * factor, space=reveal.space * factor,
      marks=[dataclasses.replace(m, x0=m.x0 * factor, y0=m.y0 * factor,
                                 x1=m.x1 * factor, y1=m.y1 * factor)
             for m in reveal.marks],
      systems=[(top * factor, bottom * factor) for top, bottom in reveal.systems])


def _viewbox_height(svg: str) -> float:
  """The engraving's own height, in SVG user units."""
  match = re.search(r'viewBox="([-\d.eE ]+)"', svg)
  if match:
    parts = match.group(1).split()
    if len(parts) >= 4:
      try:
        return float(parts[3])
      except ValueError:
        pass
  return 0.0


def element_ids(svg: str, ids: Dict[str, int]) -> Dict[str, str]:
  """glyph key -> element id, for the glyphs in this SVG.

  Only meaningful for the SVG it was computed from, since Verovio regenerates ids
  on every engraving -- which is why masks are stored as positions, not ids.
  """
  return {glyph.key: element_id for glyph, element_id in glyph_elements(svg, ids)}


def hide_classes(svg: str, classes) -> str:
  """Suppress every element of a class, e.g. the engraved fingerings (`fing`)."""
  wanted = [c for c in classes if c]
  if not wanted:
    return svg
  css = ''.join('g.%s { display: none; }' % name for name in wanted)
  return svg.replace('</style>', css + '</style>', 1)


def hide_elements(svg: str, element_ids_to_hide) -> str:
  """Return the SVG with those elements suppressed.

  The stylesheet is appended to Verovio's own `<style>` block so it wins on order.
  """
  wanted = [e for e in element_ids_to_hide if e]
  if not wanted:
    return svg
  css = ''.join('#%s { display: none; }' % element for element in wanted)
  return svg.replace('</style>', css + '</style>', 1)


def verovio_module():
  """The verovio module, with the install hint if it is missing."""
  try:
    import verovio
  except ImportError as e:
    raise ScoreRenderError('the score view needs verovio: .venv/bin/pip install verovio (%s)' % e)
  return verovio


def _scale_glyphs(glyphs: List[GlyphBox], factor: float) -> List[GlyphBox]:
  def scaled(value):
    return value * factor if value is not None else None
  return [dataclasses.replace(g, x0=g.x0 * factor, y0=g.y0 * factor,
                              x1=g.x1 * factor, y1=g.y1 * factor,
                              hit_x0=scaled(g.hit_x0), hit_y0=scaled(g.hit_y0),
                              hit_x1=scaled(g.hit_x1), hit_y1=scaled(g.hit_y1))
          for g in glyphs]


def _scale_boxes(boxes: List[MeasureBox], factor: float) -> List[MeasureBox]:
  return [dataclasses.replace(b, x0=b.x0 * factor, y0=b.y0 * factor,
                              x1=b.x1 * factor, y1=b.y1 * factor,
                              full_x0=b.full_x0 * factor, full_y0=b.full_y0 * factor,
                              full_x1=b.full_x1 * factor, full_y1=b.full_y1 * factor)
          for b in boxes]


def _require_engines():
  try:
    import verovio
  except ImportError as e:
    raise ScoreRenderError(
        'the score view needs verovio to engrave the MusicXML: '
        '.venv/bin/pip install verovio (%s)' % e)
  try:
    import cairosvg
  except ImportError as e:
    raise ScoreRenderError(
        'the score view needs cairosvg to turn the engraving into images: '
        '.venv/bin/pip install cairosvg (%s)' % e)
  return verovio, cairosvg


def _viewbox_scale(svg: str, png_width: int) -> float:
  """PNG pixels per SVG user unit."""
  m = re.search(r'viewBox="([-\d.eE ]+)"', svg)
  if m:
    parts = m.group(1).split()
    if len(parts) >= 3:
      try:
        width = float(parts[2])
        if width > 0:
          return png_width / width
      except ValueError:
        pass
  m = re.search(r'width="([\d.]+)px"', svg)
  if m:
    try:
      width = float(m.group(1))
      if width > 0:
        return png_width / width
    except ValueError:
      pass
  return 1.0


# ---------------------------------------------------------------------------
# rendering, with a cache
# ---------------------------------------------------------------------------

def cache_dir(cfg: AnalyzerConfig, musicxml) -> pathlib.Path:
  return cfg.render_dir / ('score-%s' % library.slugify(musicxml))


def cached_render(cfg: AnalyzerConfig, musicxml, scale: float = RASTER_SCALE) -> Optional[ScoreRender]:
  """The cached render for this score, if it matches the score on disk."""
  path = pathlib.Path(musicxml)
  if not path.is_file():
    return None
  manifest = cache_dir(cfg, path) / 'pages.json'
  if not manifest.is_file():
    return None
  try:
    data = json.loads(manifest.read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return None
  if data.get('version') != CACHE_VERSION or float(data.get('scale') or 0) != float(scale):
    return None
  stat = path.stat()
  if int(data.get('source_size') or -1) != stat.st_size or \
      abs(float(data.get('source_mtime') or 0) - stat.st_mtime) > 1.0:
    return None
  render = ScoreRender.from_dict(data)
  for page in render.pages:
    if not pathlib.Path(page.image).is_file():
      return None
  return render


def write_cache(cfg: AnalyzerConfig, musicxml, render: ScoreRender,
                scale: float = RASTER_SCALE) -> pathlib.Path:
  """Persist a render's manifest, so the next open finds it.

  Kept separate from `render_score` because the cache format is a detail of *this*
  module, and a storage adapter that wants to store a render should not have to
  reimplement it. The page images are expected to be at the paths the render
  already references.
  """
  path = pathlib.Path(musicxml)
  out_dir = cache_dir(cfg, path)
  out_dir.mkdir(parents=True, exist_ok=True)
  payload = render.to_dict()
  if path.is_file():
    stat = path.stat()
    payload.update({'scale': float(scale), 'source_size': stat.st_size,
                    'source_mtime': stat.st_mtime})
  manifest = out_dir / 'pages.json'
  temp = manifest.with_suffix('.json.tmp')
  temp.write_text(json.dumps(payload), encoding='utf-8')
  temp.replace(manifest)
  return manifest


def render_score(cfg: AnalyzerConfig, musicxml, scale: float = RASTER_SCALE,
                 force: bool = False, progress: Progress = None) -> ScoreRender:
  """Engrave `musicxml` into page images with measure geometry.

  Args:
    cfg: where the render cache lives.
    musicxml: the authoritative score (.musicxml, .xml or .mxl).
    scale: PNG pixels per SVG pixel.
    force: ignore the cache and engrave again.
    progress: optional `(percent, message)` callback.

  Returns:
    A `ScoreRender` whose page images exist on disk.

  Raises:
    ScoreRenderError: if the score cannot be read, or the engraving tools are
      missing or fail.
  """
  path = pathlib.Path(musicxml)
  if not path.is_file():
    raise ScoreRenderError('no such score: %s' % path)
  if not force:
    cached = cached_render(cfg, path, scale)
    if cached is not None:
      _report(progress, 100.0, 'score already rendered')
      return cached

  verovio, cairosvg = _require_engines()
  cfg.ensure_dirs()
  out_dir = cache_dir(cfg, path)
  out_dir.mkdir(parents=True, exist_ok=True)

  _report(progress, 5.0, 'reading the score')
  try:
    xml = _score_xml(path)
  except (OSError, zipfile.BadZipFile) as e:
    raise ScoreRenderError('could not read %s: %s' % (path.name, e))

  toolkit = verovio.toolkit()
  toolkit.setOptions({'pageWidth': 2100, 'scale': 40, 'adjustPageHeight': True,
                      'svgBoundingBoxes': True, 'header': 'none', 'footer': 'none'})
  try:
    toolkit.loadData(xml)
  except Exception as e:
    raise ScoreRenderError('verovio could not read %s: %s' % (path.name, e))

  total = max(1, toolkit.getPageCount())
  ids = measure_ids(toolkit.getMEI())
  if not ids:
    raise ScoreRenderError('no measures were found in %s' % path.name)

  pages: List[ScorePage] = []
  warnings: List[str] = []
  for number in range(1, total + 1):
    _report(progress, 10.0 + 80.0 * (number - 1) / total,
            'engraving page %d of %d' % (number, total))
    svg = toolkit.renderToSVG(number)
    boxes, box_warnings = _measure_geometry(svg, ids)
    glyphs = [glyph for glyph, _element_id in glyph_elements(svg, ids)]
    warnings.extend(box_warnings)
    image_path = out_dir / ('page-%02d.png' % number)
    try:
      png = cairosvg.svg2png(bytestring=svg.encode('utf-8'), scale=scale)
    except Exception as e:
      raise ScoreRenderError('could not rasterise page %d: %s' % (number, e))
    image_path.write_bytes(png)

    from PIL import Image
    import io
    with Image.open(io.BytesIO(png)) as image:
      width, height = image.size
    factor = _viewbox_scale(svg, width)
    pages.append(ScorePage(number=number, image=str(image_path), width=width,
                           height=height, measures=_scale_boxes(boxes, factor),
                           glyphs=_scale_glyphs(glyphs, factor)))

  render = ScoreRender(slug=library.slugify(path), source=str(path), pages=pages,
                       warnings=warnings)
  write_cache(cfg, path, render, scale=scale)
  _report(progress, 100.0, 'engraved %d page(s)' % len(pages))
  return render
