"""Progress aggregation.

The logs are the durable record -- the in-memory session resets with the server
-- so what matters here is that attempts are attributed to the right piece and
that the summary says something a student can act on (worst type first, the
specific questions they keep missing).
"""

import json

from tutor.progress import (Attempt, log_files, measures_in, parse_attempts,
                            per_piece, read_attempts, summarise)

PIECE_A = '/scores/chopin-waltz-in-a-minor.mxl'
PIECE_B = '/scores/intermezzo-left.mxl'


def line(**record):
  return json.dumps(record)


def attempt(question_id, qtype, correct, prompt='What is the chord in measure 7?',
            expected='Am', given='Am', at=0.0):
  return line(event='attempt', question_id=question_id, type=qtype,
              prompt=prompt, expected=expected, given=given, correct=correct,
              at=at)


def sample_log(piece=PIECE_A):
  return [line(event='session_start', piece=piece, at=0.0),
          attempt('q1', 'chord', True, at=1.0),
          attempt('q1', 'chord', False, at=2.0),
          attempt('q2', 'rhythm', False, prompt='What is the rhythm of measure 9?',
                  expected='Q Q E E', given='Q Q Q Q', at=3.0),
          line(event='turn', trigger='answer_text', user='x', reply='y')]


# -- attribution ------------------------------------------------------------

def test_attempts_are_attributed_to_the_piece_in_force():
  attempts = parse_attempts(sample_log(PIECE_A))
  assert len(attempts) == 3
  assert {a.piece for a in attempts} == {'chopin-waltz-in-a-minor.mxl'}


def test_one_log_can_hold_several_pieces():
  lines = sample_log(PIECE_A) + sample_log(PIECE_B)
  both = parse_attempts(lines)
  assert len(both) == 6
  assert {a.piece for a in both} == {'chopin-waltz-in-a-minor.mxl',
                                     'intermezzo-left.mxl'}
  only_b = parse_attempts(lines, piece='intermezzo-left.mxl')
  assert len(only_b) == 3
  assert {a.piece for a in only_b} == {'intermezzo-left.mxl'}


def test_turn_records_are_ignored():
  attempts = parse_attempts(sample_log())
  assert all(a.type in ('chord', 'rhythm') for a in attempts)


def test_a_truncated_final_line_does_not_break_the_read():
  lines = sample_log() + ['{"event": "attempt", "questio']
  assert len(parse_attempts(lines)) == 3


def test_missing_fields_do_not_crash():
  lines = [line(event='session_start', piece=PIECE_A), line(event='attempt')]
  attempts = parse_attempts(lines)
  assert len(attempts) == 1 and attempts[0].correct is False


# -- files ------------------------------------------------------------------

def test_read_attempts_is_bounded_and_newest_first(tmp_path):
  for index in range(4):
    path = tmp_path / ('session%d.jsonl' % index)
    path.write_text('\n'.join(sample_log(PIECE_A)))
    import os
    os.utime(path, (1000 + index, 1000 + index))
  files = log_files(str(tmp_path), max_files=2)
  assert [f.split('/')[-1] for f in files] == ['session3.jsonl',
                                               'session2.jsonl']
  assert len(read_attempts(str(tmp_path), max_files=2)) == 6
  assert read_attempts(str(tmp_path / 'nope')) == []


# -- summary ----------------------------------------------------------------

def test_summary_totals_and_type_accuracy_worst_first():
  summary = summarise(parse_attempts(sample_log()))
  assert summary['totals'] == {'attempts': 3, 'correct': 1, 'wrong': 2,
                               'accuracy': 0.333}
  order = list(summary['by_type'])
  assert order[0] == 'rhythm'                 # 0% before 50%
  assert summary['by_type']['chord']['accuracy'] == 0.5


def test_weak_spots_are_questions_missed_more_than_hit():
  summary = summarise(parse_attempts(sample_log()))
  weak = summary['weak_spots']
  assert len(weak) == 1
  assert weak[0]['question_id'] == 'q2'
  assert weak[0]['wrong'] == 1 and weak[0]['correct'] == 0
  assert weak[0]['expected'] == 'Q Q E E'


def test_missed_measures_are_pulled_from_the_questions():
  summary = summarise(parse_attempts(sample_log()))
  assert summary['missed_measures'] == [9]


def test_measures_in_handles_the_phrasings_we_generate():
  assert measures_in('What is the chord in measure 25?') == [25]
  assert measures_in('The passage in measures 40-83 comes back later.') == [40, 83]
  assert measures_in('at bar 69') == [69]
  assert measures_in('no numbers here') == []


def test_recent_is_newest_first_and_bounded():
  summary = summarise(parse_attempts(sample_log()), recent=2)
  recent = summary['recent']
  assert len(recent) == 2
  assert recent[0]['type'] == 'rhythm'        # the latest attempt
  assert recent[1]['type'] == 'chord'


def test_empty_history_is_reported_not_crashed():
  summary = summarise([])
  assert summary['totals']['attempts'] == 0
  assert summary['by_type'] == {} and summary['weak_spots'] == []
  assert summary['recent'] == []


def test_per_piece_overview_counts_each_piece():
  attempts = parse_attempts(sample_log(PIECE_A) + sample_log(PIECE_B))
  overview = per_piece(attempts)
  # Displayed like the app refers to pieces: slug-like, without the extension.
  assert {p['piece'] for p in overview} == {'chopin-waltz-in-a-minor',
                                            'intermezzo-left'}
  assert {p['source'] for p in overview} == {'chopin-waltz-in-a-minor.mxl',
                                             'intermezzo-left.mxl'}
  assert all(p['attempts'] == 3 for p in overview)


def test_attempt_dataclass_defaults():
  entry = Attempt(piece='p', question_id='q', type='t', prompt='', expected='',
                  given='', correct=True)
  assert entry.at == 0.0
