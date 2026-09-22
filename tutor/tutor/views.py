"""Payloads for the Map and Progress views.

Both are built from data that already exists: the fingerprint for the map, the
session logs for progress. Keeping them here (rather than in the HTTP handler)
means they can be tested without a server, and the client gets one flat JSON
document per view instead of scraping several endpoints.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .progress import display_name, per_piece, summarise

MAX_MEASURES = 400          # a 400-row table is already a lot of scrolling


def unique_bars(measures: List[dict], repetitions: List[dict]) -> Dict:
  """Bars that appear in no verified repeat -- the material to actually learn."""
  covered = set()
  for rep in repetitions or []:
    covered.update(range(int(rep['from_measure']), int(rep['to_measure']) + 1))
    covered.update(range(int(rep['repeat_measure']),
                         int(rep['repeat_end_measure']) + 1))
  free = sorted(m['measure'] for m in measures if m['measure'] not in covered)
  spans: List[List[int]] = []
  for bar in free:
    if spans and bar == spans[-1][1] + 1:
      spans[-1][1] = bar
    else:
      spans.append([bar, bar])
  return {'count': len(free), 'total': len(measures), 'spans': spans}


def trust_notes(fingerprint: Dict) -> Dict:
  """What the analysis is and is not entitled to claim.

  These drive the honesty of the Map view: a bar grid derived from timing does
  not match the printed page, and sections guessed from pitch statistics are
  excluded from drilling -- the reader should see that, not have to infer it.
  """
  grid = fingerprint.get('grid_source', '')
  sections = fingerprint.get('section_basis', '')
  repeats = fingerprint.get('repeat_basis', '')
  notes: List[str] = []
  if grid != 'score-measures':
    notes.append('Bar numbers come from timing, not the score\'s measures, so '
                 'they may not match the printed page.')
  if sections != 'key-signatures':
    notes.append('Sections are inferred from pitch statistics, not key '
                 'signatures -- so section questions are excluded from '
                 'drilling.')
  if repeats and repeats != 'score-content':
    notes.append('Repeats are approximate rather than verified note-for-note.')
  return {'bar_grid': grid, 'sections': sections, 'repeats': repeats,
          'notes': notes,
          'verified': grid == 'score-measures' and sections == 'key-signatures'}


def build_map(facts, max_measures: int = MAX_MEASURES) -> Dict:
  raw = facts.raw
  global_facts = facts.global_facts
  measures = raw.get('measures') or []
  rows = [{
      'bar': m['measure'], 'chord': m.get('chord') or '',
      'top': m.get('melody') or '', 'notes': m.get('notes_count') or 0,
      'rhythm': m.get('rhythm') or '', 'contour': m.get('contour') or '',
      'velocity': m.get('avg_velocity') or 0,
      'pitches': m.get('pitches') or [],
  } for m in measures[:max_measures]]

  tiers: Dict[str, int] = {}
  by_type: Dict[str, int] = {}
  for question in raw.get('questions') or []:
    tiers[str(question.get('tier', 0))] = tiers.get(str(question.get('tier', 0)), 0) + 1
    by_type[question.get('type', '?')] = by_type.get(question.get('type', '?'), 0) + 1

  return {
      'piece': {
          'slug': facts.path.split('/pieces/')[-1].split('/')[0],
          'title': global_facts.get('title') or '',
          'composers': global_facts.get('composers') or [],
          'bars': global_facts.get('num_measures'),
          'meters': global_facts.get('time_signatures_declared') or [],
          'key': global_facts.get('key_inferred'),
          'key_from_score': global_facts.get('key_from_score'),
          'lowest': global_facts.get('lowest_note'),
          'highest': global_facts.get('highest_note'),
          'duration_s': global_facts.get('total_time_s'),
          'notes': global_facts.get('total_notes'),
          'climax': raw.get('climax_measure'),
      },
      'trust': trust_notes(raw),
      'sections': raw.get('sections') or [],
      'repetitions': raw.get('repetitions') or [],
      'unique': unique_bars(measures, raw.get('repetitions') or []),
      'measures': rows,
      'measures_truncated': len(measures) > max_measures,
      'questions': {'total': len(raw.get('questions') or []), 'tiers': tiers,
                    'by_type': by_type},
  }


def build_progress(facts, all_attempts: List, session_status: Dict,
                   weak_spots: List[str], log_names: Optional[List[str]] = None,
                   recent: int = 20, slug: Optional[str] = None) -> Dict:
  """History for the active piece, plus a repertoire overview across pieces.

  Attempts are matched on the score's basename, because that is what the log's
  `session_start` record stores; the reported slug is the app's own, so the view
  identifies a piece the same way the rest of the tutor does.
  """
  source = _piece_basename(getattr(facts, 'raw', {}).get('source_file', ''))
  mine = [a for a in all_attempts if not a.piece or a.piece == source]
  history = summarise(mine, recent=recent)
  return {
      'piece': {'slug': slug or display_name(source), 'source': source,
                'title': (facts.global_facts.get('title') or '')},
      'current_run': {'status': session_status, 'weak_spots': weak_spots},
      'history': history,
      'pieces': per_piece(all_attempts),
      'logs': log_names or [],
  }


def _piece_basename(source_file: str) -> str:
  import os
  return os.path.basename(source_file or '')
