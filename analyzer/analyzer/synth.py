"""Playing the score: the whole piano, or one hand at a time.

The measure map exists because a *recording* is the reference for how a piece goes.
But a recording cannot be taken apart -- separating the hands in a performance is
source separation, and nothing here pretends to do that. The **score** can be taken
apart, because every note in it says which staff it is on. So the practice tracks are
synthesised from the score, not filtered out of the recording.

Three tracks per piece:

    both    the whole piano, as written
    left    the lower staff only
    right   the upper staff only

All three come from one parse of one score with one tempo, so a bar is at the *same
instant* in every track. That is the point of them: practising the left hand alone
and then switching to both hands must not shift anything.

Three decisions, each of which was a fork in the road:

**The split is by staff, never by pitch.** "Below middle C is the left hand" is wrong
on real piano music, which overlaps in the middle: on the Chopin used to test this,
297 of 1912 notes would be misassigned, because the right hand goes down to B3 and the
left hand up to E-flat 4.

**The repo's MusicXML reader discards `<staff>`**, so the split is done at the parse
step -- parse the document, delete the other staff's notes from each measure, and only
then build the sequence. It keeps `<voice>`, which is not the same thing: voices are
per staff, not per hand, and this score uses five of them across two staves. Onsets
are worked out while parsing, so deleting notes afterwards moves nothing.

**The tracks are in strict time, at the tempo the piece's own reference MIDI already
uses.** A practice track is something to play along with, and the reference MIDI is
already a steady-tempo render of the score (that is what `align.midi_grid` reads). So
the expressive tempo curve is thrown away and every track is laid out on the same
uniform grid -- which also means a bar's position in the audio is arithmetic
(`(bar - first_bar) * seconds_per_bar`) instead of something to fit.

Rendering is FluidSynth, the same one the alignment uses, so a practice track sounds
like the reference the measure map was fitted against.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import xml.etree.ElementTree as ET
import zipfile
from typing import Dict, List, Optional, Sequence, Tuple

from .config import AnalyzerConfig


# The tracks, in the order a player thinks of them: everything, then each hand.
TRACKS = ('both', 'left', 'right')
TRACK_LABELS = {'both': 'both hands', 'left': 'left hand', 'right': 'right hand'}
STAFF_OF_TRACK = {'both': None, 'left': 2, 'right': 1}


class SynthError(RuntimeError):
  """Raised when a track cannot be made, with a message meant for a person."""


# ---------------------------------------------------------------------------
# the score, split by staff
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
  """The element name without its namespace; MusicXML files come both ways."""
  return tag.split('}')[-1]


def score_xml_text(path) -> str:
  """The MusicXML inside a .mxl, or the file itself."""
  path = pathlib.Path(path)
  if not path.is_file():
    raise SynthError('%s is not there' % path)
  if path.suffix.lower() != '.mxl':
    return path.read_text(encoding='utf-8', errors='replace')
  try:
    with zipfile.ZipFile(path) as archive:
      names = [n for n in archive.namelist()
               if n.lower().endswith(('.xml', '.musicxml')) and not n.startswith('META-INF')]
      if not names:
        raise SynthError('%s holds no MusicXML' % path.name)
      return archive.read(names[0]).decode('utf-8', errors='replace')
  except zipfile.BadZipfile as e:
    raise SynthError('could not open %s: %s' % (path.name, e))


def staff_per_note(path) -> List[List[Optional[int]]]:
  """Which staff each `<note>` is on, measure by measure, in document order.

  `<staff>` is optional in MusicXML and holds until it changes, so a note without one
  takes the staff of the note before it. `None` means the score never said -- a
  single-staff piece, in which case there are no hands to separate.
  """
  root = ET.fromstring(score_xml_text(path))
  measures: List[List[Optional[int]]] = []
  current: Optional[int] = None
  for measure in root.iter():
    if _local(measure.tag) != 'measure':
      continue
    staves: List[Optional[int]] = []
    for child in measure:
      if _local(child.tag) != 'note':
        continue
      staff = next((c for c in child if _local(c.tag) == 'staff'), None)
      if staff is not None and (staff.text or '').strip().isdigit():
        current = int(staff.text.strip())
      staves.append(current)
    measures.append(staves)
  return measures


def has_hands(staves: Sequence[Sequence[Optional[int]]]) -> bool:
  """Whether this score actually uses more than one staff."""
  return len({s for measure in staves for s in measure if s is not None}) > 1


# ---------------------------------------------------------------------------
# tempo: the score as written -> strict time
# ---------------------------------------------------------------------------

class TempoCurve:
  """Seconds to beats across a piece's tempo changes.

  The same piecewise-constant map `memorization/analyze.py` builds when it writes its
  steady practice MIDI; it lives here too so the analyzer can lay the practice tracks
  out on that grid without depending on the other project.
  """

  def __init__(self, tempos, total_time: float):
    events = sorted((max(0.0, float(t.time)), float(t.qpm)) for t in tempos if t.qpm > 0)
    if not events:
      events = [(0.0, 120.0)]
    if events[0][0] > 0:
      events.insert(0, (0.0, events[0][1]))
    dedup: List[Tuple[float, float]] = []
    for time_s, qpm in events:
      if dedup and abs(dedup[-1][0] - time_s) < 1e-9:
        dedup[-1] = (time_s, qpm)
      else:
        dedup.append((time_s, qpm))
    self.events = dedup
    self.secs = [0.0]
    self.beats = [0.0]
    qpm = dedup[0][1]
    last_t = last_b = 0.0
    for time_s, event_qpm in dedup[1:]:
      last_b += (time_s - last_t) * qpm / 60.0
      last_t = time_s
      self.beats.append(last_b)
      self.secs.append(time_s)
      qpm = event_qpm
    if total_time > last_t:
      self.beats.append(last_b + (total_time - last_t) * qpm / 60.0)
      self.secs.append(total_time)

  def seconds_to_beat(self, seconds: float) -> float:
    if seconds <= 0:
      return 0.0
    for i in range(1, len(self.secs)):
      if seconds <= self.secs[i] + 1e-9:
        span = self.secs[i] - self.secs[i - 1]
        frac = 0.0 if span <= 0 else (seconds - self.secs[i - 1]) / span
        return self.beats[i - 1] + frac * (self.beats[i] - self.beats[i - 1])
    qpm = self.events[-1][1]
    return self.beats[-1] + (seconds - self.secs[-1]) * qpm / 60.0

  @property
  def total_beats(self) -> float:
    return self.beats[-1]

  def average_qpm(self) -> float:
    """The tempo that makes the piece take exactly as long as it is written to."""
    if self.secs[-1] <= 0:
      return self.events[-1][1]
    return self.total_beats * 60.0 / self.secs[-1]


def _steady(sequence, qpm: float):
  """A copy of the sequence in strict time, one tempo throughout."""
  steady = copy.deepcopy(sequence)
  curve = TempoCurve(sequence.tempos, float(sequence.total_time or 0.0))
  scale = 60.0 / qpm
  for note in steady.notes:
    note.start_time = curve.seconds_to_beat(note.start_time) * scale
    note.end_time = curve.seconds_to_beat(note.end_time) * scale
  for annotation in steady.text_annotations:
    annotation.time = curve.seconds_to_beat(annotation.time) * scale
  steady.total_time = curve.seconds_to_beat(float(sequence.total_time or 0.0)) * scale
  del steady.tempos[:]
  tempo = steady.tempos.add()
  tempo.time, tempo.qpm = 0.0, qpm
  return steady


def _beats_per_bar(sequence, fallback: int = 4) -> int:
  """The piece's meter, from the parse (the parser invents fractional ones)."""
  counts: Dict[int, int] = {}
  for signature in sequence.time_signatures:
    if signature.numerator > 0:
      counts[int(signature.numerator)] = counts.get(int(signature.numerator), 0) + 1
  if not counts:
    return fallback
  return max(counts.items(), key=lambda kv: kv[1])[0]


