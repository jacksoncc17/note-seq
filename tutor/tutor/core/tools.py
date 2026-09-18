"""The deterministic tools the LLM is allowed to reason over.

Every tool returns a `ToolResult` carrying both structured `data` (for the
model) and a canonical `summary` sentence (for grounding: the tutor may only
assert bar numbers, keys and notes that appear in these summaries).

Answer grading lives here rather than in the prompt, because a tutor that
accepts "m53", "measure 53" and "53" as the same answer -- and records *how* an
answer was wrong -- is a property of code, not of wording.
"""

from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

from .facts import (DERIVED, GatedQuestion, FactStore, QUIZZABLE, SCORE_EXACT)

# --------------------------------------------------------------------------
# Answer normalisation
# --------------------------------------------------------------------------

_INT_RE = re.compile(r'-?\d+')
_RHYTHM_RE = re.compile(r'\b(St|Et|Ht|Qt|S\.|E\.|Q\.|H\.|W|H|Q|E|S)\b')
_NOTE_RE = re.compile(r'^([a-gA-G])([#b♯♭]?)(-?\d)?$')
_KEY_RE = re.compile(r'^([a-gA-G])([#b]?)\s*(minor|major|min|maj|m|M)?$')
_FILLER = re.compile(
    r'\b(the|a|an|is|it|at|in|on|around|about|measure|measures|bar|bars|m|'
    r'approximately|roughly|i|think|its|it\'s)\b')

_FLAT_TO_SHARP = {'Cb': 'B', 'Db': 'C#', 'Eb': 'D#', 'Fb': 'E', 'Gb': 'F#',
                  'Ab': 'G#', 'Bb': 'A#', 'C': 'C', 'D': 'D', 'E': 'E',
                  'F': 'F', 'G': 'G', 'A': 'A', 'B': 'B'}


def _canon_note(text: str) -> Optional[str]:
  m = _NOTE_RE.match(text.strip())
  if not m:
    return None
  letter, accidental, octave = m.group(1).upper(), m.group(2), m.group(3)
  if accidental in ('b', '♭'):
    letter = _FLAT_TO_SHARP.get(letter + 'b', letter)
    accidental = '#' if letter.endswith('#') else ''
    letter = letter[0]
  elif accidental in ('#', '♯'):
    accidental = '#'
  return letter + accidental + (octave or '')


def _canon_key(text: str) -> Optional[str]:
  # Lower-case before matching so "A MINOR", "a minor" and "A Minor" agree.
  m = _KEY_RE.match(text.strip().replace('.', '').lower())
  if not m:
    return None
  letter, accidental, mode = m.group(1).upper(), m.group(2), m.group(3)
  if accidental == 'b':
    sharp = _FLAT_TO_SHARP.get(letter + 'b', letter)
    letter = sharp[0]
    accidental = '#' if sharp.endswith('#') else ''
  mode = (mode or '').lower()
  if mode in ('m', 'min', 'minor'):
    mode = 'minor'
  elif mode in ('maj', 'major', ''):
    mode = 'major' if mode else ''
  return ('%s%s %s' % (letter, accidental, mode)).strip()


def _canon_chord(text: str) -> str:
  t = text.strip().lower().replace(' ', '')
  t = t.replace('ø', 'm7b5').replace('°', 'dim').replace('o7', 'dim7')
  t = t.replace('halfdiminished', 'm7b5').replace('half-diminished', 'm7b5')
  t = t.replace('minor', 'm').replace('major', 'maj')
  t = t.replace('♭', 'b').replace('♯', '#')
  return t


def _canon_contour(text: str) -> str:
  t = text.strip().lower()
  if any(w in t for w in ('up', 'rise', 'rises', 'ascend', 'higher', 'climb')):
    return 'up'
  if any(w in t for w in ('down', 'fall', 'falls', 'descend', 'lower', 'drop')):
    return 'down'
  if any(w in t for w in ('level', 'same', 'stay', 'static', 'flat', 'hold')):
    return 'level'
  return t


def _canon_rhythm(text: str) -> str:
  tokens = _RHYTHM_RE.findall(text.upper())
  return ' '.join(tokens)


