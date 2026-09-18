"""Where a piece's question bank lives.

One directory per piece, so starting a new piece never destroys the last one:

    memorization/output/pieces/<slug>/fingerprint.json
    memorization/output/pieces/<slug>/fingerprint.mid
    memorization/output/pieces/<slug>/fingerprint_steady.mid
    memorization/output/pieces/index.json      <- one line per piece

The slug is derived from the source filename, so this is enough to start a new
piece:

    .venv/bin/python memorization/analyze.py /path/to/new-piece.mxl
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, 'output')
PIECES_DIR = os.path.join(OUTPUT_DIR, 'pieces')
INDEX_PATH = os.path.join(PIECES_DIR, 'index.json')
# Written by earlier versions; still read so nothing breaks.
LEGACY_FINGERPRINT = os.path.join(OUTPUT_DIR, 'fingerprint.json')


def slugify(source_path: str) -> str:
  """A filesystem-friendly name from a score filename."""
  base = os.path.basename(source_path or '')
  base = re.sub(r'\.(musicxml|xml|mxl|mid|midi)$', '', base, flags=re.I)
  slug = re.sub(r'[^a-z0-9]+', '-', base.lower()).strip('-')
  return slug or 'piece'


def piece_dir(slug: str) -> str:
  return os.path.join(PIECES_DIR, slug)


def fingerprint_path(slug: str) -> str:
  return os.path.join(piece_dir(slug), 'fingerprint.json')


def _load_index() -> List[Dict]:
  if not os.path.exists(INDEX_PATH):
    return []
  try:
    with open(INDEX_PATH) as fh:
      data = json.load(fh)
    return data if isinstance(data, list) else []
  except (ValueError, OSError):
    return []


def record_piece(slug: str, meta: Dict) -> None:
  """Add or update this piece's entry in the index."""
  entries = [e for e in _load_index() if e.get('slug') != slug]
  entry = dict(meta)
  entry['slug'] = slug
  entry['generated_at'] = time.time()
  entry['dir'] = piece_dir(slug)
  entries.append(entry)
  entries.sort(key=lambda e: e.get('generated_at') or 0, reverse=True)
  os.makedirs(PIECES_DIR, exist_ok=True)
  with open(INDEX_PATH, 'w') as fh:
    json.dump(entries, fh, indent=2)


def list_pieces() -> List[Dict]:
  """Every piece with a bank on disk, newest first."""
  found: Dict[str, Dict] = {}
  for entry in _load_index():
    slug = entry.get('slug')
    if slug and os.path.exists(fingerprint_path(slug)):
      found[slug] = entry
  # Also pick up directories the index does not know about (hand-made, or from
  # a version that did not write an index).
  if os.path.isdir(PIECES_DIR):
    for name in sorted(os.listdir(PIECES_DIR)):
      path = fingerprint_path(name)
      if os.path.isdir(os.path.join(PIECES_DIR, name)) and os.path.exists(path):
        found.setdefault(name, {'slug': name,
                                'generated_at': os.path.getmtime(path),
                                'path': path})
  pieces = list(found.values())
  pieces.sort(key=lambda e: e.get('generated_at') or 0, reverse=True)
  return pieces


def latest() -> Optional[Dict]:
  pieces = list_pieces()
  return pieces[0] if pieces else None


def describe(entry: Dict) -> str:
  bits = ['%s' % entry.get('slug')]
  if entry.get('bars'):
    bits.append('%d bars' % entry['bars'])
  if entry.get('questions'):
    bits.append('%d questions' % entry['questions'])
  if entry.get('title'):
    bits.append(entry['title'])
  return '  '.join(bits)