def _writable(sequence, beats: int):
  """A copy MIDI can hold: fractional meters cannot be written to a MIDI file."""
  clean = copy.deepcopy(sequence)
  del clean.time_signatures[:]
  del clean.key_signatures[:]
  meter = clean.time_signatures.add()
  meter.time, meter.numerator, meter.denominator = 0.0, beats, 4
  if not clean.tempos:
    tempo = clean.tempos.add()
    tempo.time, tempo.qpm = 0.0, 120.0
  return clean


# ---------------------------------------------------------------------------
# building a track
# ---------------------------------------------------------------------------

def _sequence_for_staff(piece, staff: Optional[int]):
  """The score as a NoteSequence with only one staff left in it."""
  from note_seq import musicxml_parser, musicxml_reader
  try:
    document = musicxml_parser.MusicXMLDocument(str(piece.musicxml))
  except Exception as e:
    raise SynthError('could not read %s: %s' % (pathlib.Path(piece.musicxml).name, e))
  if staff is not None:
    staves = staff_per_note(piece.musicxml)
    measures = document.parts[0].measures if document.parts else []
    if len(staves) != len(measures):
      raise SynthError('the score and its parse disagree about the measures (%d vs %d)'
                       % (len(staves), len(measures)))
    for index, measure in enumerate(measures):
      wanted = staves[index]
      if len(wanted) != len(measure.notes):
        raise SynthError('measure %d does not line up with its parse' % (index + 1))
      for position in range(len(measure.notes) - 1, -1, -1):
        if wanted[position] != staff:
          del measure.notes[position]
  try:
    return musicxml_reader.musicxml_to_sequence_proto(document)
  except Exception as e:
    raise SynthError('could not convert the score: %s' % e)


