"""The Map and Progress payloads.

Both are built from data that already exists, so these tests are about the two
things that could quietly mislead: claiming more trust than the analysis earned,
and attributing history to the wrong piece.
"""

import json

import pytest

from tutor.progress import parse_attempts
from tutor.views import build_map, build_progress, trust_notes, unique_bars


class FakeFacts:
  """The slice of FactStore the views read."""

  def __init__(self, raw):
    self.raw = raw
    self.path = '/tmp/pieces/demo/fingerprint.json'

  @property
  def global_facts(self):
    return self.raw.get('global', {})


def bank(**over):
  raw = {
      'source_file': '/scores/demo.mxl',
      'grid_source': 'score-measures',
      'section_basis': 'key-signatures',
      'repeat_basis': 'score-content',
      'climax_measure': 12,
      'global': {'title': 'Demo', 'composers': ['Someone'], 'num_measures': 8,
                 'time_signatures_declared': ['3/4'], 'key_inferred': 'A minor',
                 'key_from_score': 'C major', 'lowest_note': 'E2',
                 'highest_note': 'D6', 'total_time_s': 12.0, 'total_notes': 40},
      'sections': [{'label': 'A', 'key': 'A minor', 'start_measure': 1,
                    'end_measure': 8, 'measures': 8,
                    'signature_in_score': 'C major'}],
      'repetitions': [],
      'measures': [{'measure': i, 'chord': 'Am', 'melody': 'A4', 'notes_count': 4,
                    'rhythm': 'Q Q E E', 'contour': 'up', 'avg_velocity': 64.0,
                    'pitches': ['A4']} for i in range(1, 9)],
      'questions': [{'id': 'q1', 'tier': 1, 'type': 'chord', 'prompt': 'p',
                     'answer': 'Am'}],
  }
  raw.update(over)
  return raw


# -- unique bars ------------------------------------------------------------

def test_unique_bars_without_repeats_is_everything():
  measures = bank()['measures']
  result = unique_bars(measures, [])
  assert result['count'] == 8 and result['total'] == 8
  assert result['spans'] == [[1, 8]]


def test_unique_bars_excludes_both_sides_of_a_repeat():
  measures = bank()['measures']
  repeats = [{'from_measure': 1, 'to_measure': 2,
              'repeat_measure': 5, 'repeat_end_measure': 6}]
  result = unique_bars(measures, repeats)
  assert result['count'] == 4
  assert result['spans'] == [[3, 4], [7, 8]]


# -- trust ------------------------------------------------------------------

def test_trust_notes_are_silent_when_everything_came_from_the_score():
  notes = trust_notes(bank())
  assert notes['verified'] is True
  assert notes['notes'] == []


def test_trust_notes_warn_about_a_heuristic_bar_grid_and_sections():
  notes = trust_notes(bank(grid_source='time-grid',
                           section_basis='tonal-areas (heuristic)'))
  assert notes['verified'] is False
  text = ' '.join(notes['notes'])
  assert 'printed page' in text          # bar numbers may not match
  assert 'excluded from drilling' in text
  assert len(notes['notes']) == 2


# -- map payload ------------------------------------------------------------

def test_map_payload_carries_the_facts_the_view_needs():
  data = build_map(FakeFacts(bank()))
  assert data['piece']['slug'] == 'demo'
  assert data['piece']['key'] == 'A minor'
  assert data['piece']['key_from_score'] == 'C major'   # a real disagreement
  assert data['piece']['bars'] == 8
  assert data['sections'][0]['label'] == 'A'
  assert len(data['measures']) == 8
  assert data['measures'][0]['bar'] == 1
  assert data['questions']['total'] == 1
  assert data['questions']['tiers'] == {'1': 1}
  assert data['measures_truncated'] is False
  assert data['trust']['verified'] is True


def test_map_payload_truncates_a_huge_table_but_says_so():
  measures = [{'measure': i, 'chord': 'Am', 'melody': 'A4', 'notes_count': 1,
               'rhythm': 'Q', 'contour': 'up', 'avg_velocity': 60.0}
              for i in range(1, 501)]
  data = build_map(FakeFacts(bank(measures=measures)), max_measures=100)
  assert len(data['measures']) == 100
  assert data['measures_truncated'] is True


# -- progress payload -------------------------------------------------------

def lines_for(piece, correct):
  return [
      json.dumps({'event': 'session_start', 'piece': piece, 'at': 0}),
      json.dumps({'event': 'attempt', 'question_id': 'q1', 'type': 'chord',
                  'prompt': 'What is the chord in measure 7?', 'expected': 'Am',
                  'given': 'Am', 'correct': correct, 'at': 1}),
  ]


def test_progress_separates_this_piece_from_the_rest():
  attempts = parse_attempts(lines_for('/scores/demo.mxl', False)
                            + lines_for('/scores/other.mxl', True))
  data = build_progress(FakeFacts(bank()), attempts,
                        {'attempts': 0}, [], ['session.jsonl'])
  # History is the active piece only...
  assert data['history']['totals']['attempts'] == 1
  assert data['history']['totals']['wrong'] == 1
  # ...while the repertoire table covers everything.
  assert {p['piece'] for p in data['pieces']} == {'demo', 'other'}
  assert data['piece']['slug'] == 'demo'          # slug-like, not a filename
  assert data['piece']['source'] == 'demo.mxl'    # what the log stores
  assert data['logs'] == ['session.jsonl']


def test_progress_reports_the_current_run_separately_from_history():
  data = build_progress(FakeFacts(bank()), [], {'attempts': 3, 'correct': 2},
                        ['What is the chord in measure 7?'])
  assert data['current_run']['status']['attempts'] == 3
  assert data['current_run']['weak_spots'] == ['What is the chord in measure 7?']
  assert data['history']['totals']['attempts'] == 0     # nothing logged yet
