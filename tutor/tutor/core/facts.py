"""Ground truth: the fact model, with provenance and a quizzability gate.

This module exists because a tutor that asks questions has to be able to tell a
*fact* from a *guess*. Anything the analyser inferred with a heuristic (tonal
areas guessed from pitch statistics, an energy-based "climax") is not allowed
to become a quiz answer, and the tutor is told to hedge on anything merely
derived.

Provenance levels:
  score-exact        read from the score's own notation
  derived-validated  computed by a deterministic pass over the score (chords)
  heuristic          a guess; hint material only, never an answer
  performance        requires grading the student's actual playing
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SCORE_EXACT = 'score-exact'
DERIVED = 'derived-validated'
HEURISTIC = 'heuristic'
PERFORMANCE = 'performance'

QUIZZABLE = (SCORE_EXACT, DERIVED)

# Which artifact each question type depends on, and what makes it trustworthy.
_SECTION_TYPES = {'section_key', 'section_start', 'key_change', 'measure_key'}
_MEASURE_TYPES = {'chord', 'progression', 'chord_count'}
_PERFORMANCE_TYPES = {'play_from', 'play_section', 'play_ending'}
_GLOBAL_TYPES = {'count', 'key', 'meter', 'range'}
_SURFACE_TYPES = {'first_note', 'last_note', 'density', 'contour', 'note_count',
                  'top_note', 'rhythm'}
# Inherently judgement calls: never askable as a fact.
_SUBJECTIVE_TYPES = {'form', 'climax'}


@dataclass
class Fact:
  kind: str
  value: object
  source: str
  evidence: str = ''
  validated: bool = True

  @property
  def quizzable(self) -> bool:
    return self.validated and self.source in QUIZZABLE


@dataclass
class GatedQuestion:
  id: str
  tier: int
  type: str
  prompt: str
  answer: str
  options: List[str] = field(default_factory=list)
  hint: str = ''
  source: str = SCORE_EXACT

  def public(self) -> dict:
    """The question as the tutor may speak it -- never contains the answer."""
    out = {'question_id': self.id, 'tier': self.tier, 'prompt': self.prompt}
    if self.options:
      out['options'] = list(self.options)
    if self.hint:
      out['hint'] = self.hint
    return out


class FactStore:
  def __init__(self, path: str):
    with open(path) as f:
      self.raw = json.load(f)
    self.path = path
    self.global_facts: Dict[str, object] = self.raw.get('global', {})
    self.sections: List[dict] = self.raw.get('sections', [])
    self.repetitions: List[dict] = self.raw.get('repetitions', [])
    self.measures: Dict[int, dict] = {
        m['measure']: m for m in self.raw.get('measures', [])}
    self.climax = self.raw.get('climax_measure')
    self.section_basis = self.raw.get('section_basis', 'unknown')
    self.repeat_basis = self.raw.get('repeat_basis', 'unknown')
    self.grid_source = self.raw.get('grid_source', 'unknown')
    self.questions: List[GatedQuestion] = [
        self._gate(q) for q in self.raw.get('questions', [])]

  # -- provenance ---------------------------------------------------------

  def source_for(self, qtype: str) -> str:
    if qtype in _SUBJECTIVE_TYPES:
      return HEURISTIC
    if qtype in _SECTION_TYPES:
      # Only trustworthy when the boundaries came from the score's own key
      # signatures; a windowed tonal estimate must not be quizzed.
      return SCORE_EXACT if self.section_basis == 'key-signatures' else HEURISTIC
    if qtype == 'repeat':
      return (SCORE_EXACT if self.repeat_basis == 'score-content'
              else DERIVED)
    if qtype in _MEASURE_TYPES:
      return DERIVED
    if qtype in _PERFORMANCE_TYPES:
      return PERFORMANCE
    if qtype in _GLOBAL_TYPES or qtype in _SURFACE_TYPES:
      return SCORE_EXACT if self.grid_source == 'score-measures' else DERIVED
    return HEURISTIC

  def _gate(self, raw: dict) -> GatedQuestion:
    qtype = raw.get('type', '')
    source = self.source_for(qtype)
    validated = source in QUIZZABLE
    evidence = {
        SCORE_EXACT: 'read from the score',
        DERIVED: 'derived from the score notes',
        HEURISTIC: 'heuristic estimate - not askable',
        PERFORMANCE: 'needs performance grading',
    }[source]
    return GatedQuestion(id=raw.get('id', ''), tier=int(raw.get('tier', 0)),
                         type=qtype, prompt=raw.get('prompt', ''),
                         answer=str(raw.get('answer', '')),
                         options=list(raw.get('options') or []),
                         hint=raw.get('hint', ''), source=source)

  def quizzable(self, tiers: Optional[List[int]] = None,
                allow_performance: bool = False) -> List[GatedQuestion]:
    out = []
    for q in self.questions:
      if not (q.source in QUIZZABLE or
              (allow_performance and q.source == PERFORMANCE)):
        continue
      if tiers is not None and q.tier not in tiers:
        continue
      out.append(q)
    return out

  def provenance_report(self) -> dict:
    counts: Dict[str, int] = {}
    by_type: Dict[str, Dict[str, int]] = {}
    for q in self.questions:
      counts[q.source] = counts.get(q.source, 0) + 1
      by_type.setdefault(q.type, {}).setdefault(q.source, 0)
      by_type[q.type][q.source] += 1
    return {
        'fingerprint': self.path,
        'total_questions': len(self.questions),
        'by_source': counts,
        'quizzable': sum(1 for q in self.questions if q.source in QUIZZABLE),
        'excluded': sum(1 for q in self.questions if q.source not in QUIZZABLE),
        'by_type': by_type,
        'section_basis': self.section_basis,
        'repeat_basis': self.repeat_basis,
        'grid_source': self.grid_source,
    }

  # -- lookups ------------------------------------------------------------

  def bar(self, n: int) -> Optional[dict]:
    return self.measures.get(int(n))

  def section_for_bar(self, n: int) -> Optional[dict]:
    for s in self.sections:
      if s['start_measure'] <= n <= s['end_measure']:
        return s
    return None

  def spine(self) -> str:
    """One canonical, citable sentence describing the piece."""
    g = self.global_facts
    keys = ' -> '.join('%s m%d-%d' % (s['key'], s['start_measure'],
                                      s['end_measure']) for s in self.sections)
    reps = '; '.join('m%d-%d returns at m%d' % (
        r['from_measure'], r['to_measure'], r['repeat_measure'])
        for r in self.repetitions[:4])
    return ('%d bars in %s, key %s. Key areas: %s. Repeats: %s.'
            % (g.get('num_measures', 0),
               '/'.join(g.get('time_signatures_declared', ['4/4'])),
               g.get('key_inferred', '?'), keys or 'none found', reps or 'none'))

  def bar_line(self, n: int) -> str:
    m = self.bar(n)
    if not m:
      return 'Bar %s is outside this piece (it has %d bars).' % (
          n, self.global_facts.get('num_measures', 0))
    sec = self.section_for_bar(m['measure'])
    where = ' in the %s area (m%d-%d)' % (sec['key'], sec['start_measure'],
                                          sec['end_measure']) if sec else ''
    return ('Bar %d%s: chord %s, top note %s, %d notes, rhythm %s, contour %s.'
            % (m['measure'], where, m.get('chord') or '?', m.get('melody') or '-',
               m.get('notes_count', 0), m.get('rhythm') or '-',
               m.get('contour') or '-'))