def reference_tempo(piece) -> Optional[Tuple[float, int]]:
  """(qpm, beats per bar) from the piece's steady reference MIDI, if it has one.

  The reference MIDI is what the measure map was fitted against, and it is already a
  steady-tempo render -- so the practice tracks borrow its grid and stay comparable
  with it rather than inventing a second tempo for the same piece.
  """
  from . import align
  if piece is None or piece.reference_midi is None:
    return None
  if not pathlib.Path(piece.reference_midi).is_file():
    return None
  try:
    _seconds_per_bar, beats, qpm = align.midi_grid(piece.reference_midi)
  except Exception:
    return None
  return (float(qpm), int(round(beats)))


def _sidecar(wav: pathlib.Path) -> pathlib.Path:
  return wav.with_suffix('.json')


def track_paths(cfg: AnalyzerConfig, piece, track: str) -> Tuple[pathlib.Path, pathlib.Path]:
  """Where a track's WAV and MIDI live, named after the score they came from.

  A replaced score gets new names rather than stale tracks; the old files are left
  behind for the cache to outlive.
  """
  if track not in TRACKS:
    raise SynthError('unknown track %r' % (track,))
  if piece is None or piece.musicxml is None:
    raise SynthError('this piece has no score to play from')
  stat = pathlib.Path(piece.musicxml).stat()
  stamp = hashlib.sha1(('%d:%f' % (stat.st_size, stat.st_mtime)).encode('utf-8')).hexdigest()[:8]
  base = cfg.tracks_dir / ('%s-%s-%s' % (piece.slug, track, stamp))
  return base.with_suffix('.wav'), base.with_suffix('.mid')


def build_track(cfg: AnalyzerConfig, piece, track: str) -> pathlib.Path:
  """The WAV for one track, rendering it if it is not cached yet.

  Returns:
    The path to the WAV.

  Raises:
    SynthError: when the score cannot be split, or FluidSynth or a soundfont is
      missing.
  """
  from note_seq import midi_io
  from . import align

  wav, midi = track_paths(cfg, piece, track)
  if wav.is_file() and wav.stat().st_size > 44:
    return wav
  cfg.tracks_dir.mkdir(parents=True, exist_ok=True)

  sequence = _sequence_for_staff(piece, STAFF_OF_TRACK[track])
  if not sequence.notes:
    raise SynthError('this score has nothing on the %s' % TRACK_LABELS[track])

  beats = _beats_per_bar(sequence)
  borrowed = reference_tempo(piece)
  curve = TempoCurve(sequence.tempos, float(sequence.total_time or 0.0))
  qpm = borrowed[0] if borrowed else curve.average_qpm()
  if borrowed:
    beats = borrowed[1] or beats
  steady = _steady(sequence, qpm)

  try:
    midi_io.sequence_proto_to_midi_file(_writable(steady, beats), str(midi))
  except Exception as e:
    raise SynthError('could not write the %s as MIDI: %s' % (TRACK_LABELS[track], e))
  try:
    align.render_midi_to_wav(midi, wav)
  except align.AlignError as e:
    raise SynthError(str(e))
  except Exception as e:
    raise SynthError('could not render the %s: %s' % (TRACK_LABELS[track], e))

  import soundfile as sf
  info = sf.info(str(wav))
  _sidecar(wav).write_text(json.dumps({
      'track': track, 'label': TRACK_LABELS[track], 'notes': len(sequence.notes),
      'qpm': round(qpm, 4), 'beats': beats,
      'seconds_per_bar': round(beats * 60.0 / qpm, 6) if qpm else None,
      'first_bar': 1, 'duration_s': round(info.frames / float(info.samplerate), 3),
      'score': pathlib.Path(piece.musicxml).name,
  }, indent=1) + '\n', encoding='utf-8')
  return wav


