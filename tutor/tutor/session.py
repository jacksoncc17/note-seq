"""Session state: attempts, mastery and an append-only turn log.

The log is the artefact a coach (human or LLM) reads later, so it records tool
calls, grounding verdicts and cancellations -- not just the transcript.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .core.facts import GatedQuestion


@dataclass
class Attempt:
  question_id: str
  type: str
  prompt: str
  expected: str
  given: str
  correct: bool
  source: str
  at: float = field(default_factory=time.time)


class Session:
  def __init__(self, log_path: Optional[str] = None, piece: str = ''):
    self.attempts: List[Attempt] = []
    self.log_path = log_path
    self.piece = piece
    self.started_at = time.time()
    if log_path:
      os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
      with open(log_path, 'a') as f:
        f.write(json.dumps({'event': 'session_start', 'piece': piece,
                            'at': self.started_at}) + '\n')

  # -- tool-facing API ----------------------------------------------------

  def record_attempt(self, question: GatedQuestion, given: str,
                     correct: bool) -> None:
    self.attempts.append(Attempt(
        question_id=question.id, type=question.type, prompt=question.prompt,
        expected=question.answer, given=given, correct=correct,
        source=question.source))
    self.log({'event': 'attempt', **asdict(self.attempts[-1])})

  def weak_spots(self, limit: int = 5) -> List[str]:
    """Question ids whose misses outnumber their hits."""
    tally: Dict[str, Dict[str, int]] = {}
    for a in self.attempts:
      t = tally.setdefault(a.question_id, {'correct': 0, 'wrong': 0,
                                           'prompt': a.prompt})
      t['correct' if a.correct else 'wrong'] += 1
    weak = [(k, v) for k, v in tally.items() if v['wrong'] > v['correct']]
    weak.sort(key=lambda kv: -kv[1]['wrong'])
    return [k for k, _ in weak[:limit]]

  def weak_spot_prompts(self, limit: int = 5) -> List[str]:
    by_id = {a.question_id: a.prompt for a in self.attempts}
    return [by_id.get(k, k) for k in self.weak_spots(limit)]

  def by_type(self) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for a in self.attempts:
      d = out.setdefault(a.type, {'correct': 0, 'wrong': 0})
      d['correct' if a.correct else 'wrong'] += 1
    return out

  def status(self) -> dict:
    correct = sum(1 for a in self.attempts if a.correct)
    return {
        'attempts': len(self.attempts),
        'correct': correct,
        'wrong': len(self.attempts) - correct,
        'accuracy': round(correct / len(self.attempts), 3) if self.attempts else 0.0,
        'by_type': self.by_type(),
        'duration_s': round(time.time() - self.started_at, 1),
    }

  # -- logging ------------------------------------------------------------

  def log(self, record: dict) -> None:
    if not self.log_path:
      return
    with open(self.log_path, 'a') as f:
      f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')

  def log_turn(self, trigger: str, user_text: str, tool_calls: List[dict],
               reply: str, grounding: Optional[dict] = None,
               spoken_fully: bool = True, latency_ms: float = 0.0,
               think_ms: Optional[float] = None,
               playback_ms: Optional[float] = None) -> None:
    """Record one turn.

    `think_ms` is the part that affects perceived responsiveness (tool calls +
    generation + verification, up to the moment speech starts); `playback_ms` is
    how long the reply took to say. Reporting their sum as "latency" once made a
    38-second monologue look like a 38-second think.
    """
    self.log({'event': 'turn', 'trigger': trigger, 'user': user_text,
              'tool_calls': tool_calls, 'reply': reply,
              'grounding': grounding, 'spoken_fully': spoken_fully,
              'latency_ms': round(latency_ms, 1),
              'think_ms': None if think_ms is None else round(think_ms, 1),
              'playback_ms': (None if playback_ms is None
                              else round(playback_ms, 1))})

  def summary(self) -> str:
    st = self.status()
    return ('%d attempts, %d correct, %d wrong (accuracy %.0f%%).'
            % (st['attempts'], st['correct'], st['wrong'],
               100 * st['accuracy']))
