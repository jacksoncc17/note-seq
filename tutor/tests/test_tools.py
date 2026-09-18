"""Fact-gate, normalisation and grading tests.

The gate tests are the ones that encode the lesson of this project: a question
built on a heuristic must never reach the student.
"""

from tutor.config import TutorConfig
from tutor.core.facts import (DERIVED, HEURISTIC, PERFORMANCE, SCORE_EXACT,
                              FactStore)
from tutor.core.tools import ToolBox, answers_match, normalize_answer
from tutor.session import Session

CFG = TutorConfig()


def store():
  return FactStore(CFG.fingerprint)


def toolbox(facts=None, session=None, tier_max=None):
  facts = facts or store()
  session = session or Session(log_path=None)
  return ToolBox(facts, session, tier_max=CFG.tier_max if tier_max is None
                 else tier_max)


# -- the gate ---------------------------------------------------------------

def test_heuristic_facts_are_never_quizzable():
  facts = store()
  assert facts.source_for('climax') == HEURISTIC
  assert facts.source_for('form') == HEURISTIC
  ids = {q.type for q in facts.quizzable()}
  assert 'climax' not in ids and 'form' not in ids


def test_section_questions_follow_the_section_basis():
  facts = store()
  expected = (SCORE_EXACT if facts.section_basis == 'key-signatures'
              else HEURISTIC)
  assert facts.source_for('section_start') == expected
  assert facts.source_for('key_change') == expected


def test_chords_are_derived_not_exact():
  facts = store()
  assert facts.source_for('chord') == DERIVED
  assert facts.source_for('rhythm') == SCORE_EXACT


def test_performance_tasks_are_excluded_unless_enabled():
  facts = store()
  default_ids = {q.id for q in facts.quizzable()}
  with_perf = {q.id for q in facts.quizzable(allow_performance=True)}
  assert facts.source_for('play_from') == PERFORMANCE
  assert default_ids <= with_perf
  assert len(with_perf) > len(default_ids) or not any(
      q.type == 'play_from' for q in facts.questions)


def test_provenance_report_accounts_for_every_question():
  facts = store()
  report = facts.provenance_report()
  assert report['total_questions'] == len(facts.questions)
  assert report['quizzable'] + report['excluded'] == report['total_questions']


# -- answer normalisation ---------------------------------------------------

def test_measure_numbers_accept_loose_phrasings():
  for given in ('53', 'm53', 'M53', 'measure 53', 'bar 53', 'Bar 53.',
                'measure number 53', 'it is 53'):
    assert answers_match('53', given, 'section_start'), given
  assert not answers_match('53', '54', 'section_start')


def test_keys_ignore_case_and_allow_a_bare_tonic():
  assert answers_match('A minor', 'a minor', 'key')
  assert answers_match('A minor', 'A MINOR', 'key')
  assert answers_match('A minor', 'A', 'key')
  assert not answers_match('A minor', 'A major', 'key')


def test_contour_accepts_synonyms():
  assert answers_match('up', 'it rises', 'contour')
  assert answers_match('down', 'falls', 'contour')
  assert answers_match('level', 'stays the same', 'contour')
  assert not answers_match('up', 'down', 'contour')


def test_rhythm_ignores_spacing_and_separators():
  assert answers_match('Q Q E E', 'q-q-e-e', 'rhythm')
  assert normalize_answer('Q Q E E', 'rhythm') == normalize_answer(
      'Q  Q   E E', 'rhythm')
  assert not answers_match('Q Q E E', 'Q Q Q E', 'rhythm')


def test_chords_ignore_case_and_spacing():
  assert answers_match('Bm7b5', 'bm7b5', 'chord')
  assert answers_match('Bm7b5', 'B m7b5', 'chord')
  assert answers_match('Am', 'A minor', 'chord')


def test_notes_normalise_enharmonically():
  assert answers_match('A#4', 'Bb4', 'top_note')
  assert answers_match('E2', 'e2', 'range')


# -- grading and mastery ----------------------------------------------------

def test_grading_records_mastery_and_feeds_weak_spots():
  session = Session(log_path=None)
  tb = toolbox(session=session)
  q = tb.next_question(tier=1)
  assert q.ok
  first = tb.grade_answer('definitely not the answer')
  assert first.data['correct'] is False
  assert session.status()['wrong'] == 1
  assert tb.session.weak_spots() == [first.data['question_id']]

  tb.next_question(tier=1)
  right = tb.grade_answer(tb.pending.answer if tb.pending else '')
  assert right.ok


def test_the_same_question_is_not_re_asked_immediately():
  tb = toolbox()
  ids = []
  for _ in range(5):
    result = tb.next_question(tier=1)
    ids.append(result.data['question_id'])
  assert all(a != b for a, b in zip(ids, ids[1:])), ids


def test_grade_without_a_pending_question_is_refused():
  tb = toolbox()
  result = tb.grade_answer('53')
  assert result.ok is False


# -- playing comparison -----------------------------------------------------

def test_compare_played_finds_missing_pitch_classes():
  facts = store()
  tb = ToolBox(facts, Session(log_path=None), tier_max=CFG.tier_max)
  from tutor.cli import note_to_midi
  full = list(facts.bar(1)['pitches'])
  played = [note_to_midi(p) for p in full]
  ok = tb.compare_played([1], played)
  assert ok.data['missing'] == [] and ok.data['extra'] == []
  assert 'matches' in ok.summary

  # Drop one whole pitch class (every note of it), which is what the tool can
  # honestly detect; dropping a single doubling changes nothing.
  dropped = full[0]
  partial = [note_to_midi(p) for p in full
             if (note_to_midi(p) or 0) % 12 != (note_to_midi(dropped) or 0) % 12]
  bad = tb.compare_played([1], partial)
  assert bad.data['missing'], bad.data
  assert 'missing' in bad.summary


def test_compare_played_never_claims_timing():
  facts = store()
  tb = ToolBox(facts, Session(log_path=None), tier_max=CFG.tier_max)
  result = tb.compare_played([1], [60, 64])
  assert result.data['timing_assessed'] is False
  assert 'timing' in result.summary.lower()