def normalize_answer(text: str, qtype: str) -> str:
  """Canonical form of an answer, so equivalent phrasings compare equal."""
  if text is None:
    return ''
  raw = text.strip()
  if qtype in ('count', 'note_count', 'density', 'section_start', 'key_change',
               'measure_key', 'repeat', 'chord_count'):
    # Numbers may arrive as "53", "m53", "measure 53", "53 bars".
    n = _first_number(raw)
    return str(n) if n is not None else _FILLER.sub(' ', raw.lower()).strip()
  if qtype == 'range' or qtype in ('first_note', 'last_note', 'top_note'):
    note = _canon_note(raw)
    return note or raw.lower().strip()
  if qtype == 'key' or qtype == 'section_key':
    key = _canon_key(raw)
    return key or raw.lower().strip()
  if qtype in ('chord', 'progression', 'final_chord'):
    parts = re.split(r'[-–>,]| then | to ', raw)
    return '-'.join(_canon_chord(p) for p in parts if p.strip())
  if qtype == 'contour':
    return _canon_contour(raw)
  if qtype == 'rhythm':
    rhythm = _canon_rhythm(raw)
    return rhythm or raw.upper().strip()
  if qtype == 'meter':
    m = re.search(r'(\d+)\s*[/\- ]\s*(\d+)', raw)
    return '%s/%s' % (m.group(1), m.group(2)) if m else raw.strip()
  return _FILLER.sub(' ', raw.lower()).strip()


def _first_number(text: str) -> Optional[int]:
  m = _INT_RE.search(text.replace(',', ''))
  return int(m.group()) if m else None


def answers_match(expected: str, given: str, qtype: str) -> bool:
  e = normalize_answer(expected, qtype)
  g = normalize_answer(given, qtype)
  if not g:
    return False
  if e == g:
    return True
  # A key named without a mode ("A") still counts as the tonic.
  if qtype in ('key', 'section_key'):
    return e.split(' ')[0] == g.split(' ')[0] and (len(g.split(' ')) == 1)
  return False


# --------------------------------------------------------------------------
# Tool results
# --------------------------------------------------------------------------

@dataclass
class ToolResult:
  name: str
  ok: bool = True
  data: dict = field(default_factory=dict)
  summary: str = ''

  def as_text(self) -> str:
    """What goes back to the model as the tool result."""
    return json.dumps({'ok': self.ok, 'data': self.data, 'summary': self.summary},
                      ensure_ascii=False)


class SessionLike(Protocol):
  """The slice of session state the tools need (keeps coupling one-way)."""
  def record_attempt(self, question: GatedQuestion, given: str,
                     correct: bool) -> None: ...
  def status(self) -> dict: ...
  def weak_spots(self, limit: int = 5) -> List[str]: ...