# ---------------------------------------------------------------------------
# what the caller needs to know
# ---------------------------------------------------------------------------

def _read_sidecar(cfg: AnalyzerConfig, piece, track: str) -> Optional[Dict[str, object]]:
  try:
    wav, _midi = track_paths(cfg, piece, track)
  except SynthError:
    return None
  path = _sidecar(wav)
  if not path.is_file():
    return None
  try:
    return json.loads(path.read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return None


def _planned_tempo(cfg: AnalyzerConfig, piece) -> Optional[Tuple[float, int, str, Optional[float]]]:
  """(qpm, beats, where it came from, duration if a track already knows it).

  A built track's sidecar is what its file on disk actually used, so that wins.
  Otherwise the piece's steady reference MIDI -- the tempo the tracks *will* be built
  at. Failing both, the score itself is parsed to work out its average tempo, which is
  what a piece with no question bank behind it needs: the phone has to know where the
  bars are before it can ask for one.
  """
  for track in TRACKS:
    sidecar = _read_sidecar(cfg, piece, track)
    if sidecar and sidecar.get('seconds_per_bar'):
      duration = sidecar.get('duration_s')
      return (float(sidecar.get('qpm') or 0.0), int(sidecar.get('beats') or 4),
              'built track', float(duration) if duration else None)
  borrowed = reference_tempo(piece)
  if borrowed:
    return (borrowed[0], borrowed[1], 'reference MIDI', None)
  if piece is None or piece.musicxml is None:
    return None
  try:
    sequence = _sequence_for_staff(piece, None)
  except SynthError:
    return None
  curve = TempoCurve(sequence.tempos, float(sequence.total_time or 0.0))
  qpm = curve.average_qpm()
  if not qpm:
    return None
  return (qpm, _beats_per_bar(sequence), 'score', None)


def grid(cfg: AnalyzerConfig, piece) -> Optional[Dict[str, object]]:
  """The tracks' bar grid: seconds per bar, beats, where bar 1 sits, and its length.

  One tempo throughout, so a bar's position is arithmetic rather than a fitted map --
  which is what lets the phone ask for "bars 40 to 43" of a hand and highlight them
  while they play.
  """
  plan = _planned_tempo(cfg, piece)
  if plan is None:
    return None
  qpm, beats, source, duration = plan
  if not qpm:
    return None
  return {'seconds_per_bar': beats * 60.0 / qpm, 'beats': beats, 'qpm': qpm,
          'first_bar': 1, 'source': source, 'duration_s': duration}


def available(cfg: AnalyzerConfig, piece) -> Dict[str, object]:
  """Which tracks this piece can be played in, and what is missing if not all.

  Answers from the score file alone -- one XML parse, about 30 ms -- so the phone can
  ask every time it opens a piece without waiting for a render.
  """
  tracks: List[str] = []
  reason = ''
  if piece is None or piece.musicxml is None:
    reason = 'this piece has no score to play from'
  elif not pathlib.Path(piece.musicxml).is_file():
    reason = 'the score is not on disk any more'
  else:
    try:
      staves = staff_per_note(piece.musicxml)
    except (SynthError, ET.ParseError) as e:
      reason = 'could not read the score: %s' % e
      staves = []
    if staves:
      if has_hands(staves):
        tracks = list(TRACKS)
      else:
        tracks = ['both']
        reason = 'this score is on one staff, so there are no hands to separate'
  built = [t for t in tracks if _sidecar(track_paths(cfg, piece, t)[0]).is_file()]
  return {'tracks': tracks, 'built': built, 'grid': grid(cfg, piece), 'reason': reason}
