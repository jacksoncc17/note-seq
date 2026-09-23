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

import dataclasses
import json
import pathlib
import re
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .config import AnalyzerConfig
from . import library


Progress = Optional[Callable[[float, str], None]]

CACHE_VERSION = 3      # 3 adds per-glyph geometry, including dots, for masking
RASTER_SCALE = 1.5          # PNG pixels per SVG pixel; ~1260 px wide pages
MEI_NS = '{http://www.musicxml.org/ns/musicxml}'


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
  """Where one measure sits, in PNG pixels on its page."""

  number: int
  x0: float
  y0: float
  x1: float
  y1: float

  def contains(self, x: float, y: float, pad: float = 0.0) -> bool:
    return (self.x0 - pad) <= x <= (self.x1 + pad) and (self.y0 - pad) <= y <= (self.y1 + pad)

  def to_dict(self) -> Dict[str, float]:
    return {'number': self.number, 'x0': round(self.x0, 1), 'y0': round(self.y0, 1),
            'x1': round(self.x1, 1), 'y1': round(self.y1, 1)}


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
                          x1=float(m['x1']), y1=float(m['y1']))
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
  """MusicXML text, unpacking .mxl. Separate from measures.py on purpose: this
  module only needs the bytes to hand to Verovio."""
  path = pathlib.Path(path)
  if path.suffix.lower() in ('.mxl', '.zip'):
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
    boxes.append(MeasureBox(number=number, x0=x0, y0=y0, x1=x1, y1=y1))
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
                     scale: float = RASTER_SCALE) -> Tuple[str, Dict[str, str]]:
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

  Returns:
    (svg, {glyph key: element id}) for that page.
  """
  text = _score_xml(pathlib.Path(musicxml))
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
  return svg, element_ids(svg, measure_ids(engine.getMEI()))


def element_ids(svg: str, ids: Dict[str, int]) -> Dict[str, str]:
  """glyph key -> element id, for the glyphs in this SVG.

  Only meaningful for the SVG it was computed from, since Verovio regenerates ids
  on every engraving -- which is why masks are stored as positions, not ids.
  """
  return {glyph.key: element_id for glyph, element_id in glyph_elements(svg, ids)}


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
  return [MeasureBox(number=b.number, x0=b.x0 * factor, y0=b.y0 * factor,
                     x1=b.x1 * factor, y1=b.y1 * factor) for b in boxes]


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
