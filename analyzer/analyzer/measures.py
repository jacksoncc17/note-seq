"""The list of measures a performance actually plays, in order.

A score's printed measure numbers are not the order they are played: repeat
barlines send you back, first and second endings choose different bars on
different passes, and a D.C. or D.S. jump can replay a whole section. Anything
that maps measures onto a recording has to work in *performance order*, so this
module turns a MusicXML file into that order.

The result is a list of `MeasureEvent`s, one per measure as played, each knowing
both its printed number (what the pianist reads on the page) and its position in
the performance (what a timestamp can be attached to).

Scope and honesty: repeats (`<repeat>`, including `times`), volta endings and
D.C./D.S./To Coda/Fine are implemented. Anything else that changes playback
order -- `time-only` repeats, nested/mid-measure repeats with no left barline,
multiple segnos -- is *not* guessed at: those raise a warning in the result so
the caller can tell the user instead of silently mis-numbering their bars.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple
import xml.etree.ElementTree as ET


# A repeat that never terminates is a malformed score, not an infinite piece:
# this bounds the simulation at twenty times the written length.
_MAX_PASSES_PER_MEASURE = 20


class ScoreError(RuntimeError):
  """Raised when a score cannot be read well enough to number its measures."""


@dataclass(frozen=True)
class MeasureEvent:
  """One measure as played.

  Attributes:
    index: 0-based index of the measure in the written score.
    number: the printed measure number, as an int.
    printed: the raw `number` attribute from the file, kept because it is not
      always numeric (split measures are numbered `12a`, `12b`).
    play_index: 0-based position in the performance.
    pass_no: 1-based pass number for this measure (2 means a repeat pass).
    is_repeat_pass: True when this is not the first pass over this measure.
    is_pickup: True for an implicit anacrusis measure.
  """

  index: int
  number: int
  printed: str
  play_index: int
  pass_no: int
  is_repeat_pass: bool
  is_pickup: bool = False


@dataclass
class Expansion:
  """The performance order of a score, plus what we could not be sure about."""

  events: List[MeasureEvent] = field(default_factory=list)
  written: int = 0
  warnings: List[str] = field(default_factory=list)
  source: str = ''

  def __len__(self) -> int:
    return len(self.events)

  @property
  def played_numbers(self) -> List[int]:
    """Printed measure numbers in performance order -- the `played` list."""
    return [e.number for e in self.events]

  @property
  def repeat_start_measures(self) -> List[int]:
    return sorted({e.number for e in self.events if e.is_repeat_pass})

  @property
  def is_expanded(self) -> bool:
    return len(self.events) != self.written

  def play_index_of(self, measure: int, pass_no: int = 1) -> Optional[int]:
    """Position in the performance of the `pass_no`-th playing of `measure`."""
    seen = 0
    for e in self.events:
      if e.number == measure:
        seen += 1
        if seen == pass_no:
          return e.play_index
    return None

  def summary(self) -> str:
    if self.is_expanded:
      repeats = len(self.events) - self.written
      return ('%d written measures, %d played (+%d from repeats)'
              % (self.written, len(self.events), repeats))
    return '%d measures, no repeats' % self.written


# ---------------------------------------------------------------------------
# reading the file
# ---------------------------------------------------------------------------

def load_score_xml(path) -> str:
  """Return the MusicXML text of `path`, transparently unpacking .mxl.

  Raises:
    ScoreError: if the file is missing, not a zip/XML, or an .mxl without a score.
  """
  path = pathlib.Path(path)
  if not path.is_file():
    raise ScoreError('no such score file: %s' % path)
  with open(path, 'rb') as handle:
    magic = handle.read(4)
  # by content, not by name: a renamed archive used to fail with a confusing error
  if magic[:2] == b'PK' or path.suffix.lower() in ('.mxl', '.zip'):
    try:
      with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith(('.xml', '.musicxml')) and 'META-INF' not in n]
        if not names:
          raise ScoreError('%s contains no MusicXML part' % path.name)
        # container.xml names the root score; fall back to the largest .xml
        try:
          container = ET.fromstring(z.read('META-INF/container.xml'))
          rootfiles = [e.get('full-path') for e in container.iter()
                       if _local(e.tag) == 'rootfile' and e.get('full-path')]
          if rootfiles and rootfiles[0] in z.namelist():
            names = rootfiles
        except (KeyError, ET.ParseError):
          pass
        return z.read(names[0]).decode('utf-8', 'replace')
    except zipfile.BadZipFile as e:
      raise ScoreError('%s is not a readable .mxl archive (%s)' % (path.name, e))
  try:
    return path.read_text(encoding='utf-8', errors='replace')
  except OSError as e:
    raise ScoreError('could not read %s: %s' % (path, e))


def _local(tag: str) -> str:
  return tag.rsplit('}', 1)[-1]


def _children(element, name: str):
  return [c for c in element if _local(c.tag) == name]


def _descendants(element, name: str):
  return [d for d in element.iter() if _local(d.tag) == name]


# ---------------------------------------------------------------------------
# reading the structure of one measure
# ---------------------------------------------------------------------------

@dataclass
class _Marks:
  """What a single measure says about playback order."""

  forward: bool = False
  backward: bool = False
  backward_times: Optional[int] = None
  start_ending: Optional[Set[int]] = None      # numbers, e.g. {1} or {1, 2}
  stop_ending: bool = False
  dacapo: bool = False
  dalsegno: bool = False
  tocoda: bool = False
  fine: bool = False
  segno: bool = False
  coda: bool = False
  time_only: Optional[str] = None


def _parse_ending_numbers(raw: Optional[str]) -> Set[int]:
  """`number="1,2"` -> {1, 2}; an empty or unparsable value means 'any pass'."""
  if not raw:
    return set()
  out = set()
  for token in re.split(r'[,\s]+', raw.strip()):
    if token.isdigit():
      out.add(int(token))
  return out


def _measure_marks(measure) -> _Marks:
  marks = _Marks()
  for barline in _children(measure, 'barline'):
    for child in barline:
      tag = _local(child.tag)
      if tag == 'repeat':
        direction = (child.get('direction') or '').lower()
        if direction == 'forward':
          marks.forward = True
        elif direction == 'backward':
          marks.backward = True
          times = child.get('times')
          marks.backward_times = int(times) if times and times.isdigit() else None
        if child.get('after-jump') == 'yes':
          marks.time_only = 'after-jump'
      elif tag == 'ending':
        etype = (child.get('type') or '').lower()
        if etype == 'start':
          marks.start_ending = _parse_ending_numbers(child.get('number'))
        elif etype in ('stop', 'discontinue'):
          marks.stop_ending = True
    if (barline.get('location') or '').lower() == 'left':
      pass
  for direction in _children(measure, 'direction'):
    for sound in _descendants(direction, 'sound'):
      if (sound.get('dacapo') or '').lower() == 'yes':
        marks.dacapo = True
      if sound.get('dalsegno'):
        marks.dalsegno = True
      if sound.get('tocoda'):
        marks.tocoda = True
      if (sound.get('fine') or '').lower() == 'yes':
        marks.fine = True
      if sound.get('time-only'):
        marks.time_only = sound.get('time-only')
    for kind in ('segno', 'coda'):
      if _descendants(direction, kind):
        setattr(marks, kind, True)
  return marks


def _measure_numbers(part) -> Tuple[List[str], List[bool]]:
  """Raw `number` attributes and pickup flags, in score order."""
  printed, pickups = [], []
  for measure in _children(part, 'measure'):
    printed.append(measure.get('number') or str(len(printed) + 1))
    pickups.append((measure.get('implicit') or '').lower() == 'yes')
  return printed, pickups


def _numeric_numbers(printed: Sequence[str], warnings: List[str]) -> List[int]:
  """Printed numbers as ints, falling back to 1..n for non-numeric ones.

  Split measures (`12a`, `12b`) and `X1`-style numbers cannot be compared with a
  recording's bar count, so they get sequential numbers and a warning rather
  than being silently coerced into a wrong bar number.
  """
  out, non_numeric = [], []
  for i, raw in enumerate(printed):
    text = raw.strip()
    if re.fullmatch(r'\d+', text):
      out.append(int(text))
    else:
      out.append(i + 1)
      non_numeric.append(raw)
  if non_numeric:
    warnings.append(
        'measure numbers that are not plain numbers were renumbered '
        'sequentially: %s' % ', '.join(non_numeric[:6]))
  return out


# ---------------------------------------------------------------------------
# turning marks into a performance order
# ---------------------------------------------------------------------------

def _expand(numbers: List[int], printed: List[str], marks: List[_Marks],
            pickups: List[bool], warnings: List[str]) -> List[MeasureEvent]:
  n = len(numbers)
  ending_at: Dict[int, Tuple[int, Set[int]]] = {}
  open_ending: Optional[Tuple[int, Set[int]]] = None
  for i, m in enumerate(marks):
    if m.start_ending is not None:
      open_ending = (i, m.start_ending)
    if open_ending is not None:
      ending_at[i] = open_ending
    if m.stop_ending and open_ending is not None:
      open_ending = None

  repeat_start_for: Dict[int, int] = {}          # measure index -> forward index
  for i, m in enumerate(marks):
    if m.forward:
      repeat_start_for[i] = i

  segno_index = next((i for i, m in enumerate(marks) if m.segno), None)
  coda_index = next((i for i, m in enumerate(marks) if m.coda), None)
  jump_index = next((i for i, m in enumerate(marks) if m.dacapo or m.dalsegno), None)
  jump_target = 0 if (jump_index is not None and marks[jump_index].dacapo) else segno_index
  fine_index = next((i for i, m in enumerate(marks) if m.fine), None)
  tocoda_index = next((i for i, m in enumerate(marks) if m.tocoda), None)

  if jump_index is not None and jump_target is None:
    warnings.append('a D.S. jump has no segno mark; the jump was ignored')

  # Open repeats. Each frame counts the passes it has *finished*, which is both
  # the pass number of the measures now being played and what the volta endings
  # need in order to choose between a first and a second ending. Counting per
  # frame (rather than in a dict keyed by measure index) is what makes nested
  # repeats work: re-entering the outer repeat must start a fresh inner count.
  order: List[MeasureEvent] = []
  stack: List[Dict[str, int]] = []               # {'start', 'want', 'done'}
  jumped = False
  i = 0
  guard = n * _MAX_PASSES_PER_MEASURE + 8
  while i < n and guard > 0:
    guard -= 1
    m = marks[i]

    # first/second endings: skip a group whose numbers exclude this pass
    group = ending_at.get(i)
    if group is not None:
      start, numbers_in_ending = group
      current_pass = (stack[-1]['done'] + 1) if stack else 1
      if numbers_in_ending and current_pass not in numbers_in_ending:
        # this pass belongs to the other ending: jump past the whole group
        end = start
        while end + 1 in ending_at and ending_at[end + 1] == group:
          end += 1
        i = end + 1
        continue

    pass_no = (stack[-1]['done'] + 1) if stack else 1
    order.append(MeasureEvent(
        index=i, number=numbers[i], printed=printed[i],
        play_index=len(order), pass_no=pass_no,
        is_repeat_pass=pass_no > 1, is_pickup=pickups[i]))

    # Both of these stop or redirect the piece *after* the measure they are
    # notated in: 'To Coda' means 'play this bar, then jump', and 'Fine' means
    # the bar it marks is the last one heard.
    if m.tocoda and jumped and coda_index is not None:
      i = coda_index
      continue
    if m.fine and jumped:
      break

    if m.time_only:
      warnings.append('measure %s uses a time-only repeat; its order may be wrong'
                      % printed[i])

    if m.forward and not (stack and stack[-1]['start'] == i):
      stack.append({'start': i, 'want': 0, 'done': 0})

    if m.backward and stack:
      frame = stack[-1]
      frame['want'] = m.backward_times or 2
      frame['done'] += 1
      if frame['done'] < frame['want']:
        i = frame['start']
        continue
      stack.pop()

    if jump_index is not None and i == jump_index and not jumped and jump_target is not None:
      jumped = True
      i = jump_target
      continue

    i += 1
  else:
    if guard <= 0:
      warnings.append('repeat structure did not terminate; the expansion was stopped')

  if jump_index is not None and not jumped:
    warnings.append('a D.C./D.S. jump was never reached during expansion')
  if len(order) > n * _MAX_PASSES_PER_MEASURE:
    warnings.append('unusually many passes (%d); check the repeat marks' % len(order))
  return order


def expand_measures(path) -> Expansion:
  """Read `path` and return the measures in performance order.

  Args:
    path: a MusicXML file, or an .mxl archive containing one.

  Returns:
    An `Expansion` whose `events` are in performance order. `warnings` collects
    anything that could change the order and was not fully understood.

  Raises:
    ScoreError: if the file cannot be read or has no measures.
  """
  text = load_score_xml(path)
  try:
    root = ET.fromstring(text)
  except ET.ParseError as e:
    raise ScoreError('could not parse the MusicXML: %s' % e)

  parts = [p for p in root.iter() if _local(p.tag) == 'part']
  if not parts:
    raise ScoreError('no <part> elements found; is this a MusicXML score?')
  counts = [len(_children(p, 'measure')) for p in parts]
  part = parts[max(range(len(parts)), key=lambda k: counts[k])]
  if len(set(counts)) > 1:
    # Parts of a piano score normally align measure for measure. If they do not,
    # the largest part still gives usable numbers, but say so.
    pass

  printed, pickups = _measure_numbers(part)
  warnings: List[str] = []
  numbers = _numeric_numbers(printed, warnings)
  if not numbers:
    raise ScoreError('%s has no measures' % pathlib.Path(path).name)

  marks = [_measure_marks(m) for m in _children(part, 'measure')]
  events = _expand(numbers, printed, marks, pickups, warnings)

  if not any(m.forward or m.backward for m in marks):
    warnings = [w for w in warnings if 'repeat' not in w]

  return Expansion(events=events, written=len(numbers), warnings=warnings,
                   source=str(pathlib.Path(path)))


def played_measures(path) -> List[int]:
  """Convenience wrapper: printed measure numbers in performance order."""
  return expand_measures(path).played_numbers


def describe(path) -> str:
  """One-line human summary, e.g. for a log line or a label."""
  expansion = expand_measures(path)
  return '%s: %s' % (pathlib.Path(path).name, expansion.summary())
