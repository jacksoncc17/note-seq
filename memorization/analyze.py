"""Piece fingerprinting + quiz question-bank generator for memorization drills.

Usage:
  .venv/bin/python memorization/analyze.py <piece.musicxml|.xml|.mxl> \
      [--spq 8] [--phrase-len 4] [--chords-per-bar 1] \
      [--out output/fingerprint.json] [--no-midi] [--no-performance]

The tool turns a MusicXML score into a "ground-truth fingerprint" used to quiz
a player from memory:

  * global facts: measure count, key (from score + inferred), meter, tempo,
    pitch range, duration, climax
  * per measure: chord, top melody note, contour, note count, dynamics, rhythm
  * form: 4-bar phrase blocks compared on pitch content + rhythm, labeled A/B/C
  * a graduated question bank (tiers 0-3) written to JSON

Bar numbers are taken from the score's own <measure> elements, so "measure 57"
in a quiz means printed bar 57. Notes are aligned to bars by counting note
elements per bar (the parser emits exactly one note per non-rest element and
never reorders them), which avoids every drift problem a tempo/beat grid has.
When that alignment cannot be validated the tool falls back to a tempo-derived
time grid and says so in the output.
"""

import argparse
import json
import math
import os
import random
import sys
import xml.etree.ElementTree as ET
import zipfile

# Make the repo root importable when running from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ...and this directory, so `pieces` (the per-piece bank layout) resolves even
# when this file is loaded by path from the tutor.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pieces as _pieces                       # noqa: E402

from note_seq import chord_inference          # noqa: E402
from note_seq import midi_io                  # noqa: E402
from note_seq import musicxml_reader          # noqa: E402
from note_seq import sequences_lib            # noqa: E402
from note_seq.protobuf import music_pb2       # noqa: E402

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
KEY_NAMES = {0: 'C', 1: 'C#', 2: 'D', 3: 'D#', 4: 'E', 5: 'F', 6: 'F#', 7: 'G',
             8: 'G#', 9: 'A', 10: 'A#', 11: 'B'}
MODE_NAMES = {0: 'major', 1: 'minor', 2: 'not specified', 3: 'mixolydian',
              4: 'dorian', 5: 'phrygian', 6: 'lydian', 7: 'locrian'}
CHORD_SYMBOL = music_pb2.NoteSequence.TextAnnotation.CHORD_SYMBOL

# Krumhansl-Schmuckler key profiles.
MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66,
                 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69,
                 3.34, 3.17]


