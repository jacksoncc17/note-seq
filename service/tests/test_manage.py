"""Housekeeping: what `service.app.manage` is allowed to destroy, and what it is not.

The behaviour worth testing here is mostly about *refusing*: a dry run that changes
nothing, a running job that survives a `--jobs running --delete`, renders left alone while
a worker may be writing them. Each of those is a way to lose work that a summary cannot
undo, so each gets a test rather than a paragraph.

Everything runs against a hand-made data directory: jobs created through `JobStore` and
pieces through `conftest.make_piece`, so nothing here needs the analyzer or real music.
"""

from __future__ import annotations

import pathlib
import time

import pytest

from conftest import make_piece

from service.app.config import Settings
from service.app.jobs import JobStore
from service.app.manage import main
from service.app.pieces import PieceStore


@pytest.fixture
def house(tmp_path, monkeypatch):
  """A data directory with four jobs, three pieces, a queue and some debris."""
  monkeypatch.setenv('SERVICE_DATA', str(tmp_path / 'data'))
  monkeypatch.delenv('SERVICE_PUBLISH_URL', raising=False)
  settings = Settings()
  settings.ensure_dirs()
  store = JobStore(settings)

  jobs = {}

  def add(status, title, *, age_days=0.0, attempts=1):
    job = store.create({'score': 'x.musicxml'}, title=title)
    directory = settings.job_dir(job.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'upload.wav').write_bytes(b'x' * 2048)
    if status == 'done':
      store.claim('test')
      store.finish(job.id, store.get(job.id).token, 'some-slug')
    elif status == 'failed':
      store.claim('test')
      store.fail(job.id, store.get(job.id).token, 'it broke')
    elif status == 'running':
      store.claim('test')
    if age_days:
      _age(settings, job.id, age_days)
    jobs[title] = job.id
    return job.id

  jobs['old done'] = add('done', 'old done', age_days=30)
  jobs['new done'] = add('done', 'new done')
  jobs['failed'] = add('failed', 'a failure', age_days=10)
  jobs['running'] = add('running', 'in flight')

  for slug in ('keep-me', 'delete-me', 'also-delete'):
    make_piece(settings.pieces_dir / slug, slug=slug, title=slug)

  (settings.pieces_dir / 'half-built.building').mkdir()
  (settings.pieces_dir / 'half-built.building' / 'meta.json').write_text('{}')
  (settings.jobs_dir / 'orphan1234').mkdir()
  (settings.jobs_dir / 'orphan1234' / 'score.mxl').write_bytes(b'x' * 512)
  (settings.data_dir / 'output' / 'render').mkdir(parents=True)
  (settings.data_dir / 'output' / 'render' / 'take.wav').write_bytes(b'x' * 4096)
  settings.outbox_dir.mkdir(parents=True, exist_ok=True)
  (settings.outbox_dir / 'delete-me.json').write_text('{"slug": "delete-me", "url": "x"}')
  (settings.outbox_dir / 'delete-me.zip').write_bytes(b'zip')

  return {'settings': settings, 'store': store, 'pieces': PieceStore(settings),
          'jobs': jobs, 'tmp': tmp_path}


def _age(settings: Settings, job_id: str, days: float) -> None:
  """Backdate a job, so age-based cleanup can be tested without waiting a month."""
  when = time.time() - days * 86400
  with JobStore(settings).database.immediate() as connection:
    connection.execute('UPDATE jobs SET created_at = ?, finished_at = ? WHERE id = ?',
                       (when, when, job_id))


# --------------------------------------------------------------------------- summary

def test_the_summary_deletes_nothing(house, capsys):
  """A dry run is the default, and it is the same information the real run prints."""
  assert main([]) == 0
  printed = capsys.readouterr().out
  assert 'jobs' in printed and 'pieces' in printed and 'renders' in printed
  assert len(house['pieces'].slugs()) == 3
  assert len(house['store'].list(limit=None)) == 4


def test_a_half_built_piece_is_not_listed_as_a_piece(house):
  """`write` puts a real `meta.json` in `<slug>.building` before it renames the directory,

  so a listing that went by "has a meta.json" showed a half-written piece -- or two copies
  of the same one -- for as long as an analysis took. It went by that until this test.
  """
  assert 'half-built.building' not in house['pieces'].slugs()
  assert all('.building' not in slug for slug in house['pieces'].slugs())


def test_nothing_is_selected_by_default(house):
  settings = house['settings']
  assert main(['--jobs', 'done']) == 0                    # no --delete
  assert (settings.pieces_dir / 'delete-me' / 'meta.json').is_file()
  assert len(house['store'].list(limit=None)) == 4


# --------------------------------------------------------------------------- jobs

def test_a_status_and_an_age_select_jobs(house, capsys):
  settings, store, jobs = house['settings'], house['store'], house['jobs']
  assert main(['--jobs', 'done', '--older-than', '7', '--delete']) == 0

  assert store.get(jobs['old done']) is None
  assert not settings.job_dir(jobs['old done']).exists()      # its uploads went too
  assert store.get(jobs['new done']) is not None              # too recent
  assert store.get(jobs['failed']) is not None                # wrong status
  assert store.get(jobs['running']) is not None
  assert '1 removed' in capsys.readouterr().out


