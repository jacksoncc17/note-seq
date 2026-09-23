"""Anki cards for a passage: the masked score in front, everything else behind.

A card is built from four things the analyzer already has: the engraving (so a
passage can be cropped and its notes can be hidden), the measure map (so the
passage has an audio clip), the question bank (so the back can say what the chords
are without anyone typing them), and the storage ports (so the specs survive).

Two decisions worth knowing:

**Notes are addressed by position, not by id.** A mask says "bar 53, left hand,
5th notehead" (`53:2:note:4`), never Verovio's element id, because those are
regenerated on every engraving. Verified: after a fresh engraving all 15 glyphs in
bar 53 land on the same pixels, so a saved mask keeps pointing at the note the user
tapped. If the *score* changes, the masks are stale in a detectable way -- the
spec records the score's size and mtime.

**Scheduling is not here.** This module only authors cards; Anki schedules them,
on the desktop or in AnkiDroid, which is why the output is an `.apkg` and why
nothing in this file knows what a due date is.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .config import AnalyzerConfig
from . import scoreview
from .scoreview import GlyphBox, ScoreRender, render_score

# The card model and deck ids are fixed on purpose: Anki uses them to recognise
# cards it has seen before, so changing them would duplicate everybody's deck.
MODEL_ID = 1748293011
DECK_PREFIX = 'Piano'

MASK_RULES = {
    'everything': 'every notehead (the pitches go, the rhythm stays)',
    'staves_only': 'everything but the staves — an empty bar to fill in',
    'right_hand': 'the right hand (upper staff)',
    'left_hand': 'the left hand (lower staff)',
    'melody': 'the highest notehead of each onset',
    'inner_voices': 'every notehead except that melody',
    'first_onset': 'the first chord or note of each bar',
    'accidentals': 'the accidentals only',
}

MASKS_STAFF_KIND = ('note', 'rest', 'accid')      # what a tap can target
FINGERING_DIGITS = ('1', '2', '3', '4', '5')     # piano fingering, per hand
FINGERING_PLACEMENT = {1: 'above', 2: 'below'}   # right hand above, left hand below
CARDS_SCALE = 2.5               # card images are rendered larger than the app's view


class CardError(RuntimeError):
  """Raised when a card cannot be built, with a message fit to show a user."""


# ---------------------------------------------------------------------------
# the spec: what the user chose
# ---------------------------------------------------------------------------

@dataclass
class CardSpec:
  """One card: a bar range, what to hide, and what to say on the back."""

  slug: str
  first_bar: int
  last_bar: int
  title: str = ''
  masks: List[str] = field(default_factory=list)          # explicit glyph keys
  rules: List[str] = field(default_factory=list)          # rule names
  fingering: Dict[str, str] = field(default_factory=dict)  # glyph key -> digit
  fields: Dict[str, str] = field(default_factory=dict)     # curated back text
  tags: List[str] = field(default_factory=list)
  audio_side: str = 'back'                                 # back | front | both | none
  play_index: Optional[int] = None                         # which pass, with repeats
  score_size: int = 0                                      # the score this was made from
  score_mtime: float = 0.0
  created_at: str = ''

  @property
  def bar_range(self) -> Tuple[int, int]:
    return (min(self.first_bar, self.last_bar), max(self.first_bar, self.last_bar))

  def signature(self) -> str:
    """A stable identity for the card: the same choices give the same signature,
    so re-exporting updates cards instead of duplicating them."""
    payload = json.dumps({
        'slug': self.slug, 'range': self.bar_range,
        'masks': sorted(self.masks), 'rules': sorted(self.rules),
        'fingering': sorted(self.fingering.items()),
    }, sort_keys=True)
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]

  def to_dict(self) -> Dict[str, object]:
    return dataclasses.asdict(self)

  @classmethod
  def from_dict(cls, data: Dict[str, object]) -> 'CardSpec':
    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class DeckSpec:
  """A named set of cards for one piece -- what gets exported as one .apkg."""

  slug: str
  name: str
  cards: List[CardSpec] = field(default_factory=list)
  updated_at: str = ''

  def to_dict(self) -> Dict[str, object]:
    return {'slug': self.slug, 'name': self.name, 'updated_at': self.updated_at,
            'cards': [c.to_dict() for c in self.cards]}

  @classmethod
  def from_dict(cls, data: Dict[str, object]) -> 'DeckSpec':
    return cls(slug=str(data.get('slug', '')), name=str(data.get('name', '')),
               updated_at=str(data.get('updated_at', '')),
               cards=[CardSpec.from_dict(c) for c in (data.get('cards') or [])])


# ---------------------------------------------------------------------------
# masks: rules and taps resolve to the same thing, a set of glyph keys
# ---------------------------------------------------------------------------

def _onset_groups(glyphs: Sequence[GlyphBox], tolerance: float = 12.0) -> List[List[GlyphBox]]:
  """Group noteheads that sound together, across both hands.

  Grouping is by x position and by measure, not by musical knowledge, because the
  SVG does not say which notes share a beat. Notes at the same x in the same bar
  were engraved as one onset, whether they are in the right hand or the left --
  which is what makes "the melody" mean the top note of a two-hand chord rather
  than the top note of the right hand, a different and less useful answer.
  """
  notes = sorted((g for g in glyphs if g.kind == 'note'), key=lambda g: (g.measure, g.x0))
  groups: List[List[GlyphBox]] = []
  for note in notes:
    if groups and groups[-1][0].measure == note.measure and \
        abs(note.x0 - groups[-1][0].x0) <= tolerance:
      groups[-1].append(note)
    else:
      groups.append([note])
  return groups


def resolve_rule(rule: str, glyphs: Sequence[GlyphBox]) -> List[str]:
  """The glyph keys a named rule covers, within the passage's glyphs.

  Rules are deliberately geometric -- staff, x position, element kind -- because
  that is what the engraving actually tells us. `melody` therefore means "the
  highest notehead of each onset", which is what a pianist means in most textures.

  Raises:
    CardError: for a rule name that does not exist.
  """
  if rule not in MASK_RULES:
    raise CardError('unknown masking rule: %r (known: %s)'
                    % (rule, ', '.join(sorted(MASK_RULES))))
  notes = [g for g in glyphs if g.kind == 'note']

  if rule == 'everything':
    return [g.key for g in notes]
  if rule == 'staves_only':
    # every drawn thing except the staff lines themselves: what a pianist would
    # have to supply from memory, including the rhythm
    return [g.key for g in glyphs if g.kind in ('note', 'rest', 'accid', 'stem',
                                                'beam', 'dots')]
  if rule == 'right_hand':
    return [g.key for g in notes if g.staff == 1]
  if rule == 'left_hand':
    return [g.key for g in notes if g.staff == 2]
  if rule == 'accidentals':
    return [g.key for g in glyphs if g.kind == 'accid']
  if rule == 'melody':
    return [min(group, key=lambda g: g.y0).key for group in _onset_groups(glyphs)]
  if rule == 'inner_voices':
    melody = {min(group, key=lambda g: g.y0).key for group in _onset_groups(glyphs)}
    return [g.key for g in notes if g.key not in melody]
  if rule == 'first_onset':
    chosen: List[str] = []
    for _measure, group in _by_measure(glyphs).items():
      groups = _onset_groups(group)
      if groups:
        chosen.extend(g.key for g in groups[0])
    return chosen
  raise CardError('rule %r is not implemented' % rule)


def _by_measure(glyphs: Sequence[GlyphBox]) -> Dict[int, List[GlyphBox]]:
  out: Dict[int, List[GlyphBox]] = {}
  for glyph in glyphs:
    out.setdefault(glyph.measure, []).append(glyph)
  return out


def masked_keys(spec: CardSpec, glyphs: Sequence[GlyphBox]) -> List[str]:
  """Everything hidden on the front: explicit taps plus whatever the rules cover.

  Sorted and de-duplicated so two specs that mean the same thing produce the same
  card signature.
  """
  keys = set(spec.masks)
  for rule in spec.rules:
    keys.update(resolve_rule(rule, glyphs))
  available = {g.key for g in glyphs}
  return sorted(keys & available)


# ---------------------------------------------------------------------------
# pictures
# ---------------------------------------------------------------------------

def passage_strips(render: ScoreRender, first: int, last: int
                   ) -> List[Tuple[int, Tuple[float, float, float, float]]]:
  """The rectangles to crop, one per system the passage touches.

  Cropping per system rather than one big rectangle keeps unrelated bars out of the
  middle of the image, and a passage that crosses a page break still comes out in
  reading order.

  Returns:
    [(page number, (x0, y0, x1, y1)), ...] in reading order.
  """
  low, high = min(first, last), max(first, last)
  rows: List[Tuple[int, int, float, float, float, float]] = []
  for page in render.pages:
    selected = [b for b in page.measures if low <= b.number <= high]
    if not selected:
      continue
    for box in sorted(selected, key=lambda b: (b.y0, b.x0)):
      # the staff box, deliberately: the geometry says where the *system* is, and
      # `expand_to_ink` grows it to the drawn music within a cap. Vertovio's other
      # boxes cannot be trusted per bar -- unioning every drawn element in one bar
      # produced a box ten times too tall -- so the extension is measured, not looked
      # up.
      x0, y0, x1, y1 = box.x0, box.y0, box.x1, box.y1
      placed = False
      for index, row in enumerate(rows):
        page_number, _n, _x0, ry0, _x1, ry1 = row
        if page_number == page.number and not (y1 < ry0 or y0 > ry1):
          rows[index] = (page_number, row[1], min(row[2], x0), min(row[3], y0),
                         max(row[4], x1), max(row[5], y1))
          placed = True
          break
      if not placed:
        rows.append((page.number, box.number, x0, y0, x1, y1))
  ordered = sorted(rows, key=lambda r: (r[0], r[3]))
  return [(page, (x0, y0, x1, y1)) for page, _n, x0, y0, x1, y1 in ordered]


def flatten(image, background: str = 'white'):
  """Composite a page image onto a solid background.

  The engraved pages are transparent -- cairosvg draws no background -- so dropping
  the alpha channel turns every pixel black. Anything that reads the pixels has to
  flatten first, which is easy to forget precisely because it looks fine on screen,
  where the viewer supplies its own white.
  """
  from PIL import Image
  if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
    image = image.convert('RGBA')
    flat = Image.new('RGB', image.size, background)
    flat.paste(image, mask=image.split()[-1])
    return flat
  return image.convert('RGB')


def strip_offsets(strips: Sequence[Tuple[int, Tuple[float, float, float, float]]],
                  page_size: Tuple[int, int], pad: float = 6.0,
                  gap: float = 10.0) -> List[Dict[str, float]]:
  """Where each cropped system lands in the stacked image.

  Shared by cropping and masking on purpose: if the two disagreed by even a few
  pixels the masks would sit beside the notes instead of on them, which is exactly
  the kind of fault nobody notices until they see the card.
  """
  width, height = page_size
  placed: List[Dict[str, float]] = []
  y = 0.0
  for _page_number, (x0, y0, x1, y1) in strips:
    left = max(0.0, x0 - pad)
    top = max(0.0, y0 - pad)
    right = min(float(width), x1 + pad)
    bottom = min(float(height), y1 + pad)
    placed.append({'source_left': left, 'source_top': top,
                   'x0': left, 'x1': right, 'y0': top, 'y1': bottom,
                   'paste_x': 0.0, 'paste_y': y,
                   'width': max(1.0, right - left), 'height': max(1.0, bottom - top)})
    y += max(1.0, bottom - top) + gap
  return placed


def crop_passage(image_path, strips: Sequence[Tuple[int, Tuple[float, float, float, float]]],
                 page_size: Tuple[int, int], pad: float = 6.0, gap: float = 10.0):
  """One image of a passage, its systems stacked with a small gap between them.

  Args:
    image_path: the page PNG to crop from.
    strips: rectangles in that image's pixels, all from the same page.
    page_size: the image's (width, height), used to clamp the crop.
    pad: pixels of margin around each system.
    gap: pixels between stacked systems.

  Returns:
    A PIL Image.
  """
  from PIL import Image
  if not strips:
    raise CardError('nothing to crop: the passage has no measures on any page')
  page = image_path if hasattr(image_path, 'crop') else flatten(Image.open(str(image_path)))
  placed = strip_offsets(strips, page_size, pad, gap)
  width = int(max(entry['width'] for entry in placed))
  height = int(max(entry['paste_y'] + entry['height'] for entry in placed))
  out = Image.new('RGB', (max(1, width), max(1, height)), 'white')
  for entry in placed:
    piece = page.crop((int(entry['x0']), int(entry['y0']),
                       int(entry['x1']), int(entry['y1'])))
    out.paste(piece, (int(entry['paste_x']), int(entry['paste_y'])))
  return out


def expand_to_ink(image, strip, budget: float = 120.0, bridge: float = 34.0,
                  threshold: int = 200, margin: float = 8.0, cap: float = 0.45):
  """Grow a system's rectangle outwards until the ink stops.

  The staff box alone is too tight -- leger-line notes, dynamics and tempo text sit
  outside it -- but a fixed generous band is too loose, because it reaches the next
  system and the crop swallows two systems' worth of music. So walk outwards row by
  row, keeping track of the last row that had ink and allowing a small gap (a space
  between leger lines is ink, a gap between systems is not), and stop at the first
  long blank run.

  Args:
    image: the page image.
    strip: (x0, y0, x1, y1) of the system, from the measure geometry.
    budget: how far to look, in pixels.
    bridge: the largest blank run still considered part of the same system.
    threshold: how dark a pixel counts as ink.
    margin: pixels of margin kept around the result.
    cap: the most the walk may extend beyond the staves, as a fraction of the
      system's height. Without it a system whose neighbours are close enough gets
      bridged to them and the crop grows to a whole page -- which is worse than
      clipping a leger line.

  Returns:
    A (x0, y0, x1, y1) rectangle clipped to the image.
  """
  import numpy as np
  x0, y0, x1, y1 = [int(v) for v in strip]
  x0, x1 = max(0, x0), min(image.width, x1)
  band_top = max(0, int(y0 - budget))
  band_bottom = min(image.height, int(y1 + budget))
  if x1 <= x0 or band_bottom <= band_top:
    return (x0, max(0, y0), x1, min(image.height, y1))
  grey = np.asarray(image.convert('L').crop((x0, band_top, x1, band_bottom)))
  rows = (grey < threshold).any(axis=1)
  start = max(0, min(len(rows) - 1, int(y0 - band_top)))
  stop = max(0, min(len(rows) - 1, int(y1 - band_top)))
  if not rows.any():
    return (x0, max(0, y0), x1, min(image.height, y1))

  top, gap = start, 0
  for row in range(start, -1, -1):
    if rows[row]:
      top, gap = row, 0
    else:
      gap += 1
      if gap > bridge:
        break
  bottom, gap = stop, 0
  for row in range(stop, len(rows)):
    if rows[row]:
      bottom, gap = row, 0
    else:
      gap += 1
      if gap > bridge:
        break
  limit = max(2.0, cap * (y1 - y0))
  top = max(top, start - int(limit))
  bottom = min(bottom, stop + int(limit))
  return (x0, max(0, int(band_top + top - margin)), x1,
          min(image.height, int(band_top + bottom + 1 + margin)))


def fingered_score_xml(score_xml: str, fingerings: Dict[str, str]) -> str:
  """The score with fingerings added, as text. The source file is never touched.

  Fingering is written into a copy of the MusicXML held in memory so Verovio
  engraves it: the number is then placed by the engraver (the right side of the
  notehead, clear of stems and slurs) instead of being painted over the picture. The
  caller passes the result straight to Verovio, which takes XML as a string, so no
  file is written anywhere.

  Notes are addressed the way masks are -- `bar:staff:note:index` -- and resolved by
  counting the drawn notes of that staff within the bar, which is the same order
  Verovio draws them in. Rests are skipped, and each hand gets its own placement.

  Args:
    score_xml: the original MusicXML text.
    fingerings: glyph key -> digit.

  Returns:
    MusicXML text with `<fingering>` added, or the input unchanged when there is
    nothing to add.

  Raises:
    CardError: if the score cannot be parsed or written back.
  """
  import xml.etree.ElementTree as ET

  if not fingerings:
    return score_xml
  try:
    root = ET.fromstring(score_xml)
  except ET.ParseError as e:
    raise CardError('could not read the score to add fingerings: %s' % e)

  def local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]

  for element in root.iter():
    namespace = element.tag.split('}')[0][1:] if '}' in element.tag else ''
    if namespace:
      ET.register_namespace('', namespace)
      break

  wanted: Dict[tuple, str] = {}
  for key, digit in fingerings.items():
    parts = str(key).split(':')
    if len(parts) < 4 or parts[2] != 'note':
      continue
    try:
      wanted[(int(parts[0]), int(parts[1]), int(parts[3]))] = str(digit)
    except ValueError:
      continue
  if not wanted:
    return score_xml

  placed = 0
  for measure in root.iter():
    if local(measure.tag) != 'measure':
      continue
    try:
      number = int(str(measure.get('number') or '').strip())
    except ValueError:
      continue
    staff = 1
    counters: Dict[int, int] = {}
    for note in list(measure):
      if local(note.tag) != 'note':
        continue
      for child in note:
        if local(child.tag) == 'staff' and (child.text or '').strip().isdigit():
          staff = int(child.text.strip())
      if any(local(child.tag) == 'rest' for child in note):
        continue
      index = counters.get(staff, 0)
      counters[staff] = index + 1
      digit = wanted.get((number, staff, index))
      if digit is None:
        continue
      _attach_fingering(ET, note, local, digit,
                        FINGERING_PLACEMENT.get(staff, 'above'))
      placed += 1

  if not placed:
    return score_xml
  try:
    return ET.tostring(root, encoding='unicode')
  except (TypeError, ValueError) as e:
    raise CardError('could not write the fingerings back: %s' % e)


def _attach_fingering(ET, note, local, digit: str, placement: str) -> None:
  """Put `<notations><technical><fingering>` on a note, in schema order.

  MusicXML expects notations before lyric and play, so a note that carries lyrics
  must not simply have this appended.
  """
  notations = next((c for c in note if local(c.tag) == 'notations'), None)
  if notations is None:
    notations = ET.Element(note.tag.split('}')[0] + 'notations' if '}' in note.tag
                           else 'notations')
    at = len(note)
    for position, child in enumerate(note):
      if local(child.tag) in ('lyric', 'play'):
        at = position
        break
    note.insert(at, notations)
  technical = next((c for c in notations if local(c.tag) == 'technical'), None)
  if technical is None:
    technical = ET.SubElement(notations, notations.tag.split('}')[0] + 'technical'
                              if '}' in notations.tag else 'technical')
  fingering = ET.SubElement(technical, technical.tag.split('}')[0] + 'fingering'
                            if '}' in technical.tag else 'fingering')
  fingering.set('placement', placement)
  fingering.text = digit


def card_score_xml(piece, spec: CardSpec) -> Optional[str]:
  """The MusicXML to engrave for this card: the score, with this card's fingerings.

  Fingerings come from the piece (entered once) and the card records the subset it
  used, so a card stays self-describing after the piece's set changes.
  """
  if piece is None or piece.musicxml is None:
    return None
  text = scoreview._score_xml(pathlib.Path(piece.musicxml))
  merged: Dict[str, str] = {}
  if piece.record is not None:
    merged.update(piece.record.fingerings or {})
  merged.update({k: v for k, v in (spec.fingering or {}).items() if v})
  low, high = spec.bar_range
  merged = {k: v for k, v in merged.items()
            if k.split(':')[0].isdigit() and low <= int(k.split(':')[0]) <= high}
  return fingered_score_xml(text, merged)


def fingerings_in(spec: CardSpec, piece=None) -> Dict[str, str]:
  """The fingerings this card will engrave: the piece's set, the card's overrides."""
  merged: Dict[str, str] = {}
  if piece is not None and piece.record is not None:
    merged.update(piece.record.fingerings or {})
  merged.update({k: v for k, v in (spec.fingering or {}).items() if v})
  low, high = spec.bar_range
  return {k: v for k, v in merged.items()
          if k.split(':')[0].isdigit() and low <= int(k.split(':')[0]) <= high}


def passage_regions(cfg: AnalyzerConfig, piece, render: ScoreRender, spec: CardSpec,
                    scale: float = CARDS_SCALE,
                    xml_text: Optional[str] = None) -> Dict[int, Tuple[float, float, float, float]]:
  """The rectangle each page's passage occupies, measured from the ink.

  Computed once and used for both sides of the card, so the front and the back show
  exactly the same region -- measuring again after hiding notes would give the front
  a different crop, and the two images would not line up.
  """
  import io

  import cairosvg
  from PIL import Image

  low, high = spec.bar_range
  strips = passage_strips(render, low, high)
  if not strips:
    raise CardError('bars %d-%d are not in the engraving' % (low, high))
  by_page: Dict[int, List[Tuple[int, Tuple[float, float, float, float]]]] = {}
  for page_number, rect in strips:
    by_page.setdefault(page_number, []).append((page_number, rect))

  regions: Dict[int, Tuple[float, float, float, float]] = {}
  for page_number in sorted(by_page):
    page = next((p for p in render.pages if p.number == page_number), None)
    if page is None:
      continue
    try:
      svg, _ids = scoreview.page_svg_and_ids(cfg, piece.musicxml, page_number, scale=scale,
                                             xml_text=xml_text)
      png = cairosvg.svg2png(bytestring=svg.encode('utf-8'), scale=scale)
    except Exception as e:
      raise CardError('could not engrave page %d for the card: %s' % (page_number, e))
    with Image.open(io.BytesIO(png)) as drawn:
      image = flatten(drawn)
    ratio = image.width / float(page.width)
    boxes = []
    for _number, (x0, y0, x1, y1) in by_page[page_number]:
      height = max(1.0, y1 - y0)
      boxes.append(expand_to_ink(image, (x0 * ratio, y0 * ratio, x1 * ratio, y1 * ratio),
                                 budget=0.7 * height * ratio, bridge=0.12 * height * ratio))
    regions[page_number] = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                            max(b[2] for b in boxes), max(b[3] for b in boxes))
  return regions


def passage_image(cfg: AnalyzerConfig, piece, render: ScoreRender, spec: CardSpec,
                  hidden: Sequence[str], scale: float = CARDS_SCALE, hide: bool = True,
                  regions: Optional[Dict[int, Tuple[float, float, float, float]]] = None,
                  xml_text: Optional[str] = None,
                  hide_fingering: bool = False):
  """One image of the passage, optionally with the chosen glyphs suppressed.

  Usage:
    back  = passage_image(..., hide=False)
    front = passage_image(..., hide=True, hidden=masked_keys(...))

  The page is engraved again at `scale` and the masked elements are hidden in the
  SVG before it is rasterised, so a hidden notehead is genuinely not drawn -- no
  patch beside it, and the staff lines stay clean. The crop rectangles come from the
  cached render, so they are scaled from that render's page width to this one.

  Raises:
    CardError: if the passage is not in the engraving, or a page cannot be made.
  """
  import io

  import cairosvg
  from PIL import Image

  low, high = spec.bar_range
  strips = passage_strips(render, low, high)
  if not strips:
    raise CardError('bars %d-%d are not in the engraving' % (low, high))

  by_page: Dict[int, List[Tuple[int, Tuple[float, float, float, float]]]] = {}
  for page_number, rect in strips:
    by_page.setdefault(page_number, []).append((page_number, rect))

  pieces = []
  for page_number in sorted(by_page):
    page = next((p for p in render.pages if p.number == page_number), None)
    if page is None:
      continue
    try:
      svg, ids = scoreview.page_svg_and_ids(cfg, piece.musicxml, page_number, scale=scale,
                                            xml_text=xml_text)
    except Exception as e:
      raise CardError('could not engrave page %d for the card: %s' % (page_number, e))
    if hide and hidden:
      wanted = [ids[key] for key in hidden if key in ids]
      if not wanted:
        raise CardError('the masks do not match this engraving (page %d)' % page_number)
      svg = scoreview.hide_elements(svg, wanted)
    if hide_fingering and fingerings_in(spec, piece):
      # the front asks "what fingering would you use?" by not showing the answer
      svg = scoreview.hide_classes(svg, ['fing'])
    try:
      png = cairosvg.svg2png(bytestring=svg.encode('utf-8'), scale=scale)
    except Exception as e:
      raise CardError('could not draw page %d: %s' % (page_number, e))
    with Image.open(io.BytesIO(png)) as drawn:
      image = flatten(drawn)
    ratio = image.width / float(page.width)
    if regions is not None and page_number in regions:
      tight = tuple(v for v in regions[page_number])
    else:
      tight = expand_to_ink(image, (by_page[page_number][0][1][0] * ratio,
                                    by_page[page_number][0][1][1] * ratio,
                                    by_page[page_number][0][1][2] * ratio,
                                    by_page[page_number][0][1][3] * ratio))
    pieces.append(crop_passage(image, [(page_number, tight)], image.size))
  return _stack(pieces, low, high)


def _stack(pieces: List[object], low: int, high: int):
  """Stack cropped systems vertically, which is how a pianist reads a passage."""
  from PIL import Image
  if not pieces:
    raise CardError('nothing was cropped for bars %d-%d' % (low, high))
  width = max(p.width for p in pieces)
  height = sum(p.height for p in pieces) + 10 * (len(pieces) - 1)
  out = Image.new('RGB', (width, height), 'white')
  y = 0
  for piece_image in pieces:
    out.paste(piece_image, (0, y))
    y += piece_image.height + 10
  return out


# ---------------------------------------------------------------------------
# sound
# ---------------------------------------------------------------------------

def passage_audio(cfg: AnalyzerConfig, piece, stored, first: int, last: int,
                  pre_roll: float = 0.12, post_roll: float = 0.25,
                  bitrate: str = '64k') -> Optional[bytes]:
  """The passage as a small audio file, MP3 when ffmpeg can manage it.

  Returns:
    The encoded bytes, or None when the piece has no recording or no map.
  """
  if stored is None or not piece.has_audio:
    return None
  span = stored.result.range_for_measures(first, last)
  if span is None:
    return None
  import soundfile as sf
  path = pathlib.Path(piece.audio.path)
  info = sf.info(str(path))
  duration = info.frames / float(info.samplerate)
  start = max(0.0, span[0] - pre_roll)
  end = min(duration, span[1] + post_roll)
  data, rate = sf.read(str(path), dtype='float32',
                       start=int(start * info.samplerate), stop=int(end * info.samplerate),
                       always_2d=False)
  if len(data) == 0:
    return None
  wav = io.BytesIO()
  sf.write(wav, data, rate, format='WAV', subtype='PCM_16')
  raw = wav.getvalue()

  ffmpeg = shutil.which('ffmpeg')
  if not ffmpeg:
    return raw
  try:
    result = subprocess.run(
        [ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'wav', '-i', 'pipe:0',
         '-codec:a', 'libmp3lame', '-b:a', bitrate, '-ac', '1', '-f', 'mp3', 'pipe:1'],
        input=raw, capture_output=True, timeout=120)
  except (OSError, subprocess.SubprocessError):
    return raw
  return result.stdout if result.returncode == 0 and result.stdout else raw


# ---------------------------------------------------------------------------
# the back of the card
# ---------------------------------------------------------------------------

def memorisation_fields(piece, first: int, last: int) -> Dict[str, str]:
  """What the back can say without anybody typing it.

  Comes from the question bank, which already knows each bar's chord, rhythm,
  contour, note count and the section it sits in.
  """
  low, high = min(first, last), max(first, last)
  fields: Dict[str, str] = {'range': 'bars %d-%d' % (low, high)}
  if piece is None:
    return fields
  fields['piece'] = piece.display_name

  measures = _bank_measures(piece, low, high)
  if measures:
    chords: List[str] = []
    for entry in measures:
      chord = str(entry.get('chord') or '').strip()
      if chord and (not chords or chords[-1] != chord):
        chords.append(chord)
    if chords:
      fields['chords'] = ' → '.join(chords)
    rhythms = sorted({str(e.get('rhythm') or '').strip() for e in measures if e.get('rhythm')})
    if rhythms:
      fields['rhythm'] = ', '.join(rhythms)
    contours = sorted({str(e.get('contour') or '').strip() for e in measures if e.get('contour')})
    if contours:
      fields['contour'] = ', '.join(contours)
    notes = [int(e.get('notes_count') or 0) for e in measures]
    if any(notes):
      fields['notes'] = '%d-%d per bar (%d in all)' % (min(notes), max(notes), sum(notes))
    first_notes = [str(e.get('first_note') or '') for e in measures if e.get('first_note')]
    if first_notes:
      fields['top_notes'] = ', '.join(first_notes[:8])
  sections = _bank_sections(piece, low, high)
  if sections:
    fields['section'] = ', '.join(sections)
  return fields


def _bank_measures(piece, low: int, high: int) -> List[Dict[str, object]]:
  """Per-bar facts for a range, from whichever bank the piece came from."""
  path = _bank_path(piece)
  if path is None:
    return []
  try:
    data = json.loads(path.read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return []
  out = []
  for entry in data.get('measures') or []:
    try:
      number = int(entry.get('measure'))
    except (TypeError, ValueError):
      continue
    if low <= number <= high:
      out.append(entry)
  return out


def _bank_sections(piece, low: int, high: int) -> List[str]:
  path = _bank_path(piece)
  if path is None:
    return []
  try:
    data = json.loads(path.read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return []
  names = []
  for section in data.get('sections') or []:
    label = section.get('name') or section.get('label')
    start = section.get('start_measure') or section.get('start')
    end = section.get('end_measure') or section.get('end')
    try:
      if label and float(start) <= high and float(end) >= low:
        names.append(str(label))
    except (TypeError, ValueError):
      continue
  return names


def _bank_path(piece) -> Optional[pathlib.Path]:
  """The question bank behind a piece, if it is still on disk.

  Found through the catalogue's own record -- the reference MIDI sits in the same
  directory as the bank -- rather than by guessing at paths.
  """
  candidates: List[pathlib.Path] = []
  if piece.record is not None and piece.record.reference_midi is not None:
    candidates.append(pathlib.Path(piece.record.reference_midi.key).parent / 'fingerprint.json')
  if piece.musicxml is not None:
    sibling = piece.musicxml.with_name('fingerprint.json')
    if sibling.is_file():
      candidates.append(sibling)
  for candidate in candidates:
    if candidate.is_file():
      return candidate
  return None


# ---------------------------------------------------------------------------
# building one card
# ---------------------------------------------------------------------------

@dataclass
class BuiltCard:
  """A card that exists as files, ready to be packaged."""

  spec: CardSpec
  front_png: pathlib.Path
  back_png: pathlib.Path
  audio: Optional[pathlib.Path]
  fields: Dict[str, str]
  guid: str

  def answer_html(self) -> str:
    rows = ''.join('<div><b>%s</b>: %s</div>' % (key.replace('_', ' '), value)
                   for key, value in self.fields.items() if value)
    return rows


def build_card(cfg: AnalyzerConfig, piece, render: ScoreRender, spec: CardSpec,
               workspace: pathlib.Path) -> BuiltCard:
  """Crop, mask, slice and describe one passage.

  Args:
    cfg: paths, for the render cache.
    piece: the piece being studied (for its recording and its bank).
    render: the engraving to crop.
    spec: what the user chose.
    workspace: where the card's files are written.

  Returns:
    A `BuiltCard` whose files exist under `workspace`.

  Raises:
    CardError: if the passage has nothing to crop, or the engraving is missing.
  """
  low, high = spec.bar_range
  strips = passage_strips(render, low, high)
  if not strips:
    raise CardError('bars %d-%d are not in the engraving' % (low, high))
  workspace.mkdir(parents=True, exist_ok=True)

  glyphs = render.glyphs_in_measures(low, high)
  hidden = masked_keys(spec, glyphs)

  xml_text = card_score_xml(piece, spec)
  has_fingering = bool(fingerings_in(spec, piece))
  # one engraving, used for both sides, so the two images line up
  regions = passage_regions(cfg, piece, render, spec, xml_text=xml_text)
  back_image = passage_image(cfg, piece, render, spec, hidden, hide=False, regions=regions,
                             xml_text=xml_text)
  front_image = (passage_image(cfg, piece, render, spec, hidden, hide=True, regions=regions,
                               xml_text=xml_text, hide_fingering=has_fingering)
                 if (hidden or has_fingering) else back_image.copy())
  assert front_image.size == back_image.size, 'the two sides must line up' 

  front_path = workspace / ('front-%s.png' % spec.signature())
  back_path = workspace / ('back-%s.png' % spec.signature())
  front_image.save(front_path)
  back_image.save(back_path)

  audio_path = None
  if spec.audio_side != 'none':
    stored = _stored_map(cfg, piece)
    data = passage_audio(cfg, piece, stored, low, high) if stored else None
    if data:
      suffix = '.mp3' if data[:3] == b'ID3' or data[:2] == b'\xff\xfb' else '.wav'
      audio_path = workspace / ('passage-%s%s' % (spec.signature(), suffix))
      audio_path.write_bytes(data)

  fields = dict(memorisation_fields(piece, low, high))
  fields.update({k: v for k, v in (spec.fields or {}).items() if v})
  if spec.title:
    fields['card'] = spec.title
  fingering = fingerings_in(spec, piece)
  if fingering:
    ordered = sorted(fingering.items(), key=lambda kv: _key_order(kv[0]))
    fields['fingering'] = ' '.join('%s:%s' % (_key_label(k), v) for k, v in ordered[:24])
  hidden_notes = [k for k in hidden if ':note:' in k]
  if hidden_notes:
    fields['masked'] = ('%d notehead(s) hidden — name them before revealing'
                        % len(hidden_notes))

  guid = _guid_for(spec)
  return BuiltCard(spec=spec, front_png=front_path, back_png=back_path,
                   audio=audio_path, fields=fields, guid=guid)


def _stored_map(cfg: AnalyzerConfig, piece):
  if piece is None:
    return None
  from . import store
  repo = store.open_repository(cfg)
  return repo.maps.get(piece.slug)


def _key_order(key: str):
  parts = key.split(':')
  try:
    return (int(parts[0]), int(parts[1]), int(parts[3]))
  except (IndexError, ValueError):
    return (0, 0, 0)


def _key_label(key: str) -> str:
  """`53:2:note:3` reads better as `53/L4` on the back of a card."""
  parts = key.split(':')
  if len(parts) < 4:
    return key
  hand = 'R' if parts[1] == '1' else 'L'
  try:
    return '%s%s%d' % (parts[0], hand, int(parts[3]) + 1)
  except ValueError:
    return key


def _guid_for(spec: CardSpec) -> str:
  """A stable Anki note id.

  Same choices, same guid -- which is what makes re-exporting an improved deck
  *update* the existing cards instead of duplicating every one of them.
  """
  import genanki
  return genanki.guid_for('analyzer', spec.slug, spec.bar_range[0], spec.bar_range[1],
                          spec.signature())


# ---------------------------------------------------------------------------
# building the deck
# ---------------------------------------------------------------------------

def _model():
  import genanki
  return genanki.Model(
      MODEL_ID, 'Analyzer measure card',
      fields=[{'name': 'Front'}, {'name': 'Back'}],
      templates=[{
          'name': 'Score passage',
          # No `{{FrontSide}}` on the answer side: that is Anki's usual default and
          # it renders the question again above the answer, which on a score card
          # means showing the masked passage a second time for no benefit.
          'qfmt': '<div class="prompt">{{Front}}</div>',
          'afmt': '<div class="answer">{{Back}}</div>',
      }],
      css='''
      .card { font-family: system-ui, sans-serif; background: #fbfbf8; color: #111;
              text-align: center; }
      .prompt img, .answer img { max-width: 100%; }
      .answer { text-align: left; font-size: 15px; line-height: 1.5; }
      .answer b { color: #1d4e8f; }
      ''',
      sort_field_index=0)


def build_deck(cfg: AnalyzerConfig, piece, deck: DeckSpec,
               workspace: Optional[pathlib.Path] = None) -> pathlib.Path:
  """Build cards for a whole deck and package them as an .apkg.

  Returns:
    The path to the .apkg.

  Raises:
    CardError: when the piece has no engraving, or no card could be built.
  """
  import genanki
  if piece is None:
    raise CardError('no such piece: %s' % deck.slug)
  if piece.musicxml is None:
    raise CardError('%s has no score on disk' % deck.slug)
  render = render_score(cfg, piece.musicxml)
  scratch = pathlib.Path(workspace or tempfile.mkdtemp(prefix='analyzer-cards-'))
  scratch.mkdir(parents=True, exist_ok=True)

  cards: List[BuiltCard] = []
  problems: List[str] = []
  for spec in deck.cards:
    try:
      cards.append(build_card(cfg, piece, render, spec, scratch))
    except CardError as e:
      problems.append('bars %d-%d: %s' % (spec.bar_range[0], spec.bar_range[1], e))
  if not cards:
    raise CardError('no card could be built (%s)' % '; '.join(problems[:3]))

  anki_deck = genanki.Deck(abs(hash(deck.slug)) % (10 ** 9),
                           '%s::%s' % (DECK_PREFIX, deck.name or piece.display_name))
  media: List[str] = []
  for card in cards:
    front = '<img src="%s">' % card.front_png.name
    audio = ''
    if card.audio is not None:
      tag = '[sound:%s]' % card.audio.name
      audio = tag
      if card.spec.audio_side == 'front':
        front = front + '<br>' + tag
      elif card.spec.audio_side == 'both':
        front = front + '<br>' + tag
    back = '<img src="%s">' % card.back_png.name
    if card.audio is not None and card.spec.audio_side in ('back', 'both'):
      back += '<br>' + audio
    back += card.answer_html()
    note = genanki.Note(model=_model(), fields=[front, back], guid=card.guid,
                        tags=['analyzer', deck.slug] + list(card.spec.tags))
    anki_deck.add_note(note)
    media.append(str(card.front_png))
    media.append(str(card.back_png))
    if card.audio is not None:
      media.append(str(card.audio))

  package = genanki.Package(anki_deck)
  package.media_files = media
  out = scratch / ('%s.apkg' % re.sub(r'[^a-z0-9]+', '-', (deck.name or deck.slug).lower()))
  package.write_to_file(str(out))
  if problems:
    out.with_suffix('.warnings.txt').write_text('\n'.join(problems), encoding='utf-8')
  return out