def note_name(pitch):
  return NOTE_NAMES[int(pitch) % 12] + str(int(pitch) // 12 - 1)


# --------------------------------------------------------------------------
# Tempo map (seconds <-> quarter notes)
# --------------------------------------------------------------------------

class TempoMap:
  """Piecewise-constant tempo map built from the score's tempo events."""

  def __init__(self, tempos, total_time):
    events = sorted((max(0.0, t.time), t.qpm) for t in tempos if t.qpm > 0)
    if not events:
      events = [(0.0, 120.0)]
    if events[0][0] > 0:
      events.insert(0, (0.0, events[0][1]))
    # Collapse events that share a time (keep the last one).
    dedup = []
    for t, q in events:
      if dedup and abs(dedup[-1][0] - t) < 1e-9:
        dedup[-1] = (t, q)
      else:
        dedup.append((t, q))
    self.events = dedup
    self.beats = [0.0]
    self.secs = [0.0]
    qpm = dedup[0][1]
    last_t = last_b = 0.0
    for t, q in dedup:
      if t > last_t:
        last_b += (t - last_t) * qpm / 60.0
        last_t = t
        self.beats.append(last_b)
        self.secs.append(t)
      qpm = q
    if total_time > self.secs[-1]:
      self.beats.append(last_b + (total_time - self.secs[-1]) * qpm / 60.0)
      self.secs.append(total_time)

  def qpm_at(self, t):
    qpm = self.events[0][1]
    for et, eq in self.events:
      if et <= t + 1e-9:
        qpm = eq
      else:
        break
    return qpm

  def quarter_at(self, t):
    return 60.0 / self.qpm_at(t)

  def beat_to_seconds(self, b):
    if b <= 0:
      return 0.0
    for i in range(1, len(self.beats)):
      if b <= self.beats[i] + 1e-9:
        span = self.beats[i] - self.beats[i - 1]
        frac = 0.0 if span <= 0 else (b - self.beats[i - 1]) / span
        return self.secs[i - 1] + frac * (self.secs[i] - self.secs[i - 1])
    qpm = self.events[-1][1]
    return self.secs[-1] + (b - self.beats[-1]) * 60.0 / qpm

  def seconds_to_beat(self, s):
    if s <= 0:
      return 0.0
    for i in range(1, len(self.secs)):
      if s <= self.secs[i] + 1e-9:
        span = self.secs[i] - self.secs[i - 1]
        frac = 0.0 if span <= 0 else (s - self.secs[i - 1]) / span
        return self.beats[i - 1] + frac * (self.beats[i] - self.beats[i - 1])
    return self.beats[-1] + (s - self.secs[-1]) * self.qpm_at(s) / 60.0


# --------------------------------------------------------------------------
# Reading the score's own measure structure
# --------------------------------------------------------------------------

def read_musicxml_root(path):
  """Return the ElementTree root for a .xml or compressed .mxl score."""
  if zipfile.is_zipfile(path):
    with zipfile.ZipFile(path) as z:
      names = [n for n in z.namelist()
               if n.lower().endswith('.xml') and 'meta-inf' not in n.lower()]
      if not names:
        raise ValueError('no XML found inside %s' % path)
      # Prefer the rootfile named by META-INF/container.xml.
      chosen = None
      try:
        container = ET.fromstring(z.read('META-INF/container.xml'))
        for rf in container.iter('rootfile'):
          full = rf.get('full-path')
          if full and full in z.namelist():
            chosen = full
            break
      except KeyError:
        pass
      if chosen is None:
        chosen = 'score.xml' if 'score.xml' in names else sorted(names)[0]
      return ET.fromstring(z.read(chosen))
  return ET.parse(path).getroot()


def xml_measure_structure(root):
  """Return (note_counts_per_measure, measure_numbers, meters, key_modes).

  note_counts_per_measure[i] is the number of non-rest note elements in bar i,
  summed over every part. Returns None if the score is not score-partwise.
  """
  if not root.tag.endswith('score-partwise'):
    return None
  parts = root.findall('.//part')
  if not parts:
    return None
  per_measure = []
  numbers = []
  for part in parts:
    for idx, measure in enumerate(part.findall('measure')):
      count = 0
      for note in measure.iter('note'):
        if note.find('rest') is not None:
          continue
        count += 1
      if idx >= len(per_measure):
        per_measure.append(0)
        num = measure.get('number')
        try:
          numbers.append(int(num))
        except (TypeError, ValueError):
          numbers.append(idx + 1)
      per_measure[idx] += count
  meters, modes = [], []
  for t in root.iter('time'):
    beats = [b.text for b in t.findall('beats')]
    types = [b.text for b in t.findall('beat-type')]
    if beats and types:
      try:
        meters.append((sum(int(b) for b in beats), int(types[0])))
      except ValueError:
        pass
  for k in root.iter('key'):
    m = k.find('mode')
    if m is not None and m.text:
      modes.append(m.text.strip())
  return per_measure, numbers, meters, modes


def _repair_zero_time_notes(measures):
  """Give zero-length notes a real position.

  The parser assigns grace notes no time at all (start_time = end_time = 0),
  which would strand them at the start of the piece and wreck any per-bar
  timing derived from them. Bar membership itself is unaffected, because notes
  are aligned to bars by counting, so it is enough to move each such note to
  the first real onset in its own bar.
  """
  for info in measures:
    bad = [n for n in info['notes']
           if n.end_time <= n.start_time and n.start_time <= 1e-9]
    if not bad:
      continue
    bad_ids = {id(n) for n in bad}
    good = [n for n in info['notes'] if id(n) not in bad_ids]
    t = min((n.start_time for n in good), default=0.0)
    for n in bad:
      n.start_time = t
      n.end_time = t + 0.01


def assign_notes_to_measures(seq, structure):
  """Align parsed notes to printed bar numbers by counting (single part).

  The parser emits one NoteSequence note per non-rest <note> element, keeping
  document order (measure by measure for a single part), so we can walk the
  note list alongside the per-bar counts. Returns a list of
  {'measure': n, 'notes': [...]} or None when the totals do not line up.
  """
  per_measure, numbers, _, _ = structure
  if sum(per_measure) != len(seq.notes):
    return None
  measures = [{'measure': n, 'notes': []} for n in numbers]
  cursor = 0
  for info, count in zip(measures, per_measure):
    info['notes'] = list(seq.notes[cursor:cursor + count])
    cursor += count
  if cursor != len(seq.notes):
    return None
  _repair_zero_time_notes(measures)
  return measures


def assign_notes_to_measures_multipart(seq, root):
  """Per-part counting for scores with more than one <part>."""
  parts = root.findall('.//part')
  counts_per_part = []
  for part in parts:
    counts = []
    for measure in part.findall('measure'):
      c = 0
      for note in measure.iter('note'):
        if note.find('rest') is not None:
          continue
        c += 1
      counts.append(c)
    counts_per_part.append(counts)
  if sum(sum(c) for c in counts_per_part) != len(seq.notes):
    return None
  numbers = []
  for idx, measure in enumerate(parts[0].findall('measure')):
    try:
      numbers.append(int(measure.get('number')))
    except (TypeError, ValueError):
      numbers.append(idx + 1)
  measures = [{'measure': num, 'notes': []} for num in numbers]
  cursor = 0
  for counts in counts_per_part:
    for i, c in enumerate(counts):
      if i >= len(measures):
        return None
      measures[i]['notes'].extend(seq.notes[cursor:cursor + c])
      cursor += c
  if cursor != len(seq.notes):
    return None
  _repair_zero_time_notes(measures)
  return measures


def time_grid_measures(seq, tm, meter):
  """Fallback: derive bars from durations and assign notes by time.

  Used only when the score's own measure structure cannot be aligned.
  """
  num, den = meter
  beats_per_bar = num * 4.0 / den
  total_beats = tm.beats[-1]
  n_bars = max(1, int(math.ceil(total_beats / beats_per_bar - 1e-6)))
  bounds = []
  for k in range(n_bars):
    bounds.append((k + 1, tm.beat_to_seconds(k * beats_per_bar),
                   tm.beat_to_seconds((k + 1) * beats_per_bar)))
  measures = [{'measure': n, 'notes': []} for n, _, _ in bounds]
  for note in seq.notes:
    for i, (_, t0, t1) in enumerate(bounds):
      if t0 - 1e-9 <= note.start_time < t1 - 1e-9:
        measures[i]['notes'].append(note)
        break
  return measures


# --------------------------------------------------------------------------
# Per-measure analysis
# --------------------------------------------------------------------------

RHYTHM_LABELS = ['W', 'H.', 'H', 'Ht', 'Q.', 'Q', 'Qt', 'E.', 'E', 'Et', 'S', 'St']
RHYTHM_BEATS = [4.0, 3.0, 2.0, 4 / 3, 1.5, 1.0, 2 / 3, 0.75, 0.5, 1 / 3, 0.25, 1 / 6]


def xml_measure_onsets(root):
  """Per bar: onset positions in beats, straight from the score's divisions.

  Far more reliable than timing-derived onsets on engraved scores: no tempo map
  is involved, and tied continuations (which the parser turns into separate
  notes even though nothing is re-articulated) are excluded.
  """
  parts = root.findall('.//part')
  if not parts:
    return None
  n = max(len(p.findall('measure')) for p in parts)
  onsets = [set() for _ in range(n)]
  for part in parts:
    divisions = 1
    for idx, measure in enumerate(part.findall('measure')):
      if idx >= n:
        break
      cursor = 0  # in divisions, from the bar's start
      for el in measure:
        if el.tag == 'attributes':
          d = el.find('divisions')
          if d is not None and d.text:
            try:
              divisions = int(d.text) or divisions
            except ValueError:
              pass
        elif el.tag in ('backup', 'forward'):
          d = el.find('duration')
          if d is not None and d.text:
            delta = int(d.text)
            cursor += delta if el.tag == 'forward' else -delta
        elif el.tag == 'note':
          if el.find('chord') is not None:
            continue  # sounds with the previous note; does not advance
          d = el.find('duration')
          dur = int(d.text) if d is not None and d.text else 0
          tie = el.find('tie')
          is_tie_stop = tie is not None and tie.get('type') == 'stop'
          if el.find('grace') is not None:
            dur = 0
          if not is_tie_stop and el.find('rest') is None:
            onsets[idx].add(round(cursor / divisions, 4))
          cursor += dur
  return [sorted(s) for s in onsets]


def rhythm_pattern(notes, tm, beats_per_bar, xml_onsets=None):
  """Onset rhythm of a bar, e.g. 'Q Q E E'.

  Simultaneous notes (a left-hand chord, or three voices striking together)
  collapse into ONE event, and each event lasts until the next onset (the last
  one until the barline). That describes what you play and how the bar
  subdivides, and it always sums to exactly one bar -- unlike raw note
  durations, which in a multi-voice piano score spill past the barline via
  ties and held voices.
  """
  if not notes:
    return 'rest'
  if xml_onsets:
    positions = list(xml_onsets)
  else:
    # Fall back to timing: onsets in the score's own beat units.
    t0 = min(n.start_time for n in notes)
    quarter = tm.quarter_at(t0)
    positions = sorted({round((n.start_time - t0) / quarter, 4)
                        for n in notes})
  pat = []
  for i, pos in enumerate(positions):
    nxt = positions[i + 1] if i + 1 < len(positions) else beats_per_bar
    span = nxt - pos
    if span <= 1e-3:
      continue
    idx = min(range(len(RHYTHM_BEATS)), key=lambda k: abs(RHYTHM_BEATS[k] - span))
    pat.append(RHYTHM_LABELS[idx])
  return ' '.join(pat) if pat else 'rest'


def measure_analysis(measures, tm, beats_per_bar, onsets=None):
  for i, info in enumerate(measures):
    notes = info['notes']
    bar_onsets = onsets[i] if onsets and i < len(onsets) else None
    if not notes:
      info.update({'notes_count': 0, 'chord': None, 'melody': None,
                   'melody_pitch': None, 'contour': 'rest', 'avg_velocity': 0,
                   'pitches': [], 'first_note': None, 'last_note': None,
                   'rhythm': 'rest'})
      continue
    ordered = sorted(notes, key=lambda n: (n.start_time, -n.pitch))
    top = max(n.pitch for n in notes)
    half = max(1, len(ordered) // 2)
    early = sum(n.pitch for n in ordered[:half]) / half
    late = sum(n.pitch for n in ordered[half:]) / max(len(ordered[half:]), 1)
    contour = ('up' if late - early > 0.5 else
               'down' if early - late > 0.5 else 'level')
    info.update({
        'notes_count': len(notes),
        'chord': None,
        'melody': note_name(top),
        'melody_pitch': top,
        'contour': contour,
        'avg_velocity': round(sum(n.velocity for n in notes) / len(notes), 1),
        'pitches': [note_name(p) for p in sorted({n.pitch for n in notes})],
        'first_note': note_name(ordered[0].pitch),
        'last_note': note_name(ordered[-1].pitch),
        'rhythm': rhythm_pattern(notes, tm, beats_per_bar, bar_onsets),
    })
  return measures


def infer_chords_per_measure(measures, spq, meter):
  """Measure -> chord symbol, inferring each bar on its own.

  Each bar becomes a tiny sequence with a constant tempo and the score's
  meter, so exactly one chord covers exactly one bar. This is immune to the
  tempo drift and meter artifacts that break whole-piece inference on real
  engraved scores.
  """
  num, den = meter
  for info in measures:
    notes = info['notes']
    if not notes:
      continue
    sub = music_pb2.NoteSequence()
    sub.ticks_per_quarter = 220
    ts = sub.time_signatures.add()
    ts.numerator, ts.denominator = num, den
    tempo = sub.tempos.add()
    tempo.time, tempo.qpm = 0.0, 120.0
    t0 = min(n.start_time for n in notes)
    t1 = max(n.end_time for n in notes)
    for n in notes:
      nn = sub.notes.add()
      nn.pitch = n.pitch
      nn.velocity = n.velocity
      nn.start_time = max(0.0, n.start_time - t0)
      nn.end_time = max(nn.start_time + 1e-3, n.end_time - t0)
    sub.total_time = max(t1 - t0, 1e-3)
    try:
      q = sequences_lib.quantize_note_sequence(sub, spq)
      chord_inference.infer_chords_for_sequence(q, chords_per_bar=1)
    except Exception:  # noqa: BLE001 - leave the bar unlabelled
      continue
    figures = [ta.text for ta in q.text_annotations
               if ta.annotation_type == CHORD_SYMBOL]
    figures = [f for f in figures if f and f != 'N.C.']
    if figures:
      info['chord'] = figures[0]
  return measures


# --------------------------------------------------------------------------
# Key, form, facts
# --------------------------------------------------------------------------

def _pearson(a, b):
  n = len(a)
  ma, mb = sum(a) / n, sum(b) / n
  num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
  da = math.sqrt(sum((x - ma) ** 2 for x in a))
  db = math.sqrt(sum((y - mb) ** 2 for y in b))
  return num / (da * db) if da and db else 0.0


def krumhansl_key(notes):
  """Krumhansl-Schmuckler key estimate from duration-weighted pitch classes."""
  hist = [0.0] * 12
  for n in notes:
    hist[n.pitch % 12] += max(n.end_time - n.start_time, 1e-3)
  if not sum(hist):
    return None, 0.0
  best, best_score = None, -2.0
  for tonic in range(12):
    for profile, mode in ((MAJOR_PROFILE, 'major'), (MINOR_PROFILE, 'minor')):
      rot = [profile[(i - tonic) % 12] for i in range(12)]
      score = _pearson(hist, rot)
      if score > best_score:
        best, best_score = (KEY_NAMES[tonic], mode), score
  return '%s %s' % best, round(best_score, 3)


def infer_key(seq):
  return krumhansl_key(seq.notes)


def _cosine(a, b):
  num = sum(x * y for x, y in zip(a, b))
  da = math.sqrt(sum(x * x for x in a))
  db = math.sqrt(sum(x * x for x in b))
  return num / (da * db) if da and db else 0.0


def _label(i):
  """0 -> A, 25 -> Z, 26 -> AA (spreadsheet-style section labels)."""
  s = ''
  i += 1
  while i > 0:
    i, r = divmod(i - 1, 26)
    s = chr(ord('A') + r) + s
  return s


def xml_measure_content(root):
  """Per bar: multiset of (onset, pitch, duration) in beats, all parts.

  Read straight from the score's divisions, so it is exact and completely
  independent of the tempo map -- which matters because these playback-style
  scores carry a tempo event per note, and any duration measured in seconds
  would differ between two identical bars played at different tempos.
  """
  parts = root.findall('.//part')
  if not parts:
    return None
  n = max(len(p.findall('measure')) for p in parts)
  content = [{} for _ in range(n)]
  for part in parts:
    divisions = 1
    for idx, measure in enumerate(part.findall('measure')):
      if idx >= n:
        break
      cursor = 0
      last = None
      for el in measure:
        if el.tag == 'attributes':
          d = el.find('divisions')
          if d is not None and d.text:
            try:
              divisions = int(d.text) or divisions
            except ValueError:
              pass
        elif el.tag in ('backup', 'forward'):
          d = el.find('duration')
          if d is not None and d.text:
            delta = int(d.text)
            cursor += delta if el.tag == 'forward' else -delta
        elif el.tag == 'note':
          d = el.find('duration')
          dur = int(d.text) if d is not None and d.text else 0
          pitch = el.find('pitch')
          label = None
          if pitch is not None:
            step = pitch.find('step')
            alter = pitch.find('alter')
            octave = pitch.find('octave')
            label = '%s%s%s' % (step.text if step is not None else '?',
                                alter.text if alter is not None else '',
                                octave.text if octave is not None else '')
          if el.find('grace') is not None:
            dur = 0
          if el.find('chord') is not None:
            # Sounds with the previous note: same onset and duration.
            if last is not None and label is not None:
              key = (last[0], label, last[1])
              content[idx][key] = content[idx].get(key, 0) + 1
          else:
            onset = round(cursor / divisions, 4)
            beats = round(dur / divisions, 4)
            last = (onset, beats)
            if el.find('rest') is None and label is not None:
              key = (onset, label, beats)
              content[idx][key] = content[idx].get(key, 0) + 1
            cursor += dur
  return content


def bar_features_from_content(content):
  """Turn per-bar note content into features for repeat detection."""
  feats = []
  for bar in content:
    chroma = [0.0] * 12
    for (_onset, label, beats) in bar:
      step = label[0]
      alter = 1 if '#' in label else (-1 if '-' in label else 0)
      base = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}[step]
      chroma[(base + alter) % 12] += max(beats, 0.01)
    total = sum(chroma) or 1.0
    feats.append({'content': bar, 'chroma': [c / total for c in chroma]})
  return feats


def bar_features(measures, tm, onsets=None):
  """Fallback feature builder when the score's own content is unavailable."""
  feats = []
  for i, info in enumerate(measures):
    notes = info['notes']
    if not notes:
      feats.append({'content': {}, 'chroma': [0.0] * 12})
      continue
    chroma = [0.0] * 12
    for n in notes:
      chroma[n.pitch % 12] += max(n.end_time - n.start_time, 1e-3)
    total = sum(chroma) or 1.0
    content = {}
    t0 = min(n.start_time for n in notes)
    quarter = tm.quarter_at(t0)
    for n in notes:
      slot = int(round((n.start_time - t0) / (quarter / 4.0)))
      beats = (n.end_time - n.start_time) / max(quarter, 1e-6)
      key = (slot, note_name(n.pitch), round(beats, 3))
      content[key] = content.get(key, 0) + 1
    feats.append({'content': content, 'chroma': [c / total for c in chroma]})
  return feats


def _jaccard(a, b):
  if not a and not b:
    return 1.0
  return len(a & b) / max(len(a | b), 1)


def _multiset_similarity(a, b):
  if not a and not b:
    return 1.0
  inter = sum(min(a[k], b[k]) for k in a.keys() & b.keys())
  union = sum(a.values()) + sum(b.values()) - inter
  return inter / union if union else 0.0


def bar_similarity(fa, fb):
  """Content dominates; chroma keeps small variants reachable."""
  if not fa['content'] or not fb['content']:
    return 1.0 if fa['content'] == fb['content'] else 0.0
  return (0.75 * _multiset_similarity(fa['content'], fb['content'])
          + 0.25 * _cosine(fa['chroma'], fb['chroma']))


def find_repeats(feats, min_len, threshold):
  """Maximal diagonal runs of similar bars.

  Returns (start_a, start_b, length, min_similarity) with 0-based indices.
  """
  n = len(feats)
  sim = [[0.0] * n for _ in range(n)]
  for i in range(n):
    for j in range(i + 1, n):
      s = bar_similarity(feats[i], feats[j])
      sim[i][j] = sim[j][i] = s
  runs = []
  for i in range(n):
    for j in range(i + 1, n):
      limit = min(n - j, j - i)
      if limit < min_len:
        continue
      length = 0
      while length < limit and sim[i + length][j + length] >= threshold:
        length += 1
      if length >= min_len:
        worst = min(sim[i + k][j + k] for k in range(length))
        runs.append((i, j, length, worst))
  # Drop runs wholly contained in a longer run with the same offset.
  keep = []
  for r in sorted(runs, key=lambda x: -x[2]):
    if not any(r[1] - r[0] == k[1] - k[0] and r[0] >= k[0]
               and r[0] + r[2] <= k[0] + k[2] for k in keep):
      keep.append(r)
  return keep


def repeat_map(measures, runs, top=20, exact_only=False):
  """Rank repeated passages for the drill, preferring exact ones.

  A run counts as exact when every bar pair in it matches to within rounding,
  which for score-derived content means the two passages are literally the
  same music -- the strongest possible memory aid, so those are ranked first.
  """
  numbers = [m['measure'] for m in measures]
  scored = []
  for i, j, length, worst in runs:
    exact = worst >= 0.999
    if exact_only and not exact:
      continue
    scored.append((exact, length, i, j, worst))
  scored.sort(key=lambda r: (not r[0], -r[1], r[2]))
  accepted = []
  out = []
  for exact, length, i, j, worst in scored:
    # Skip a passage already covered by an accepted, longer one (either span).
    if any(not (j + length <= b or j >= b + al or i + length <= a or i >= a + al)
           for a, b, al in accepted):
      continue
    accepted.append((i, j, length))
    out.append({
        'from_measure': numbers[i],
        'to_measure': numbers[i + length - 1],
        'repeat_measure': numbers[j],
        'repeat_end_measure': numbers[j + length - 1],
        'length': length,
        'exact': exact,
        'similarity': round(worst, 3),
    })
    if len(out) >= top:
      break
  return out


def sections_from_key_areas(areas):
  """Label key areas A, B, C... in order of appearance."""
  return [{
      'label': _label(i),
      'key': a['key'],
      'start_measure': a['start_measure'],
      'end_measure': a['end_measure'],
      'measures': a['end_measure'] - a['start_measure'] + 1,
      'correlation': a['correlation'],
  } for i, a in enumerate(areas)]


def key_areas(measures, window=8):
  """Windowed key estimate, merged into contiguous key regions.

  This is a *tonal centre* heuristic, not the score's key plan: it can report a
  key the music only tonicises without any key-signature change. Prefer
  `sections_from_key_signatures` when the score changes signature.
  """
  areas = []
  for start in range(0, len(measures), window):
    group = measures[start:start + window]
    notes = [n for info in group for n in info['notes']]
    key, score = krumhansl_key(notes)
    if key is None:
      continue
    area = {'key': key, 'start_measure': group[0]['measure'],
            'end_measure': group[-1]['measure'], 'correlation': score}
    if areas and areas[-1]['key'] == area['key']:
      areas[-1]['end_measure'] = area['end_measure']
    else:
      areas.append(area)
  return areas


def sections_from_key_signatures(seq, measures):
  """Sections from the score's own key signature changes.

  Boundaries come from the notated key signatures (authoritative), while each
  region's key is named by its actual pitch content -- engraved piano scores
  routinely write an empty signature for a minor key, so trusting the
  signature's own major/minor would label A minor as "C major".
  """
  starts = [(m['measure'], min(n.start_time for n in m['notes']))
            for m in measures if m['notes']]
  if not starts or not seq.key_signatures:
    return None
  events = []
  for k in seq.key_signatures:
    bar = min(starts, key=lambda s: abs(s[1] - k.time))[0]
    if events and events[-1]['bar'] == bar:
      continue
    events.append({'bar': bar, 'signature':
                   '%s %s' % (KEY_NAMES.get(k.key, '?'),
                              MODE_NAMES.get(k.mode, ''))})
  sections = []
  for i, ev in enumerate(events):
    end = events[i + 1]['bar'] - 1 if i + 1 < len(events) else measures[-1]['measure']
    notes = [n for m in measures
             if ev['bar'] <= m['measure'] <= end for n in m['notes']]
    key, score = krumhansl_key(notes)
    sections.append({
        'label': _label(i),
        'key': key or ev['signature'],
        'signature_in_score': ev['signature'],
        'start_measure': ev['bar'],
        'end_measure': end,
        'measures': end - ev['bar'] + 1,
        'correlation': score,
    })
  return sections


def climax_measure(measures):
  scored = [m for m in measures if m['notes_count']]
  if not scored:
    return None
  lo = min(m['melody_pitch'] for m in scored)
  hi = max(m['melody_pitch'] for m in scored)
  maxn = max(m['notes_count'] for m in scored)
  best, best_s = None, -1.0
  for m in scored:
    s = (0.4 * (m['melody_pitch'] - lo) / max(hi - lo, 1)
         + 0.3 * m['avg_velocity'] / 127.0
         + 0.3 * m['notes_count'] / maxn)
    if s > best_s:
      best, best_s = m['measure'], s
  return best


def global_facts(seq, measures, tm, key_inferred, key_score, key_mode_declared,
                 meters):
  pitches = [n.pitch for n in seq.notes]
  key_from_score = None
  if seq.key_signatures:
    ks = seq.key_signatures[0]
    key_from_score = '%s %s' % (KEY_NAMES.get(ks.key, '?'),
                                MODE_NAMES.get(ks.mode, ''))
  return {
      'num_measures': len(measures),
      'measures_with_notes': sum(1 for m in measures if m['notes_count']),
      'total_notes': len(seq.notes),
      'num_distinct_pitches': len(set(pitches)),
      'lowest_note': note_name(min(pitches)),
      'highest_note': note_name(max(pitches)),
      'lowest_pitch': min(pitches),
      'highest_pitch': max(pitches),
      'key_from_score': key_from_score,
      'key_mode_declared_in_score': key_mode_declared,
      'key_inferred': key_inferred,
      'key_inferred_correlation': key_score,
      'tempos_qpm': sorted({round(t.qpm, 2) for t in seq.tempos})[:12] or [120.0],
      'num_tempo_events': len(seq.tempos),
      'time_signatures_declared': ['%d/%d' % m for m in meters] or ['4/4'],
      'time_signatures_parsed': ['%d/%d' % (t.numerator, t.denominator)
                                 for t in seq.time_signatures][:12],
      'total_time_s': round(seq.total_time, 1),
      'total_beats': round(tm.beats[-1], 2),
      'title': seq.sequence_metadata.title,
      'composers': list(seq.sequence_metadata.composers),
  }


# --------------------------------------------------------------------------
# Question bank
# --------------------------------------------------------------------------

def build_question_bank(measures, sections, facts, climax, repetitions,
                        include_performance):
  rng = random.Random(42)
  q = []

  def add(tier, qtype, prompt, answer, options=None, hint=None):
    item = {'id': 'q%03d' % (len(q) + 1), 'tier': tier, 'type': qtype,
            'prompt': prompt, 'answer': str(answer)}
    if options:
      item['options'] = options
    if hint:
      item['hint'] = hint
    q.append(item)

  # ---- Tier 0: structure ----
  add(0, 'count', 'How many measures is the whole piece?', facts['num_measures'])
  add(0, 'key', 'What key is the piece in?', facts['key_inferred'],
      hint='score says: %s' % (facts['key_from_score'] or 'nothing'))
  add(0, 'meter', 'What is the time signature?',
      ' / '.join(facts['time_signatures_declared']))
  add(0, 'range', 'What is the lowest note you play?', facts['lowest_note'])
  add(0, 'range', 'What is the highest note you play?', facts['highest_note'])
  if sections:
    add(0, 'form', 'How many key areas does the piece move through?', len(sections))
    add(0, 'form', 'What is the sequence of key areas (section letters)?',
        ' '.join(s['label'] for s in sections))
    key_list = sorted({s['key'] for s in sections})
    for s in sections:
      add(0, 'section_key',
          'What key is section %s (measures %d-%d) in?'
          % (s['label'], s['start_measure'], s['end_measure']),
          s['key'], options=key_list)
      add(0, 'section_start',
          'At which measure does section %s begin?' % s['label'],
          s['start_measure'], hint='%s section, %d bars long'
          % (s['key'], s['measures']))
    for s in sections[1:]:
      add(0, 'key_change',
          'The music changes key at this point: at which measure does the '
          '%s stretch begin?' % s['key'], s['start_measure'],
          hint='key signature in the score changes here')
    # Probe a spread of measures for "which key area am I in?" knowledge.
    step = max(1, len(measures) // 8)
    for info in measures[::step][:8]:
      num = info['measure']
      area = next((s for s in sections
                   if s['start_measure'] <= num <= s['end_measure']), None)
      if area:
        add(0, 'measure_key', 'What key area is measure %d in?' % num,
            area['key'], options=key_list)
  for rep in repetitions:
    add(0, 'repeat',
        'The passage in measures %d-%d comes back later. At which measure does '
        'it return?' % (rep['from_measure'], rep['to_measure']),
        rep['repeat_measure'],
        hint='%d bars long, returning as m%d-m%d'
        % (rep['length'], rep['repeat_measure'], rep['repeat_end_measure']))
  if climax:
    add(0, 'climax', 'Which measure is the climax of the piece?', climax,
        hint='highest register + loudest + densest')

  # ---- Tier 1: harmony ----
  chord_bars = [m for m in measures if m['chord']]
  all_chords = sorted({m['chord'] for m in chord_bars})
  for m in chord_bars:
    opts = [m['chord']]
    pool = [c for c in all_chords if c != m['chord']]
    rng.shuffle(pool)
    opts += pool[:3]
    rng.shuffle(opts)
    add(1, 'chord', 'What is the chord in measure %d?' % m['measure'],
        m['chord'], options=opts)
  if chord_bars:
    for start in range(0, min(len(chord_bars), 16), 4):
      group = chord_bars[start:start + 4]
      add(1, 'progression',
          'What is the chord progression of measures %d-%d?'
          % (group[0]['measure'], group[-1]['measure']),
          ' - '.join(m['chord'] for m in group))
    add(1, 'chord', 'What is the final chord of the piece?', chord_bars[-1]['chord'])
    add(1, 'chord_count', 'How many distinct chords appear in the piece?',
        len(all_chords))

  # ---- Tier 2: surface ----
  played = [m for m in measures if m['notes_count']]
  if played:
    add(2, 'first_note', 'What is the very first note of the piece?',
        played[0]['first_note'])
    add(2, 'last_note', 'What is the very last note of the piece?',
        played[-1]['last_note'])
    busiest = max(played, key=lambda m: m['notes_count'])
    add(2, 'density', 'Which measure has the most notes?', busiest['measure'])
    thinnest = min(played, key=lambda m: m['notes_count'])
    add(2, 'density', 'Which measure has the fewest notes?', thinnest['measure'])
  for m in played:
    add(2, 'contour',
        'In measure %d, does the top line move up, down, or stay level?'
        % m['measure'], m['contour'], options=['up', 'down', 'level'])
    add(2, 'note_count', 'How many notes sound in measure %d?' % m['measure'],
        m['notes_count'])
    add(2, 'top_note', 'What is the highest note in measure %d?' % m['measure'],
        m['melody'])
    add(2, 'rhythm', 'What is the rhythm of measure %d?' % m['measure'],
        m['rhythm'])

  # ---- Tier 3: performance tasks (self-graded, at the piano) ----
  if include_performance:
    entry_points = sorted({1, climax or 1,
                           *(s['start_measure'] for s in sections),
                           max(2, facts['num_measures'] - 3)})
    for ep in entry_points:
      add(3, 'play_from',
          'At the piano: play from measure %d, cold, to the end of its phrase.'
          % ep, 'perform', hint='entry point: measure %d' % ep)
    if sections:
      add(3, 'play_section',
          'Play the opening section from memory (measures %d-%d).'
          % (sections[0]['start_measure'], sections[0]['end_measure']), 'perform')
    add(3, 'play_ending',
        'Play the final 4 measures from memory.', 'perform')
  return q


def average_tempo(seq, tm):
  """Tempo that preserves the piece's overall duration.

  LilyPond-style scores carry hundreds of expressive tempo events (one per
  note, including ritardando ramps), so "most common tempo" is meaningless;
  total beats divided by total time is the honest steady tempo.
  """
  if not tm.secs or tm.secs[-1] <= 0:
    return 120.0
  return tm.beats[-1] / tm.secs[-1] * 60.0


def write_reference_midis(seq, tm, meters, out_base, steady_qpm=None):
  """Write a faithful MIDI plus a steady-tempo practice MIDI.

  The parsed sequence can carry junk time signatures (the parser invents
  "fractional" meters for measures whose durations do not add up), and MIDI
  cannot represent them, so the copies written here get sane ones.
  """
  import copy

  meter = (meters or [(4, 4)])[0]
  written = []

  # 1. Faithful timing (keeps every expressive tempo change).
  faithful = copy.deepcopy(seq)
  del faithful.time_signatures[:]
  del faithful.key_signatures[:]
  ts = faithful.time_signatures.add()
  ts.time, ts.numerator, ts.denominator = 0.0, meter[0], meter[1]
  if not faithful.tempos:
    t = faithful.tempos.add()
    t.time, t.qpm = 0.0, 120.0
  path = out_base + '.mid'
  midi_io.sequence_proto_to_midi_file(faithful, path)
  written.append(path)

  # 2. Steady pulse version, for practising at a metronome-like tempo.
  qpm = steady_qpm or average_tempo(seq, tm)
  steady = copy.deepcopy(seq)
  del steady.time_signatures[:]
  del steady.key_signatures[:]
  ts = steady.time_signatures.add()
  ts.time, ts.numerator, ts.denominator = 0.0, meter[0], meter[1]
  del steady.tempos[:]
  t = steady.tempos.add()
  t.time, t.qpm = 0.0, qpm
  scale = 60.0 / qpm
  for n in steady.notes:
    n.start_time = tm.seconds_to_beat(n.start_time) * scale
    n.end_time = tm.seconds_to_beat(n.end_time) * scale
  for ta in steady.text_annotations:
    ta.time = tm.seconds_to_beat(ta.time) * scale
  steady.total_time = tm.beats[-1] * scale
  path = out_base + '_steady.mid'
  midi_io.sequence_proto_to_midi_file(steady, path)
  written.append(path)
  return written, qpm


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument('file')
  ap.add_argument('--spq', type=int, default=8,
                  help='quantization steps per quarter note (default 8)')
  ap.add_argument('--repeat-min-len', type=int, default=4,
                  help='minimum bars for two passages to count as a repeat')
  ap.add_argument('--repeat-threshold', type=float, default=0.95,
                  help='bar similarity needed to extend a repeat (0-1). With '
                       'score-derived content, 0.95+ means "essentially the '
                       'same music"; lower it to catch freer returns.')
  ap.add_argument('--key-window', type=int, default=8,
                  help='bars per window for key-area detection')
  ap.add_argument('--out', default=None)
  ap.add_argument('--name', default=None,
                  help='piece slug; defaults to the score filename. Each piece '
                       'gets its own directory under output/pieces/, so adding a '
                       'piece never overwrites an existing question bank.')
  ap.add_argument('--no-midi', action='store_true')
  ap.add_argument('--steady-qpm', type=float, default=None,
                  help='steady practice tempo (default: average tempo). '
                       'Playback scores often carry inflated tempo maps, so a '
                       'value from the printed tempo marking is often better.')
  ap.add_argument('--midi-only', action='store_true',
                  help='only rewrite the reference MIDI files, then exit')
  ap.add_argument('--no-performance', action='store_true',
                  help='omit tier-3 piano tasks from the question bank')
  args = ap.parse_args()

  seq = musicxml_reader.musicxml_file_to_sequence_proto(args.file)
  if not seq.notes:
    raise SystemExit('No notes found in %s' % args.file)
  tm = TempoMap(seq.tempos, seq.total_time)

  # Which bar grid can we trust? The score's own <measure> elements are the
  # authority; a tempo-derived grid is only a fallback.
  root = None
  structure = None
  try:
    root = read_musicxml_root(args.file)
    structure = xml_measure_structure(root)
  except Exception as e:  # noqa: BLE001
    print('could not read score structure (%s)' % e, file=sys.stderr)

  measures = None
  grid_source = 'time-grid'
  meters = []
  key_mode_declared = False
  if structure is not None:
    per_measure, _, meters, modes = structure
    key_mode_declared = bool(modes)
    n_parts = len(root.findall('.//part'))
    if n_parts > 1:
      measures = assign_notes_to_measures_multipart(seq, root)
    else:
      measures = assign_notes_to_measures(seq, structure)
    if measures is not None:
      grid_source = 'score-measures'
    else:
      print('note counts did not line up with the score (%d note elements vs '
            '%d parsed notes); falling back to a time grid'
            % (sum(per_measure), len(seq.notes)), file=sys.stderr)

  declared_meters = list(dict.fromkeys(meters))
  slug = args.name or _pieces.slugify(args.file)
  out = args.out or _pieces.fingerprint_path(slug)
  if args.midi_only:
    paths, qpm = write_reference_midis(seq, tm, declared_meters,
                                       os.path.splitext(out)[0], args.steady_qpm)
    print('reference midi (expressive): %s' % paths[0])
    print('reference midi (steady %g qpm): %s' % (qpm, paths[1]))
    return
  if measures is None:
    measures = time_grid_measures(seq, tm, declared_meters or [(4, 4)])

  primary_meter = declared_meters[0] if declared_meters else (4, 4)
  onsets = None
  if root is not None and grid_source == 'score-measures':
    try:
      onsets = xml_measure_onsets(root)
    except Exception as e:  # noqa: BLE001
      print('could not read bar onsets (%s)' % e, file=sys.stderr)
  measures = measure_analysis(measures, tm,
                              primary_meter[0] * 4.0 / primary_meter[1], onsets)
  infer_chords_per_measure(measures, args.spq, primary_meter)

  # Section boundaries: the score's own key signatures when it has them,
  # otherwise a tonal-centre heuristic (reported as such).
  sections = sections_from_key_signatures(seq, measures)
  section_basis = 'key-signatures'
  if not sections or len(sections) < 2:
    areas = key_areas(measures, args.key_window)
    if len(areas) >= 2:
      sections = sections_from_key_areas(areas)
      section_basis = 'tonal-areas (heuristic)'
      for s in sections:
        s['signature_in_score'] = '-'
    elif not sections:
      sections = []
      section_basis = 'none'
  content = None
  if root is not None:
    try:
      content = xml_measure_content(root)
    except Exception as e:  # noqa: BLE001
      print('could not read bar content (%s)' % e, file=sys.stderr)
  if content and len(content) == len(measures):
    feats = bar_features_from_content(content)
    repeat_basis = 'score-content'
  else:
    feats = bar_features(measures, tm, onsets)
    repeat_basis = 'timing (heuristic)'
  repetitions = repeat_map(measures, find_repeats(
      feats, args.repeat_min_len, args.repeat_threshold))
  key_inferred, key_score = infer_key(seq)
  facts = global_facts(seq, measures, tm, key_inferred, key_score,
                       key_mode_declared, declared_meters or [(4, 4)])
  climax = climax_measure(measures)
  questions = build_question_bank(measures, sections, facts, climax, repetitions,
                                  not args.no_performance)

  # Protobuf note objects are not JSON-serialisable: keep them internal.
  measures_json = [{k: v for k, v in m.items() if k != 'notes'}
                   for m in measures]

  fingerprint = {
      'source_file': os.path.abspath(args.file),
      'grid_source': grid_source,
      'section_basis': section_basis,
      'repeat_basis': repeat_basis,
      'global': facts,
      'sections': sections,
      'climax_measure': climax,
      'repetitions': repetitions,
      'measures': measures_json,
      'questions': questions,
  }
  os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
  with open(out, 'w') as f:
    json.dump(fingerprint, f, indent=2)
  _pieces.record_piece(slug, {
      'source_file': os.path.abspath(args.file),
      'title': facts.get('title') or os.path.basename(args.file),
      'composers': facts.get('composers') or [],
      'bars': facts.get('num_measures'),
      'questions': len(questions),
      'key': facts.get('key_inferred'),
      'fingerprint': os.path.abspath(out),
      'grid_source': grid_source,
  })

  print('bar grid: %s (%d measures, %d with notes)'
        % (grid_source, facts['num_measures'], facts['measures_with_notes']))
  print('Wrote %s' % out)
  print(json.dumps(facts, indent=2))
  print('sections (%s): %s' % (section_basis, ' | '.join(
      '%s %s%s m%d-%d' % (s['label'], s['key'],
                          '' if s.get('signature_in_score', '-') in ('-', s['key'])
                          else ' [score says %s]' % s['signature_in_score'],
                          s['start_measure'], s['end_measure'])
      for s in sections)))
  print('repeated passages: %s' % ' | '.join(
      'm%d-%d==m%d-%d' % (r['from_measure'], r['to_measure'],
                          r['repeat_measure'], r['repeat_end_measure'])
      for r in repetitions[:8]))
  print('questions: %d (tiers: %s)' % (
      len(questions),
      {t: sum(1 for x in questions if x['tier'] == t) for t in range(4)}))
  if not args.no_midi:
    base = os.path.splitext(out)[0]
    paths, qpm = write_reference_midis(seq, tm, declared_meters, base)
    print('reference midi (expressive): %s' % paths[0])
    print('reference midi (steady %g qpm): %s' % (qpm, paths[1]))

  print('\npiece "%s" is ready: %d bars, %d questions'
        % (slug, facts.get('num_measures', 0), len(questions)))
  print('  bank: %s' % out)
  print('\n  next:')
  print('    ./tutor/run.sh check --piece %s' % slug)
  print('    ./tutor/run.sh quiz  --piece %s' % slug)
  print('    ./tutor/run.sh voice --piece %s   (needs the ASR/TTS servers)' % slug)


if __name__ == '__main__':
  main()
