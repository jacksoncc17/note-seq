"""Post-generation claim verification.

Prompt instructions are not enforcement. Before anything is spoken, the draft
is scanned for factual claims -- bar numbers, key names, note names, chords --
and each one must appear in evidence a tool actually returned. This is the
mechanism that stops a confident "the A major section starts at bar 25" when the
score says bar 53.

Policies:
  block  drop the offending sentences (and optionally let the caller retry)
  hedge  keep them but append an explicit uncertainty marker
  warn   pass through, log only
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .core.facts import HEURISTIC, FactStore

BAR_RE = re.compile(r'\b(?:bars?|measures?|mm?\.?)\s*(\d{1,4})\b', re.I)
KEY_RE = re.compile(r'\b([A-Ga-g])([#b]?)\s+(major|minor)\b')
NOTE_RE = re.compile(r'\b([A-Ga-g])([#b]?)(-?\d)\b')
# Chord roots must be capitalised so that ordinary words ("I am") do not match.
CHORD_TOKEN_RE = re.compile(
    r'\b([A-G][#b]?)(m7b5|maj7|min7|dim7|dim|aug|sus4|sus2|add9|m7|m|7|\+)\b')
OPTIONS_RE = re.compile(r'^\s*options?\b', re.I)
NUMBER_RE = re.compile(r'\b(\d{1,4})\b')
SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')

HEDGE = 'I am not certain of that part, so please check it against the score.'


@dataclass
class Claim:
  kind: str
  value: str
  sentence_index: int


@dataclass
class GroundingReport:
  ok: bool
  claims: List[Claim] = field(default_factory=list)
  ungrounded: List[Claim] = field(default_factory=list)
  known_bars: Set[int] = field(default_factory=set)

  def as_dict(self) -> dict:
    return {'ok': self.ok,
            'checked': [{'kind': c.kind, 'value': c.value} for c in self.claims],
            'ungrounded': [{'kind': c.kind, 'value': c.value} for c in self.ungrounded]}


class Evidence:
  """Facts a tool has returned this session, in citable form."""

  def __init__(self, facts: Optional[FactStore] = None):
    self.bars: Set[int] = set()
    self.keys: Set[str] = set()
    self.notes: Set[str] = set()
    self.chords: Set[str] = set()
    self.numbers: Set[str] = set()
    self.texts: List[str] = []
    self.facts = facts

  def observe(self, result) -> None:
    """Ingest a ToolResult (duck-typed to avoid an import cycle)."""
    self.texts.append(getattr(result, 'summary', '') or '')
    data = getattr(result, 'data', {}) or {}
    summary = getattr(result, 'summary', '') or ''
    for m in BAR_RE.finditer(summary):
      self.bars.add(int(m.group(1)))
    for m in KEY_RE.finditer(summary):
      self.keys.add(_key_norm(m.group(1), m.group(2), m.group(3)))
    for m in NOTE_RE.finditer(summary):
      self.notes.add(_note_norm(m.group(1), m.group(2), m.group(3)))
    for m in NUMBER_RE.finditer(summary):
      self.numbers.add(m.group(1))
    _harvest(data, self)

  def cite(self, limit: int = 3) -> List[str]:
    return [t for t in self.texts[-limit:] if t]


def _key_norm(letter: str, accidental: str, mode: str) -> str:
  return ('%s%s %s' % (letter.upper(), accidental, mode.lower())).strip()


def _note_norm(letter: str, accidental: str, octave: str) -> str:
  flat = {'Db': 'C#', 'Eb': 'D#', 'Gb': 'F#', 'Ab': 'G#', 'Bb': 'A#'}
  name = letter.upper() + accidental
  name = flat.get(name, name)
  return name + octave


def _harvest(node, ev: Evidence) -> None:
  """Walk tool result data and collect citable values."""
  if isinstance(node, dict):
    for key, value in node.items():
      lk = str(key).lower()
      if isinstance(value, (int, float)) and not isinstance(value, bool):
        if lk in ('bar', 'measure', 'start', 'end', 'from', 'to', 'returns_at',
                  'start_measure', 'end_measure', 'from_bar', 'to_bar',
                  'repeat_measure', 'num_measures', 'bar_number'):
          ev.bars.add(int(value))
        ev.numbers.add(str(int(value)))
      elif isinstance(value, str):
        if lk in ('key', 'key_inferred', 'section_key'):
          for m in KEY_RE.finditer(value):
            ev.keys.add(_key_norm(m.group(1), m.group(2), m.group(3)))
          ev.keys.add(value)
        elif lk in ('chord', 'expected'):
          ev.chords.add(_chord_norm(value))
          for m in BAR_RE.finditer(value):
            ev.bars.add(int(m.group(1)))
          for m in NUMBER_RE.finditer(value):
            ev.numbers.add(m.group(1))
        elif lk in ('melody', 'first_note', 'last_note', 'top_note'):
          for m in NOTE_RE.finditer(value):
            ev.notes.add(_note_norm(m.group(1), m.group(2), m.group(3)))
        elif lk in ('bar_line', 'spine'):
          pass
        for m in BAR_RE.finditer(value):
          ev.bars.add(int(m.group(1)))
        for m in KEY_RE.finditer(value):
          ev.keys.add(_key_norm(m.group(1), m.group(2), m.group(3)))
        for m in NOTE_RE.finditer(value):
          ev.notes.add(_note_norm(m.group(1), m.group(2), m.group(3)))
        for m in NUMBER_RE.finditer(value):
          ev.numbers.add(m.group(1))
      else:
        _harvest(value, ev)
  elif isinstance(node, list):
    for item in node:
      _harvest(item, ev)


def _chord_norm(text: str) -> str:
  return text.strip().lower().replace(' ', '')


class GroundingVerifier:
  def __init__(self, evidence: Evidence, facts: Optional[FactStore] = None,
               policy: str = 'block'):
    self.evidence = evidence
    self.facts = facts
    self.policy = policy

  def check(self, text: str) -> GroundingReport:
    """Extract factual claims and test each against citable evidence.

    Two subtleties keep this from crying wolf:
      * a question is not an assertion -- "What is the chord in measure 25?" and
        its option list name candidate answers, so only structural references
        (bar numbers) are checked inside interrogatives;
      * chord tokens are matched before notes so that "E7" is read as a chord,
        not as the note E in octave 7.
    """
    sentences = SENTENCE_RE.split(text.strip()) if text.strip() else []
    claims: List[Claim] = []
    for idx, sentence in enumerate(sentences):
      is_question = sentence.strip().endswith('?')
      is_option_list = bool(OPTIONS_RE.match(sentence.strip()))
      chord_spans = [(m.start(), m.end()) for m in CHORD_TOKEN_RE.finditer(sentence)]
      if not (is_question or is_option_list):
        for m in CHORD_TOKEN_RE.finditer(sentence):
          claims.append(Claim('chord', _chord_norm(m.group(0)), idx))
      for m in BAR_RE.finditer(sentence):
        claims.append(Claim('bar', m.group(1), idx))
      if is_question or is_option_list:
        continue
      for m in KEY_RE.finditer(sentence):
        claims.append(Claim('key', _key_norm(m.group(1), m.group(2), m.group(3)), idx))
      for m in NOTE_RE.finditer(sentence):
        if any(start <= m.start() < end for start, end in chord_spans):
          continue
        claims.append(Claim('note', _note_norm(m.group(1), m.group(2), m.group(3)), idx))

    ungrounded = [c for c in claims if not self._is_grounded(c)]
    return GroundingReport(ok=not ungrounded, claims=claims,
                           ungrounded=ungrounded,
                           known_bars=set(self.evidence.bars))

  def _is_grounded(self, claim: Claim) -> bool:
    ev = self.evidence
    if claim.kind == 'bar':
      return int(claim.value) in ev.bars
    if claim.kind == 'key':
      return claim.value in ev.keys or claim.value.split(' ')[0] in ev.keys
    if claim.kind == 'note':
      return claim.value in ev.notes
    if claim.kind == 'chord':
      return claim.value in ev.chords
    return True

  def repair(self, text: str, report: GroundingReport) -> Optional[str]:
    """Return the text to speak, or None if it must be dropped entirely."""
    if report.ok:
      return text
    if self.policy == 'warn':
      return text
    if self.policy == 'hedge':
      return text + ' ' + HEDGE
    bad_sentences = {c.sentence_index for c in report.ungrounded}
    sentences = SENTENCE_RE.split(text.strip())
    kept = [s for i, s in enumerate(sentences) if i not in bad_sentences]
    if not kept:
      return None
    return ' '.join(kept)
