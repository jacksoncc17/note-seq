"""Events the conductor reacts to.

This is the whole input alphabet: speech, instrument audio, playback, and
commands. Keeping it explicit is what makes the mode machine unit-testable
without any audio or model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def _now() -> float:
  return time.monotonic()


@dataclass
class Event:
  at: float = field(default_factory=_now)


@dataclass
class SpeechStart(Event):
  """The student began speaking."""


@dataclass
class SpeechEnd(Event):
  """The student stopped speaking; `text` is the transcript."""
  text: str = ''
  confidence: float = 1.0
  source: str = 'asr'          # 'asr' | 'typed'


@dataclass
class MusicStart(Event):
  """The student started playing."""


@dataclass
class NoteEvent(Event):
  pitch: int = 0
  velocity: int = 64


@dataclass
class MusicEnd(Event):
  """Instrument silence past the endpointing threshold: a phrase boundary."""
  pitch_events: list = field(default_factory=list)
  duration_s: float = 0.0


@dataclass
class BargeIn(Event):
  """The student interrupted the tutor (speech or playing over it)."""


@dataclass
class Tick(Event):
  """Periodic heartbeat so silence-based decisions can fire."""
  since_last_student_ms: float = 0.0
  since_music_end_ms: float = 0.0


@dataclass
class Shutdown(Event):
  """Stop the loop."""
