"""Turn-taking tests: this is why the mode machine is pure."""

from tutor.config import TutorConfig
from tutor.events import (BargeIn, MusicEnd, MusicStart, SpeechEnd, SpeechStart,
                          Tick)
from tutor.modes import (ConductorState, Decision, Floor, Mode, on_event,
                         wants_question_after_answer)

CFG = TutorConfig()


def fresh(mode=Mode.DRILL, **kw):
  return ConductorState(mode=mode, **kw)


def test_speech_barge_in_cancels_the_tutor():
  state = fresh(speaking=True, floor=Floor.TUTOR)
  state, decisions, _ = on_event(state, SpeechStart(at=1.0), CFG)
  assert Decision.CANCEL_TTS in decisions
  assert state.floor is Floor.STUDENT


def test_student_speech_produces_a_response():
  state = fresh()
  state, decisions, ctx = on_event(state, SpeechEnd(at=1.0, text='bar 53'), CFG)
  assert Decision.RESPOND_TEXT in decisions
  assert ctx['text'] == 'bar 53'


def test_empty_speech_is_not_a_turn():
  state = fresh()
  state, decisions, _ = on_event(state, SpeechEnd(at=1.0, text='   '), CFG)
  assert decisions == [Decision.STAY_SILENT]


def test_playing_over_the_tutor_ducks_speech():
  state = fresh(mode=Mode.TEACH, speaking=True)
  state, decisions, _ = on_event(state, MusicStart(at=1.0), CFG)
  assert Decision.CANCEL_TTS in decisions
  assert state.music_active is True


def test_listen_coach_waits_out_the_phrase_before_commenting():
  state = fresh(mode=Mode.LISTEN_COACH)
  state, decisions, _ = on_event(state, MusicStart(at=1.0), CFG)
  assert decisions == []
  state, decisions, _ = on_event(
      state, MusicEnd(at=2.0, pitch_events=[(60, 0.3), (64, 0.3)]), CFG)
  # Not yet: the coach delay has to elapse first.
  assert decisions == [Decision.STAY_SILENT]
  assert state.comment_due_at > 2.0
  due = state.comment_due_at
  state, decisions, _ = on_event(state, Tick(at=due - 0.01), CFG)
  assert decisions == [Decision.STAY_SILENT]
  state, decisions, _ = on_event(state, Tick(at=due + 0.01), CFG)
  assert decisions == [Decision.COMMENT_PLAYING]


def test_no_comment_while_still_playing():
  state = fresh(mode=Mode.LISTEN_COACH)
  state, _, _ = on_event(state, MusicStart(at=1.0), CFG)
  state, _, _ = on_event(state, MusicEnd(at=2.0, pitch_events=[]), CFG)
  due = state.comment_due_at
  state, _, _ = on_event(state, MusicStart(at=due - 0.05), CFG)  # starts again
  state, decisions, _ = on_event(state, Tick(at=due + 1.0), CFG)
  assert decisions == [Decision.STAY_SILENT]


def test_played_answer_counts_as_a_drill_answer():
  state = fresh(mode=Mode.DRILL, pending_question=True)
  state, decisions, ctx = on_event(
      state, MusicEnd(at=2.0, pitch_events=[(60, 0.3)]), CFG)
  assert decisions == [Decision.COMMENT_PLAYING]
  assert ctx['played_pitches'] == [60]


def test_playing_is_ignored_when_nothing_was_asked():
  state = fresh(mode=Mode.CONVERSE)
  state, decisions, _ = on_event(state, MusicEnd(at=2.0, pitch_events=[]), CFG)
  assert decisions == [Decision.STAY_SILENT]


def test_barge_in_event_cancels_speech():
  state = fresh(speaking=True)
  state, decisions, _ = on_event(state, BargeIn(at=1.0), CFG)
  assert Decision.CANCEL_TTS in decisions


def test_drill_asks_again_after_an_answer():
  assert wants_question_after_answer(
      ConductorState(mode=Mode.DRILL, pending_question=False)) is True
  assert wants_question_after_answer(
      ConductorState(mode=Mode.DRILL, pending_question=True)) is False
  assert wants_question_after_answer(
      ConductorState(mode=Mode.CONVERSE, pending_question=False)) is False
