"""Which piece's question bank to load.

Rules, in order:

1. `--piece <slug>` (or `TUTOR_PIECE`) -- explicit, and an error if missing.
2. The most recently generated bank, whether it lives in the per-piece layout
   (`memorization/output/pieces/<slug>/`) or the older single-file location.
   "Newest wins" is the behaviour that matches intent: you just added a piece,
   so that is the one you want to drill.
3. Otherwise: report what exists and how to create one.

Banks are never overwritten by adding a piece, so several can coexist and the
previous one stays drillable by name.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
OUTPUT_DIR = os.path.join(REPO_ROOT, 'memorization', 'output')
PIECES_DIR = os.path.join(OUTPUT_DIR, 'pieces')
INDEX_PATH = os.path.join(PIECES_DIR, 'index.json')
LEGACY_FINGERPRINT = os.path.join(OUTPUT_DIR, 'fingerprint.json')

ANALYZE_HINT = ('.venv/bin/python memorization/analyze.py <score.musicxml>   '
                '# .xml and .mxl all work')


def _read_index() -> Dict[str, Dict]:
  if not os.path.exists(INDEX_PATH):
    return {}
  try:
    with open(INDEX_PATH) as fh:
      entries = json.load(fh)
  except (ValueError, OSError):
    return {}
  return {e.get('slug'): e for e in entries if isinstance(e, dict) and e.get('slug')}


def available() -> List[Dict]:
  """Every usable bank on disk, newest first, with index metadata when known."""
  index = _read_index()
  found: List[Dict] = []

  if os.path.isdir(PIECES_DIR):
    for name in sorted(os.listdir(PIECES_DIR)):
      path = os.path.join(PIECES_DIR, name, 'fingerprint.json')
      if not os.path.exists(path):
        continue
      meta = dict(index.get(name) or {})
      meta.setdefault('slug', name)
      meta['path'] = path
      meta['mtime'] = os.path.getmtime(path)
      found.append(meta)

  if os.path.exists(LEGACY_FINGERPRINT):
    meta = dict(index.get('_legacy') or {})
    meta.setdefault('slug', _slug_from_fingerprint(LEGACY_FINGERPRINT))
    meta['path'] = LEGACY_FINGERPRINT
    meta['mtime'] = os.path.getmtime(LEGACY_FINGERPRINT)
    meta.setdefault('legacy', True)
    found.append(meta)

  found.sort(key=lambda m: m.get('generated_at') or m.get('mtime') or 0,
             reverse=True)
  return [_fill_from_bank(entry) for entry in found]


def _fill_from_bank(entry: Dict) -> Dict:
  """Backfill bars/questions/key/title from the bank when the index lacks them.

  Banks written before the index existed (or copied in by hand) would otherwise
  show up as a bare slug in --list-pieces.
  """
  if all(entry.get(k) for k in ('bars', 'questions')):
    return entry
  try:
    with open(entry['path']) as fh:
      data = json.load(fh)
  except (ValueError, OSError, KeyError):
    return entry
  facts = data.get('global') or {}
  entry.setdefault('title', facts.get('title') or '')
  if not entry.get('bars'):
    entry['bars'] = facts.get('num_measures') or 0
  if not entry.get('questions'):
    entry['questions'] = len(data.get('questions') or [])
  if not entry.get('key'):
    entry['key'] = facts.get('key_inferred') or ''
  entry.setdefault('source_file', data.get('source_file') or '')
  return entry


def _slug_from_fingerprint(path: str) -> str:
  try:
    with open(path) as fh:
      data = json.load(fh)
    source = data.get('source_file') or ''
    base = os.path.basename(source)
    for suffix in ('.musicxml', '.mxl', '.xml', '.mid', '.midi'):
      if base.lower().endswith(suffix):
        base = base[:-len(suffix)]
        break
    return base or 'piece'
  except (ValueError, OSError):
    return 'piece'


def describe(entry: Dict) -> str:
  bits = [str(entry.get('slug'))]
  if entry.get('bars'):
    bits.append('%d bars' % entry['bars'])
  if entry.get('questions'):
    bits.append('%d questions' % entry['questions'])
  if entry.get('key'):
    bits.append(str(entry['key']))
  title = entry.get('title')
  # The title is worth showing when it says more than the slug does.
  if title and str(title).lower() not in str(entry.get('slug', '')).lower():
    bits.append('"%s"' % title)
  if entry.get('legacy'):
    bits.append('(legacy path)')
  return '  '.join(bits)


def resolve(cfg) -> Tuple[str, str, str]:
  """(fingerprint_path, slug, why) for the requested piece."""
  pieces = available()

  if cfg.piece:
    match = next((p for p in pieces if p.get('slug') == cfg.piece), None)
    if match is None:
      known = ', '.join(str(p.get('slug')) for p in pieces) or 'none'
      raise SystemExit('no question bank called "%s". Available: %s\n'
                       'Build one with: %s' % (cfg.piece, known, ANALYZE_HINT))
    return match['path'], str(match['slug']), 'requested with --piece'

  if pieces:
    newest = pieces[0]
    others = len(pieces) - 1
    why = 'most recent bank'
    if others:
      why += ' (%d others available: --list-pieces)' % others
    return newest['path'], str(newest['slug']), why

  raise SystemExit('no question bank found under %s\n'
                   'Build one with: %s' % (OUTPUT_DIR, ANALYZE_HINT))


def listing() -> List[str]:
  pieces = available()
  if not pieces:
    return ['(none yet) build one with: %s' % ANALYZE_HINT]
  return [describe(p) for p in pieces]


def load_meta(path: str) -> Optional[Dict]:
  try:
    with open(path) as fh:
      return json.load(fh)
  except (ValueError, OSError):
    return None
