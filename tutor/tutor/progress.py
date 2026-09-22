"""Progress history, aggregated from the session logs.

The in-memory session only knows about the current run -- it resets when the
server restarts -- so the append-only JSONL logs are the durable record of what
has been practised. Attempts are attributed to a piece by the `session_start`
record that precedes them in the same file, which is what makes per-piece
history correct when several pieces share one log.

Everything here is a pure function over text lines, so it is testable without a
server, a model or a piece.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

MEASURE_RE = re.compile(
    r'\b(?:bars?|measures?|mm?\.?)\s*(\d{1,4})(?:\s*[-–—]+\s*(\d{1,4}))?', re.I)


@dataclass
class Attempt:
  piece: str
  question_id: str
  type: str
  prompt: str
  expected: str
  given: str
  correct: bool
  at: float = 0.0


def log_files(log_dir: str, max_files: int = 8) -> List[str]:
  """Session logs, newest first (bounded: history should not be a slow read)."""
  if not os.path.isdir(log_dir):
    return []
  paths = [os.path.join(log_dir, name) for name in os.listdir(log_dir)
           if name.endswith('.jsonl')]
  paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
  return paths[:max_files]


def parse_attempts(lines: Iterable[str], piece: Optional[str] = None,
                   max_records: int = 50000) -> List[Attempt]:
  """Attempt records from JSONL text, attributed to the piece in force."""
  out: List[Attempt] = []
  current = ''
  for index, line in enumerate(lines):
    if index >= max_records:
      break
    line = line.strip()
    if not line:
      continue
    try:
      record = json.loads(line)
    except ValueError:
      continue                      # a truncated final line is not fatal
    kind = record.get('event')
    if kind == 'session_start':
      current = os.path.basename(str(record.get('piece') or ''))
      continue
    if kind != 'attempt':
      continue
    if piece and current and current != piece:
      continue
    out.append(Attempt(
        piece=current or piece or '',
        question_id=str(record.get('question_id') or ''),
        type=str(record.get('type') or ''),
        prompt=str(record.get('prompt') or ''),
        expected=str(record.get('expected') or ''),
        given=str(record.get('given') or ''),
        correct=bool(record.get('correct')),
        at=float(record.get('at') or 0.0)))
  return out


def read_attempts(log_dir: str, piece: Optional[str] = None,
                  max_files: int = 8, max_records: int = 50000) -> List[Attempt]:
  attempts: List[Attempt] = []
  for path in log_files(log_dir, max_files=max_files):
    try:
      with open(path) as handle:
        attempts.extend(parse_attempts(handle, piece=piece,
                                       max_records=max_records))
    except OSError:
      continue
  attempts.sort(key=lambda a: a.at)
  return attempts


def measures_in(text: str) -> List[int]:
  """Measure numbers mentioned in a question, including range endpoints.

  Repeat questions read "measures 40-83 comes back later", and both ends belong
  in the missed-bar highlight.
  """
  found: List[int] = []
  for start, end in MEASURE_RE.findall(text or ''):
    found.append(int(start))
    if end:
      found.append(int(end))
  return found


def summarise(attempts: List[Attempt], recent: int = 20) -> Dict:
  """Totals, accuracy by question type, weak questions, and recent history."""
  total = len(attempts)
  correct = sum(1 for a in attempts if a.correct)

  by_type: Dict[str, Dict[str, object]] = {}
  for attempt in attempts:
    bucket = by_type.setdefault(attempt.type or 'unknown',
                                {'attempts': 0, 'correct': 0, 'wrong': 0})
    bucket['attempts'] += 1
    bucket['correct' if attempt.correct else 'wrong'] += 1
  for bucket in by_type.values():
    bucket['accuracy'] = round(bucket['correct'] / max(bucket['attempts'], 1), 3)
  worst = sorted(by_type.items(),
                 key=lambda kv: (kv[1]['accuracy'], -kv[1]['attempts']))

  questions: Dict[str, Dict[str, object]] = {}
  for attempt in attempts:
    entry = questions.setdefault(attempt.question_id or attempt.prompt, {
        'question_id': attempt.question_id, 'prompt': attempt.prompt,
        'expected': attempt.expected, 'type': attempt.type,
        'correct': 0, 'wrong': 0})
    entry['correct' if attempt.correct else 'wrong'] += 1
    if not attempt.correct:
      entry['expected'] = attempt.expected or entry['expected']
  weak = [e for e in questions.values() if e['wrong'] > e['correct']]
  weak.sort(key=lambda e: -e['wrong'])

  missed: List[int] = []
  for entry in weak:
    for measure in measures_in(str(entry.get('prompt') or '')):
      if measure not in missed:
        missed.append(measure)
  missed.sort()

  return {
      'totals': {'attempts': total, 'correct': correct,
                 'wrong': total - correct,
                 'accuracy': round(correct / total, 3) if total else 0.0},
      'by_type': {k: v for k, v in worst},
      'weak_spots': weak[:10],
      'missed_measures': missed[:200],
      'recent': [{'at': a.at, 'prompt': a.prompt, 'given': a.given,
                  'expected': a.expected, 'correct': a.correct,
                  'type': a.type}
                 for a in attempts[-recent:]][::-1],
  }


def per_piece(attempts: List[Attempt]) -> List[Dict]:
  """Accuracy per piece, most practised first -- the repertoire overview."""
  buckets: Dict[str, Dict[str, object]] = {}
  for attempt in attempts:
    piece = attempt.piece or '(unknown)'
    entry = buckets.setdefault(piece, {'piece': display_name(piece),
                                       'source': piece, 'attempts': 0,
                                       'correct': 0})
    entry['attempts'] += 1
    entry['correct'] += 1 if attempt.correct else 0
  out = list(buckets.values())
  for entry in out:
    entry['accuracy'] = round(entry['correct'] / max(entry['attempts'], 1), 3)
  out.sort(key=lambda e: -e['attempts'])
  return out


SCORE_SUFFIXES = ('.musicxml', '.mxl', '.xml', '.mid', '.midi')
_SLUG_STRIP = re.compile(r'[^a-z0-9]+')


def display_name(source: str) -> str:
  """'Intermezzo_left.mxl' -> 'intermezzo-left'.

  The log stores a filename while the app talks in slugs (`analyze.py` builds
  the slug the same way), so deriving the display name here keeps the progress
  table, the piece selector and --list-pieces agreeing on what a piece is
  called. Without it the same piece appeared as both "Intermezzo_left" and
  "intermezzo-left".
  """
  name = os.path.basename(source or '')
  lowered = name.lower()
  for suffix in SCORE_SUFFIXES:
    if lowered.endswith(suffix):
      name = name[:-len(suffix)]
      break
  return _SLUG_STRIP.sub('-', name.lower()).strip('-') or name
