"""Eviction: removing what nobody opens, without ever removing the only copy.

Three things have to hold, and each gets tests rather than a paragraph:

* **the ledger measures access** -- coalesced, so a page load is not three writes, and honest
  about what it counts (sessions, not requests);
* **nothing leaves without a verified archive copy** -- same name, and the same *size* as a
  freshly packed package, so a stale or half-written archive entry cannot pass for this piece;
* **an evicted piece is still a piece** -- it lists, it opens, its timings survive, and one
  restore command puts the recording and the pages back.

The archive is a stand-in here (`FakeStore`, shared with `test_archive`), and the disk is
faked where the watermark is what is being tested -- the real policy is exercised by running
the command, which is what `--evict` without `--delete` does safely.
"""

from __future__ import annotations

import json
import time

import pytest

from conftest import make_piece, package

from service.app.access import AccessLog, usage_order
from service.app.config import Settings
from service.app.jobs import JobStore
from service.app.manage import main
from service.app.pieces import PieceStore
from service.app.uploads import pack_piece

from test_archive import ARCHIVE_ENV, FakeStore


@pytest.fixture
def stored(tmp_path, monkeypatch):
  """Three pieces, an in-memory archive holding all three, and the archive configured."""
  monkeypatch.setenv('SERVICE_DATA', str(tmp_path / 'data'))
  monkeypatch.setenv('SERVICE_EMBEDDED_WORKER', '0')
  monkeypatch.setenv('NOTE_SEQ_WORKER_ENV', '/nonexistent/worker.env')
  for key, value in ARCHIVE_ENV.items():
    monkeypatch.setenv(key, value)

  store = FakeStore()
  monkeypatch.setattr(Settings, 'archive_client', lambda self: store)

  settings = Settings(data_dir=tmp_path / 'data')
  settings.ensure_dirs()
  for index, slug in enumerate(('oldest', 'middle', 'newest')):
    make_piece(settings.pieces_dir / slug, slug=slug, title=slug)
    meta_path = settings.pieces_dir / slug / 'meta.json'
    meta = json.loads(meta_path.read_text())
    meta['created_at'] = 1_700_000_000 + index      # distinct, old
    meta_path.write_text(json.dumps(meta))
    store.objects['pieces/%s.zip' % slug] = pack_piece(settings.pieces_dir / slug)
  return {'settings': settings, 'store': store, 'pieces': PieceStore(settings),
          'store_db': JobStore(settings), 'tmp': tmp_path}


def _evict_args(tmp_path, *extra):
  return ['--evict', '--data', str(tmp_path / 'data'), *extra]


# --------------------------------------------------------------------------- the ledger

def test_a_read_moves_last_access_once_per_window(tmp_path):
  """Three reads of one piece in a minute are one access -- otherwise every page load is a
  write, to answer a question whose resolution is days."""
  settings = Settings(data_dir=tmp_path / 'data')
  settings.ensure_dirs()                     # the database lives under it
  log = AccessLog(JobStore(settings).database)

  base = time.time()
  log.record('a-piece', now=base)
  log.record('a-piece', now=base + 5)
  log.record('a-piece', now=base + 600)
  rows = log.last_access()
  assert rows['a-piece']['accesses'] == 3                 # counted
  assert rows['a-piece']['last_access'] == base            # but not moved

  log.record('a-piece', now=base + 7200)                   # past the window
  assert log.last_access()['a-piece']['last_access'] == base + 7200


def test_a_piece_with_no_record_is_dated_from_when_it_was_created(stored):
  """Pieces published before the ledger existed have no row; treating them as 'never read'
  would evict a piece published a minute ago, so they are dated from `created_at`."""
  log = AccessLog(stored['store_db'].database)
  rows = {row['slug']: row for row in usage_order(stored['pieces'], log)}
  assert rows['newest']['recorded'] is False
  assert rows['newest']['last_access'] == rows['newest']['created_at']


# --------------------------------------------------------------------------- the policy

def test_a_dry_run_removes_nothing(stored, capsys):
  assert main(_evict_args(stored['tmp'], '--max-used-percent', '0')) == 0
  printed = capsys.readouterr().out
  assert 'would evict' in printed
  for slug in ('oldest', 'middle', 'newest'):
    assert stored['pieces'].complete(slug), slug


def test_eviction_keeps_the_record_and_removes_the_rest(stored, capsys):
  assert main(_evict_args(stored['tmp'], '--max-used-percent', '0', '--older-than', '0',
                          '--delete')) == 0
  for slug in ('oldest', 'middle', 'newest'):
    piece = stored['pieces']
    assert piece.exists(slug), 'the record stays'
    assert piece.map(slug) and piece.geometry(slug), 'the timings stay'
    assert not piece.audio(slug).is_file(), 'the recording goes'
    assert not list((piece.directory(slug) / 'pages').glob('*.svg')), 'the pages go'
    assert piece.evicted(slug)['archive'] == 'pieces/%s.zip' % slug
    assert piece.complete(slug) is False


def test_the_watermark_holds_eviction_back(stored, monkeypatch, capsys):
  """A disk with room is not a reason to delete somebody's piece."""
  import shutil as _shutil

  class Room:
    total = 100 << 30
    used = 10 << 30
    free = 90 << 30
  monkeypatch.setattr(_shutil, 'disk_usage', lambda _path: Room)
  assert main(_evict_args(stored['tmp'], '--delete')) == 0
  assert 'below the watermark' in capsys.readouterr().out
  assert stored['pieces'].complete('oldest')