class ToolBox:
  def __init__(self, facts: FactStore, session: SessionLike, tier_max: int = 1,
               allow_performance: bool = False, render_dir: str = 'tutor/output',
               rng: Optional[random.Random] = None):
    self.facts = facts
    self.session = session
    self.tier_max = tier_max
    self.allow_performance = allow_performance
    self.render_dir = render_dir
    self.rng = rng or random.Random(20260913)
    self.pending: Optional[GatedQuestion] = None
    self.asked: List[str] = []
    self.last_asked_id: Optional[str] = None
    self.unit: Optional[dict] = None

  # -- tools --------------------------------------------------------------

  def get_piece_map(self) -> ToolResult:
    f = self.facts
    sections = [{'label': s['label'], 'key': s['key'],
                 'start': s['start_measure'], 'end': s['end_measure'],
                 'bars': s['measures'], 'signature_in_score':
                 s.get('signature_in_score', '-')} for s in f.sections]
    reps = [{'from': r['from_measure'], 'to': r['to_measure'],
             'returns_at': r['repeat_measure'], 'bars': r['length'],
             'exact': bool(r.get('exact'))} for r in f.repetitions]
    return ToolResult('get_piece_map', data={
        'spine': f.spine(), 'sections': sections, 'repetitions': reps,
        'global': f.global_facts, 'provenance': f.provenance_report(),
    }, summary=f.spine())

  def get_bar(self, bar: int) -> ToolResult:
    m = self.facts.bar(bar)
    if not m:
      return ToolResult('get_bar', ok=False, data={'bar': bar},
                        summary='Bar %s does not exist in this piece.' % bar)
    return ToolResult('get_bar', data=m, summary=self.facts.bar_line(int(bar)))

  def next_question(self, tier: Optional[int] = None,
                    question_type: Optional[str] = None,
                    prefer_weak: bool = True) -> ToolResult:
    tiers = [tier] if tier is not None else list(range(0, self.tier_max + 1))
    pool = self.facts.quizzable(tiers=tiers,
                                allow_performance=self.allow_performance)
    if question_type:
      pool = [q for q in pool if q.type == question_type]
    if not pool:
      return ToolResult('next_question', ok=False, summary='No quizzable questions for that tier.',
                        data={'tiers': tiers})
    # Never re-ask the question we just asked: a miss should come back later,
    # not immediately, or the drill degenerates into a stuck loop.
    if self.last_asked_id and len(pool) > 3:
      pool = [q for q in pool if q.id != self.last_asked_id] or pool
    weak = set(self.session.weak_spots(limit=20))
    fresh = [q for q in pool if q.id not in self.asked and q.id not in weak]
    weak_pool = [q for q in pool if q.id in weak]
    choice = (self.rng.choice(weak_pool) if (prefer_weak and weak_pool) else
              self.rng.choice(fresh) if fresh else self.rng.choice(pool))
    self.pending = choice
    self.last_asked_id = choice.id
    if choice.id not in self.asked:
      self.asked.append(choice.id)
    self.unit = self.facts.section_for_bar(self._bar_of(choice))
    data = choice.public()
    data['source'] = choice.source
    return ToolResult('next_question', data=data,
                      summary='Question %s (tier %d): %s'
                              % (choice.id, choice.tier, choice.prompt))

  def grade_answer(self, answer: str,
                   question_id: Optional[str] = None) -> ToolResult:
    q = self.pending
    if q is None or (question_id and question_id != q.id):
      for cand in self.facts.questions:
        if cand.id == question_id:
          q = cand
          break
    if q is None:
      return ToolResult('grade_answer', ok=False,
                        summary='There is no pending question to grade.')
    correct = answers_match(q.answer, answer, q.type)
    self.session.record_attempt(q, answer, correct)
    explanation = self.explain(q, correct)
    self.pending = None
    return ToolResult('grade_answer', data={
        'question_id': q.id, 'correct': correct, 'expected': q.answer,
        'given': answer, 'type': q.type, 'source': q.source,
    }, summary=('Correct.' if correct else 'Not correct: expected %s, you said %s.'
                % (q.answer, answer)) + ' ' + explanation)

  def explain(self, q: GatedQuestion, correct: bool = True) -> str:
    """Explanation built only from score facts (never invented)."""
    f = self.facts
    t = q.type
    if t in ('section_start', 'key_change', 'section_key'):
      sec = next((s for s in f.sections if str(s['start_measure']) == q.answer
                  or s['key'] == q.answer), None)
      if sec:
        sig = sec.get('signature_in_score', '-')
        note = '' if sig in ('-', sec['key']) else (
            ' The printed signature there is %s, which in practice names %s.'
            % (sig, sec['key']))
        return ('%s runs from bar %d to bar %d (%d bars).%s'
                % (sec['key'], sec['start_measure'], sec['end_measure'],
                   sec['measures'], note))
    if t == 'repeat':
      n = _first_number(q.prompt)
      rep = next((r for r in f.repetitions if r['from_measure'] == n), None)
      if rep:
        return ('Bars %d-%d are identical to bars %d-%d (%d bars, checked bar '
                'by bar).' % (rep['from_measure'], rep['to_measure'],
                              rep['repeat_measure'], rep['repeat_end_measure'],
                              rep['length']))
    if t in ('chord', 'final_chord', 'progression'):
      n = _first_number(q.prompt)
      m = f.bar(n) if n else None
      if m:
        return ('Bar %d is %s in the per-bar chord inference, which is derived '
                'from the notes -- the score prints no chord symbols.'
                % (m['measure'], m.get('chord') or '?'))
    if t == 'measure_key':
      n = _first_number(q.prompt)
      sec = f.section_for_bar(n) if n else None
      if sec:
        return ('Bar %d falls in the %s stretch, bars %d-%d.'
                % (n, sec['key'], sec['start_measure'], sec['end_measure']))
    if t == 'count':
      return ('The piece is %d bars long as written.'
              % f.global_facts.get('num_measures', 0))
    if t == 'meter':
      return 'The score is in %s.' % '/'.join(
          f.global_facts.get('time_signatures_declared', ['4/4']))
    if t == 'key':
      return ('Duration-weighted pitch content points to %s; the printed '
              'signature is %s.' % (f.global_facts.get('key_inferred', '?'),
                                    f.global_facts.get('key_from_score', '?')))
    if t in ('contour', 'note_count', 'top_note', 'rhythm', 'first_note',
             'last_note'):
      n = _first_number(q.prompt)
      m = f.bar(n) if n else None
      if m:
        return f.bar_line(m['measure'])
    return 'The expected answer is %s.' % q.answer

  def compare_played(self, bars: List[int],
                     played_pitches: List[int]) -> ToolResult:
    """Compare what was played against the score for a bar range.

    What this can honestly say: which pitch classes are present in the score for
    those bars, which ones the student produced, and which are missing or extra.
    What it cannot say, and therefore never claims: anything about timing, and
    anything about note *counts* -- the fingerprint stores each bar's distinct
    pitches, so "you played 49 of 67 notes" would be comparing a distinct-pitch
    list against a doubling-inclusive note count. Real timing and count feedback
    needs the alignment pass plus a per-bar note list.
    """
    expected = set()
    for b in bars:
      m = self.facts.bar(int(b))
      if not m:
        continue
      for name in m.get('pitches', []):
        pc = _pitch_class(name)
        if pc:
          expected.add(pc)
    played = [_pitch_class(_pitch_name(int(p))) for p in played_pitches]
    played = [p for p in played if p]
    played_set = set(played)

    missing = sorted(expected - played_set)
    extra = sorted(played_set - expected)
    problems = []
    if missing:
      problems.append('missing %s' % ', '.join(missing))
    if extra:
      problems.append('extra %s' % ', '.join(extra))

    if not problems:
      summary = ('Pitch content matches %s: all %d pitch classes are there. '
                 'Timing is not assessed.' % (_bars_label(bars) if bars else
                                              'that passage', len(expected)))
    else:
      summary = ('In %s: %s. Pitch content only -- timing and note counts are '
                 'not assessed.' % (_bars_label(bars), '; '.join(problems)))
    return ToolResult('compare_played', data={
        'bars': list(bars), 'expected_pitch_classes': sorted(expected),
        'played_pitch_classes': sorted(played_set), 'missing': missing,
        'extra': extra, 'notes_played': len(played),
        'timing_assessed': False, 'counts_assessed': False}, summary=summary)

  def session_status(self) -> ToolResult:
    st = self.session.status()
    weak = self.session.weak_spots(limit=5)
    summary = ('%d attempts, %d correct, %d wrong. Weak spots: %s'
               % (st.get('attempts', 0), st.get('correct', 0),
                  st.get('wrong', 0), ', '.join(weak) or 'none yet'))
    return ToolResult('session_status', data={**st, 'weak_spots': weak},
                      summary=summary)

  def set_mode(self, mode: str) -> ToolResult:
    return ToolResult('set_mode', data={'mode': mode.lower().strip()},
                      summary='Mode switched to %s.' % mode.lower().strip())

  def practice_render(self, from_bar: int, to_bar: int,
                      qpm: Optional[float] = None) -> ToolResult:
    """Render a bar range to MIDI at a steady tempo, if the source is present."""
    src = self.facts.raw.get('source_file', '')
    if not src or not os.path.exists(src):
      return ToolResult('practice_render', ok=False, data={'source_file': src},
                        summary='The source score is not available, so nothing was rendered.')
    try:
      path = _render_practice_midi(src, int(from_bar), int(to_bar), qpm,
                                   self.render_dir, self.facts)
    except Exception as exc:  # noqa: BLE001 - report, never crash the lesson
      return ToolResult('practice_render', ok=False,
                        data={'error': str(exc)},
                        summary='Rendering failed: %s' % exc)
    return ToolResult('practice_render', data={'path': path, 'from_bar': from_bar,
                                               'to_bar': to_bar, 'qpm': qpm},
                      summary='Rendered bars %d-%d to %s.' % (from_bar, to_bar, path))

  # -- dispatch -----------------------------------------------------------

  def dispatch(self, name: str, args: Optional[dict] = None) -> ToolResult:
    args = args or {}
    handlers = {
        'get_piece_map': lambda: self.get_piece_map(),
        'get_bar': lambda: self.get_bar(int(args.get('bar', 0))),
        'next_question': lambda: self.next_question(
            tier=args.get('tier'), question_type=args.get('type')),
        'grade_answer': lambda: self.grade_answer(
            str(args.get('answer', '')), args.get('question_id')),
        'compare_played': lambda: self.compare_played(
            list(args.get('bars') or []), list(args.get('played_pitches') or [])),
        'session_status': lambda: self.session_status(),
        'set_mode': lambda: self.set_mode(str(args.get('mode', ''))),
        'practice_render': lambda: self.practice_render(
            int(args.get('from_bar', 1)), int(args.get('to_bar', 1)),
            args.get('qpm')),
    }
    handler = handlers.get(name)
    if handler is None:
      return ToolResult(name, ok=False, summary='Unknown tool %s.' % name)
    return handler()

  # -- helpers ------------------------------------------------------------

  def _bar_of(self, q: GatedQuestion) -> int:
    n = _first_number(q.prompt)
    return n if n is not None else 1


