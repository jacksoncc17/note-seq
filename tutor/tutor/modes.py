"""Mode state machine and floor control.

Deliberately pure: `on_event` takes state plus an event and returns decisions.
No audio, no model, no clock of its own -- which is why turn-taking can be
unit-tested. This is where "never talk over the student" actually lives.

Modes:
  TEACH         explain, demonstrate, answer questions
  DRILL         ask a question, wait for speech or playing, grade, advance
  LISTEN_COACH  stay quiet while the student plays, comment at phrase ends
  CONVERSE      free conversation
  REVIEW        go over the session log together
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Tuple

from .config import TutorConfig
from .events import (BargeIn, Event, MusicEnd, MusicStart, Shutdown, SpeechEnd,
                     SpeechStart, Tick)


class Mode(str, Enum):
  TEACH = 'teach'
  DRILL = 'drill'
  LISTEN_COACH = 'listen_coach'
  CONVERSE = 'converse'
  REVIEW = 'review'


class Floor(str, Enum):
  NONE = 'none'
  STUDENT = 'student'
  TUTOR = 'tutor'


class Decision(str, Enum):
  CANCEL_TTS = 'cancel_tts'
  RESPOND_TEXT = 'respond_text'
  ASK = 'ask'
  COMMENT_PLAYING = 'comment_playing'
  STAY_SILENT = 'stay_silent'


@dataclass
class ConductorState:
  mode: Mode = Mode.CONVERSE
  floor: Floor = Floor.NONE
  speaking: bool = False
  music_active: bool = False
  pending_question: bool = False
  last_student_at: float = 0.0
  music_started_at: float = 0.0
  music_ended_at: float = 0.0
  comment_due_at: float = 0.0
  played_pitches: List[int] = field(default_factory=list)


def on_event(state: ConductorState, event: Event,
             cfg: TutorConfig) -> Tuple[ConductorState, List[Decision], Dict]:
  """Advance the state machine. Returns (state, decisions, context)."""
  now = event.at
  decisions: List[Decision] = []
  context: Dict = {}

  if isinstance(event, SpeechStart):
    state.floor = Floor.STUDENT
    if state.speaking:
      # Barge-in: the student talking means the tutor stops mid-sentence.
      decisions.append(Decision.CANCEL_TTS)

  elif isinstance(event, SpeechEnd):
    state.last_student_at = now
    state.floor = Floor.STUDENT
    context['text'] = event.text
    if event.text.strip():
      decisions.append(Decision.RESPOND_TEXT)
    else:
      decisions.append(Decision.STAY_SILENT)

  elif isinstance(event, MusicStart):
    state.music_active = True
    state.music_started_at = now
    state.floor = Floor.STUDENT
    state.comment_due_at = 0.0
    if state.speaking and cfg.duck_tts_while_playing:
      decisions.append(Decision.CANCEL_TTS)

  elif isinstance(event, MusicEnd):
    state.music_active = False
    state.music_ended_at = now
    state.played_pitches = [p for p, _ in (event.pitch_events or [])
                            if isinstance(p, int)]
    context['played_pitches'] = list(state.played_pitches)
    if state.mode is Mode.DRILL and state.pending_question:
      # A played phrase is the answer.
      decisions.append(Decision.COMMENT_PLAYING)
    elif state.mode in (Mode.LISTEN_COACH, Mode.TEACH):
      # Wait out the coach delay so a fermata or a breath is not mistaken for
      # the end of the passage.
      state.comment_due_at = now + cfg.coach_delay_ms / 1000.0
      decisions.append(Decision.STAY_SILENT)
    else:
      decisions.append(Decision.STAY_SILENT)

  elif isinstance(event, BargeIn):
    state.floor = Floor.STUDENT
    if state.speaking:
      decisions.append(Decision.CANCEL_TTS)

  elif isinstance(event, Tick):
    if (state.comment_due_at and now >= state.comment_due_at
        and not state.music_active and not state.speaking):
      state.comment_due_at = 0.0
      decisions.append(Decision.COMMENT_PLAYING)
    else:
      decisions.append(Decision.STAY_SILENT)

  elif isinstance(event, Shutdown):
    decisions.append(Decision.STAY_SILENT)

  return state, decisions, context


def wants_question_after_answer(state: ConductorState) -> bool:
  """Drill mode keeps the loop going without being asked."""
  return state.mode is Mode.DRILL and not state.pending_question