def test_keep_removes_all_but_the_newest(house):
  store, jobs = house['store'], house['jobs']
  assert main(['--jobs', 'all', '--keep', '1', '--delete']) == 0
  left = [job.id for job in store.list(limit=None)]
  assert len(left) == 1
  assert jobs['running'] in left                              # the newest, and oldest is gone


def test_a_running_job_is_refused_unless_forced(house, capsys):
  """Deleting the row of a running job leaves its worker uploading artifacts for a job
  that no longer exists -- and with `--force` that is a decision somebody makes."""
  settings, store, jobs = house['settings'], house['store'], house['jobs']
  assert main(['--jobs', 'running', '--delete']) == 0
  assert store.get(jobs['running']) is not None
  printed = capsys.readouterr().out
  assert 'skipped' in printed and 'use --force' in printed

  assert main(['--jobs', 'running', '--delete', '--force']) == 0
  assert store.get(jobs['running']) is None
  assert not settings.job_dir(jobs['running']).exists()


def test_an_unknown_status_is_a_usage_error(house, capsys):
  assert main(['--jobs', 'finished']) == 2
  assert 'unknown status' in capsys.readouterr().err


# --------------------------------------------------------------------------- pieces

def test_named_pieces_are_removed_and_the_rest_kept(house):
  settings, pieces = house['settings'], house['pieces']
  assert main(['--pieces', 'delete-me', '--delete']) == 0
  assert not (settings.pieces_dir / 'delete-me').exists()
  assert sorted(pieces.slugs()) == ['also-delete', 'keep-me']
  # the queue entry for it stays until --outbox is asked for: separate decisions
  assert (settings.outbox_dir / 'delete-me.json').is_file()


def test_keepers_keeps_the_newest(house):
  """Newest by `created_at` in meta.json, which is what the page lists by."""
  settings, pieces = house['settings'], house['pieces']
  order = [row['slug'] for row in pieces.listing()]
  assert main(['--keepers', '1', '--delete']) == 0
  assert pieces.slugs() == [order[0]]


def test_an_unknown_piece_is_a_usage_error(house, capsys):
  assert main(['--pieces', 'never-existed', '--delete']) == 2
  assert 'no such piece' in capsys.readouterr().err


def test_the_outbox_can_be_emptied_without_touching_the_piece(house, capsys):
  """A piece the page will never accept (a wrong token, a host that moved) is the case
  this is for: drop the queue entry, keep the work."""
  settings = house['settings']
  assert main(['--outbox', '--delete']) == 0
  assert list(settings.outbox_dir.glob('*')) == []
  assert (settings.pieces_dir / 'delete-me' / 'meta.json').is_file()
  assert 'delete-me' in capsys.readouterr().out


# --------------------------------------------------------------------------- scratch

def test_scratch_clears_debris_and_orphan_directories(house):
  settings, jobs = house['settings'], house['jobs']
  assert main(['--scratch', '--delete']) == 0
  assert not (settings.pieces_dir / 'half-built.building').exists()
  assert not (settings.jobs_dir / 'orphan1234').exists()
  # a job directory that *does* have a row is not debris
  assert settings.job_dir(jobs['new done']).is_dir()


def test_renders_wait_while_a_job_is_queued_or_running(house, capsys):
  """The analyzer writes there mid-analysis, so removing them under a running worker is
  how a job fails for a reason that makes no sense in its log."""
  settings = house['settings']
  assert main(['--renders', '--delete']) == 0
  assert (settings.data_dir / 'output' / 'render' / 'take.wav').is_file()
  assert 'skipped' in capsys.readouterr().out

  assert main(['--renders', '--delete', '--force']) == 0
  assert not (settings.data_dir / 'output' / 'render' / 'take.wav').exists()


def test_a_previous_copy_that_is_the_only_one_is_restored_not_deleted(tmp_path,
                                                                       monkeypatch):
  """The case a glob would destroy: a crash between the two renames of a replacement
  leaves the *old* piece in `<slug>.previous` and no target at all."""
  monkeypatch.setenv('SERVICE_DATA', str(tmp_path / 'data'))
  settings = Settings()
  settings.ensure_dirs()
  make_piece(settings.pieces_dir / 'good', slug='good')
  (settings.pieces_dir / 'good').rename(settings.pieces_dir / 'good.previous')
  (settings.pieces_dir / 'half-built.building').mkdir()

  assert main(['--scratch']) == 0                      # dry run first: nothing changes
  assert (settings.pieces_dir / 'good.previous').is_dir()

  assert main(['--scratch', '--delete']) == 0
  assert PieceStore(settings).slugs() == ['good']      # put back, not thrown away
  assert not (settings.pieces_dir / 'half-built.building').exists()
  assert not (settings.pieces_dir / 'good.previous').exists()