def _bars_label(bars: List[int]) -> str:
  if not bars:
    return '(none)'
  if len(bars) == 1:
    return 'bar %d' % bars[0]
  return 'bars %d-%d' % (min(bars), max(bars))


def _pitch_name(pitch: int) -> str:
  names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
  return '%s%d' % (names[pitch % 12], pitch // 12 - 1)


_FLAT_MAP = {'Cb': 'B', 'Db': 'C#', 'Eb': 'D#', 'Fb': 'E', 'Gb': 'F#',
             'Ab': 'G#', 'Bb': 'A#'}


def _pitch_class(name: str) -> Optional[str]:
  """'Bb5' -> 'A#'. Octave-insensitive, so octave slips are not wrong notes."""
  m = re.match(r'^([A-Ga-g])([#b]?)\d*$', (name or '').strip())
  if not m:
    return None
  base = m.group(1).upper() + m.group(2)
  return _FLAT_MAP.get(base, base)


def _render_practice_midi(src: str, from_bar: int, to_bar: int, qpm,
                          out_dir: str, facts) -> str:
  """Reuse the verified bar assignment from memorization/analyze.py."""
  import importlib.util
  here = os.path.dirname(os.path.abspath(__file__))
  analyze_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
      here))), 'memorization', 'analyze.py')
  spec = importlib.util.spec_from_file_location('memorize_analyze', analyze_path)
  if spec is None or spec.loader is None:
    raise RuntimeError('memorization/analyze.py not found next to the tutor')
  az = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(az)
  from note_seq import musicxml_reader

  seq = musicxml_reader.musicxml_file_to_sequence_proto(src)
  root = az.read_musicxml_root(src)
  measure_map = az.assign_notes_to_measures(seq, az.xml_measure_structure(root))
  chosen = [n for info in measure_map
            if from_bar <= info['measure'] <= to_bar for n in info['notes']]
  if not chosen:
    raise RuntimeError('no notes in bars %d-%d' % (from_bar, to_bar))
  tm = az.TempoMap(seq.tempos, seq.total_time)
  tempo = qpm or 100.0
  scale = 60.0 / tempo
  out = az.music_pb2.NoteSequence()
  t0 = min(n.start_time for n in chosen)
  for n in chosen:
    nn = out.notes.add()
    nn.pitch, nn.velocity = n.pitch, n.velocity
    nn.start_time = (tm.seconds_to_beat(n.start_time) -
                     tm.seconds_to_beat(t0)) * scale
    nn.end_time = (tm.seconds_to_beat(n.end_time) -
                   tm.seconds_to_beat(t0)) * scale
  out.total_time = max(n.end_time for n in out.notes)
  meters = facts.global_facts.get('time_signatures_declared') or ['3/4']
  num, den = meters[0].split('/')
  ts = out.time_signatures.add()
  ts.time, ts.numerator, ts.denominator = 0.0, int(num), int(den)
  tp = out.tempos.add()
  tp.time, tp.qpm = 0.0, tempo
  os.makedirs(out_dir, exist_ok=True)
  path = os.path.join(out_dir, 'practice_bars_%d_%d_%gqpm.mid'
                      % (from_bar, to_bar, tempo))
  az.midi_io.sequence_proto_to_midi_file(out, path)
  return path