def test_nothing_is_evicted_without_a_verified_archive_copy(stored, capsys):
  """The precondition that matters: a piece whose archive write failed is the only copy."""
  del stored['store'].objects['pieces/oldest.zip']
  assert main(_evict_args(stored['tmp'], '--max-used-percent', '0', '--older-than', '0',
                          '--delete')) == 0
  printed = capsys.readouterr().out
  assert 'not in the archive' in printed
  assert stored['pieces'].complete('oldest'), 'the only copy is still here'


def test_an_archive_copy_of_different_bytes_does_not_count(stored, capsys):
  """Same name, different content: the archived package is not of this piece, so removing the
  local one would leave a different recording behind the same slug."""
  stored['store'].objects['pieces/oldest.zip'] = b'some other package'
  assert main(_evict_args(stored['tmp'], '--max-used-percent', '0', '--older-than', '0',
                          '--delete')) == 0
  assert 'archive copy differs' in capsys.readouterr().out
  assert stored['pieces'].complete('oldest')


def test_only_pieces_older_than_the_cutoff_are_evicted(stored):
  """`--older-than` is what makes this a policy rather than a purge."""
  now = time.time()
  log = AccessLog(stored['store_db'].database)
  log.record('middle', now=now)                      # just used
  for slug in ('oldest', 'newest'):
    log.record(slug, now=now - 40 * 86400)           # long ago
  assert main(_evict_args(stored['tmp'], '--max-used-percent', '0', '--older-than', '15',
                          '--delete')) == 0
  assert not stored['pieces'].complete('oldest')      # 40 days untouched
  assert not stored['pieces'].complete('newest')
  assert stored['pieces'].complete('middle')          # used a moment ago


def test_the_most_recently_used_can_be_kept_regardless(stored):
  now = time.time()
  log = AccessLog(stored['store_db'].database)
  for index, slug in enumerate(('oldest', 'middle', 'newest')):
    log.record(slug, now=now - (30 - index) * 86400)
  assert main(_evict_args(stored['tmp'], '--max-used-percent', '0', '--older-than', '0',
                          '--keep-used', '2', '--delete')) == 0
  assert not stored['pieces'].complete('oldest')
  assert stored['pieces'].complete('middle') and stored['pieces'].complete('newest')


def test_without_an_archive_it_refuses_to_evict_anything(stored, monkeypatch, capsys):
  for key in ARCHIVE_ENV:
    monkeypatch.delenv(key, raising=False)
  assert main(_evict_args(stored['tmp'], '--max-used-percent', '0', '--older-than', '0',
                          '--delete')) == 2
  assert 'nothing can be evicted safely' in capsys.readouterr().err
  assert stored['pieces'].complete('oldest')


# --------------------------------------------------------------------------- the round trip

def test_an_evicted_piece_still_lists_and_opens(stored, capsys):
  """What the record is for: the page can still name the piece and show its timings."""
  from fastapi.testclient import TestClient

  from service.app.api import create_app

  assert main(_evict_args(stored['tmp'], '--max-used-percent', '0', '--older-than', '0',
                          '--delete')) == 0
  client = TestClient(create_app(Settings(data_dir=stored['tmp'] / 'data')))

  listed = client.get('/api/v1/pieces').json()['pieces']
  assert sorted(row['slug'] for row in listed) == ['middle', 'newest', 'oldest']

  piece = client.get('/api/v1/pieces/oldest')
  assert piece.status_code == 200
  body = piece.json()
  assert body['evicted']['archive'] == 'pieces/oldest.zip'
  assert len(body['map']['measures']) == 8              # the timings are still here
  assert client.get('/api/v1/pieces/oldest/pages/1.svg').status_code == 404
  assert client.get('/api/v1/pieces/oldest/audio').status_code == 404


def test_reading_a_piece_records_the_access(stored):
  from fastapi.testclient import TestClient

  from service.app.api import create_app

  client = TestClient(create_app(Settings(data_dir=stored['tmp'] / 'data')))
  assert client.get('/api/v1/pieces/oldest').status_code == 200
  rows = AccessLog(JobStore(Settings(data_dir=stored['tmp'] / 'data')).database).last_access()
  assert 'oldest' in rows
  assert abs(rows['oldest']['last_access'] - time.time()) < 60


def test_restore_brings_an_evicted_piece_back(stored, capsys):
  """The whole loop: evict, then one command, then the page has its recording and pages."""
  from fastapi.testclient import TestClient

  from service.app.api import create_app

  assert main(_evict_args(stored['tmp'], '--max-used-percent', '0', '--older-than', '0',
                          '--delete')) == 0
  assert main(['--restore-from', 'note-seq', '--data', str(stored['tmp'] / 'data')]) == 0
  printed = capsys.readouterr().out
  assert '3 restored, 0 already here, 0 failed' in printed

  piece = stored['pieces']
  assert piece.complete('oldest')
  assert piece.evicted('oldest') is None                # the marker is gone: it is here now
  client = TestClient(create_app(Settings(data_dir=stored['tmp'] / 'data')))
  assert client.get('/api/v1/pieces/oldest/pages/1.svg').status_code == 200
  assert client.get('/api/v1/pieces/oldest/audio?first_bar=1&last_bar=2').status_code == 200
