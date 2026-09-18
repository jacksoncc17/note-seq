"""Which question bank gets loaded, and the per-piece layout.

The point of these tests: adding a piece must never silently replace the one you
were already learning, and `--piece` must fail loudly rather than quietly drill
the wrong music.
"""

import json
import os

import pytest

from tutor import pieces
from tutor.config import TutorConfig


@pytest.fixture
def store(tmp_path, monkeypatch):
  """An isolated pieces directory."""
  root = tmp_path / 'memorization' / 'output'
  pdir = root / 'pieces'
  pdir.mkdir(parents=True)
  monkeypatch.setattr(pieces, 'OUTPUT_DIR', str(root))
  monkeypatch.setattr(pieces, 'PIECES_DIR', str(pdir))
  monkeypatch.setattr(pieces, 'INDEX_PATH', str(pdir / 'index.json'))
  monkeypatch.setattr(pieces, 'LEGACY_FINGERPRINT', str(root / 'fingerprint.json'))
  return root, pdir


def write_bank(pdir, slug, bars=10, questions=5, generated_at=None):
  d = pdir / slug
  d.mkdir(parents=True, exist_ok=True)
  path = d / 'fingerprint.json'
  path.write_text(json.dumps({
      'source_file': '/scores/%s.mxl' % slug,
      'global': {'num_measures': bars, 'key_inferred': 'B minor'},
      'questions': [{'id': 'q%d' % i} for i in range(questions)],
  }))
  if generated_at:
    os.utime(str(path), (generated_at, generated_at))
  return path


def write_index(pdir, entries):
  (pdir / 'index.json').write_text(json.dumps(entries))


def cfg(**kw):
  c = TutorConfig()
  for k, v in kw.items():
    setattr(c, k, v)
  return c


# -- listing ----------------------------------------------------------------

def test_no_banks_is_reported_clearly(store):
  assert pieces.available() == []
  assert 'none yet' in pieces.listing()[0]
  with pytest.raises(SystemExit) as err:
    pieces.resolve(cfg())
  assert 'analyze.py' in str(err.value)      # tells you how to fix it


def test_available_lists_every_bank_newest_first(store):
  _, pdir = store
  write_bank(pdir, 'older', bars=20, generated_at=1000)
  write_bank(pdir, 'newer', bars=30, generated_at=2000)
  found = pieces.available()
  assert [p['slug'] for p in found] == ['newer', 'older']
  assert found[0]['path'].endswith('newer/fingerprint.json')


def test_index_metadata_is_used_when_present(store):
  _, pdir = store
  write_bank(pdir, 'sonata')
  write_index(pdir, [{'slug': 'sonata', 'bars': 88, 'questions': 400,
                      'title': 'Sonata in B minor', 'generated_at': 5000}])
  entry = pieces.available()[0]
  assert entry['bars'] == 88 and entry['questions'] == 400
  assert 'Sonata in B minor' in pieces.describe(entry)


def test_legacy_single_file_is_still_found(store):
  root, _ = store
  root.mkdir(parents=True, exist_ok=True)
  (root / 'fingerprint.json').write_text(json.dumps(
      {'source_file': '/scores/chopin-waltz-in-a-minor.mxl',
       'global': {'num_measures': 204}, 'questions': []}))
  found = pieces.available()
  assert len(found) == 1
  assert found[0]['slug'] == 'chopin-waltz-in-a-minor'   # derived from the source
  assert found[0]['legacy'] is True


# -- resolution -------------------------------------------------------------

def test_explicit_piece_wins(store):
  _, pdir = store
  write_bank(pdir, 'older', generated_at=1000)
  write_bank(pdir, 'newer', generated_at=2000)
  path, slug, why = pieces.resolve(cfg(piece='older'))
  assert slug == 'older' and path.endswith('older/fingerprint.json')
  assert 'requested' in why


def test_missing_piece_fails_loudly_and_lists_what_exists(store):
  _, pdir = store
  write_bank(pdir, 'sonata')
  with pytest.raises(SystemExit) as err:
    pieces.resolve(cfg(piece='nocturne'))
  message = str(err.value)
  assert 'nocturne' in message and 'sonata' in message


def test_default_picks_the_most_recent_bank(store):
  _, pdir = store
  write_bank(pdir, 'older', generated_at=1000)
  write_bank(pdir, 'newer', generated_at=2000)
  _, slug, why = pieces.resolve(cfg())
  assert slug == 'newer'
  assert 'others available' in why            # points at --list-pieces


def test_adding_a_piece_keeps_the_previous_one_drillable(store):
  _, pdir = store
  write_bank(pdir, 'first-piece', generated_at=1000)
  write_bank(pdir, 'second-piece', generated_at=2000)   # a new piece arrives
  assert pieces.resolve(cfg())[1] == 'second-piece'     # newest by default
  assert pieces.resolve(cfg(piece='first-piece'))[1] == 'first-piece'
  assert len(pieces.available()) == 2                   # nothing overwritten


def test_resolve_overwrites_cfg_so_downstream_uses_the_right_file(store):
  _, pdir = store
  path = write_bank(pdir, 'sonata')
  config = cfg()
  resolved, slug, _ = pieces.resolve(config)
  assert resolved == str(path)
