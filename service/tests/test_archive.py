"""The archive: a copy of every piece in an S3 bucket, and getting it back.

The point of it is that the machine which *serves* pieces becomes disposable — wipe its
volume, run one command, everything returns. So the tests are about two failure modes that
matter, not about S3 in general:

* **a broken archive must not cost a piece.** A store that is down, a wrong secret, a full
  disk: the analysis already succeeded, so the answer is a log line, not an exception.
* **a restore must be complete and idempotent.** Everything in the bucket comes back;
  running it twice does not re-download what is already there, and `--overwrite` does.

The four S3 verbs are exercised against a stand-in here and against a real store in the
opt-in test at the bottom (`SERVICE_ARCHIVE_URL=... pytest`), which is how the signature code
was validated — a fake cannot tell you that your SigV4 is wrong.
"""

from __future__ import annotations

import os

import pytest

from conftest import make_piece

from service.app.config import Settings

ARCHIVE_ENV = {
    'SERVICE_ARCHIVE_URL': 'http://archive.invalid',
    'SERVICE_ARCHIVE_BUCKET': 'note-seq',
    'SERVICE_ARCHIVE_KEY': 'GK-test',
    'SERVICE_ARCHIVE_SECRET': 'secret-test',
}


class FakeStore:
  """An in-memory bucket with the four verbs the archive uses."""

  def __init__(self):
    self.objects = {}
    self.fail_puts = None
    self.puts = 0

  def put(self, name, payload):
    self.puts += 1
    if self.fail_puts:
      raise self.fail_puts
    self.objects[name] = payload
    return {'name': name, 'bytes': len(payload), 'etag': 'etag-%d' % len(payload)}

  def get(self, name):
    from service.app.s3 import S3Error
    if name not in self.objects:
      raise S3Error('no such object: %s' % name, status=404, code='NoSuchKey')
    return self.objects[name]

  def head(self, name):
    from service.app.s3 import S3Error
    if name not in self.objects:
      raise S3Error('no such object', status=404)
    return {'name': name, 'bytes': len(self.objects[name]), 'etag': 'etag'}

  def list(self, prefix=''):
    return [{'name': key, 'bytes': len(value)}
            for key, value in sorted(self.objects.items()) if key.startswith(prefix)]

  def delete(self, name):
    self.objects.pop(name, None)


@pytest.fixture
def archived(tmp_path, monkeypatch):
  """A store with two pieces, an in-memory bucket, and the archive configured."""
  monkeypatch.setenv('SERVICE_DATA', str(tmp_path / 'data'))
  monkeypatch.setenv('SERVICE_EMBEDDED_WORKER', '0')
  for key, value in ARCHIVE_ENV.items():
    monkeypatch.setenv(key, value)
  store = FakeStore()
  monkeypatch.setattr(Settings, 'archive_client', lambda self: store)

  settings = Settings(data_dir=tmp_path / 'data')
  settings.ensure_dirs()
  for slug in ('one-piece', 'another-piece'):
    make_piece(settings.pieces_dir / slug, slug=slug, title=slug)
  return {'settings': settings, 'store': store, 'tmp': tmp_path}


# --------------------------------------------------------------------------- archiving

def test_a_finished_piece_is_archived_as_its_package(archived):
  from service.app import pipeline
  from service.app.uploads import unpack_piece

  settings = archived['settings']
  result = pipeline.archive_piece(settings, 'one-piece')
  assert result['archived'] is True
  assert result['name'] == 'pieces/one-piece.zip'

  payload = archived['store'].objects['pieces/one-piece.zip']
  unpacked = unpack_piece(payload, archived['tmp'] / 'unpacked', max_bytes=64 << 20)
  assert unpacked['pages'] == 2 and unpacked['title'] == 'one-piece'


def test_a_broken_archive_does_not_fail_the_piece(archived):
  """The rule that matters: the analysis is done, so an archive failure is a log line."""
  from service.app import pipeline
  from service.app.s3 import S3Error

  archived['store'].fail_puts = S3Error('connect refused', status=None)
  result = pipeline.archive_piece(archived['settings'], 'one-piece')
  assert result['configured'] is True and result['archived'] is False
  assert 'connect refused' in result['reason']
  # and the piece itself is untouched
  assert (archived['settings'].pieces_dir / 'one-piece' / 'meta.json').is_file()


def test_no_archive_url_means_no_archive(tmp_path, monkeypatch):
  from service.app import pipeline

  monkeypatch.setenv('SERVICE_DATA', str(tmp_path / 'data'))
  for key in ARCHIVE_ENV:
    monkeypatch.delenv(key, raising=False)
  settings = Settings(data_dir=tmp_path / 'data')
  settings.ensure_dirs()
  assert settings.archive_enabled is False
  assert pipeline.archive_piece(settings, 'anything') == {'configured': False}


def test_publishing_reports_what_the_archive_did(archived):
  """Both destinations, one package: the same bytes go to the page and to the bucket."""
  from service.app import pipeline

  settings = archived['settings']
  settings.publish_url = ''                       # archive only, no page to push to
  delivery = pipeline.publish_remote(settings, 'one-piece')
  assert delivery['archive']['archived'] is True
  assert delivery['archive']['name'] == 'pieces/one-piece.zip'
  assert delivery['sent'] is False


def test_the_archive_runs_before_the_push_is_attempted(archived, monkeypatch):
  """The bucket is the copy that survives losing the serving machine, so it goes first."""
  from service.app import pipeline

  settings = archived['settings']
  settings.publish_url = 'http://page.invalid/note-seq'
  order = []

  monkeypatch.setattr(pipeline, 'archive_piece',
                      lambda *a, **k: order.append('archive') or {'configured': True})
  from service.app import outbox
  monkeypatch.setattr(outbox, 'push', lambda *a, **k: order.append('push') or {'ok': True})
  monkeypatch.setattr(outbox, 'drain', lambda *a, **k: [])

  pipeline.publish_remote(settings, 'one-piece')
  assert order == ['archive', 'push']


def test_the_status_reports_the_bucket_but_never_the_secret(archived):
  described = archived['settings'].describe()
  assert described['archive'] == {'endpoint': 'http://archive.invalid',
                                  'bucket': 'note-seq'}
  assert 'secret-test' not in repr(described)


# --------------------------------------------------------------------------- restoring

def _configure_restore_env(monkeypatch, bucket='note-seq'):
  for key, value in ARCHIVE_ENV.items():
    monkeypatch.setenv(key, value)
  monkeypatch.setenv('SERVICE_ARCHIVE_BUCKET', bucket)


def test_archive_all_backfills_pieces_published_before_there_was_an_archive(archived, capsys):
  """The migration command: the archive is written at publish time, so everything older is
  missing from it. Reuses the same packer, and the same rule that one failure does not stop
  the rest."""
  from service.app.manage import main

  assert main(['--archive-all', '--data', str(archived['tmp'] / 'data')]) == 0
  assert sorted(archived['store'].objects) == ['pieces/another-piece.zip',
                                               'pieces/one-piece.zip']
  printed = capsys.readouterr().out
  assert '2 archived' in printed and '0 failed' in printed

  # idempotent: running it again re-uploads the same bytes (a PUT is a PUT) and still works
  assert main(['--archive-all', '--data', str(archived['tmp'] / 'data')]) == 0
  assert len(archived['store'].objects) == 2


def test_archive_all_refuses_to_guess_where_the_bucket_is(archived, monkeypatch, capsys):
  from service.app.manage import main

  for key in ARCHIVE_ENV:
    monkeypatch.delenv(key, raising=False)
  assert main(['--archive-all', '--data', str(archived['tmp'] / 'data')]) == 2
  assert 'the archive is not configured' in capsys.readouterr().err


def test_archive_all_survives_a_store_that_is_down(archived, capsys):
  from service.app.manage import main
  from service.app.s3 import S3Error

  archived['store'].fail_puts = S3Error('connect refused')
  assert main(['--archive-all', '--data', str(archived['tmp'] / 'data')]) == 1
  captured = capsys.readouterr()
  assert '0 archived' in captured.out and '2 failed' in captured.out
  assert 'connect refused' in captured.err


def test_restore_brings_back_everything_that_is_missing(archived, monkeypatch, capsys):
  """The disposable-serving-machine story, in one test: archive, wipe, restore, verify."""
  from service.app import pipeline
  from service.app.manage import main
  from service.app.pieces import PieceStore

  settings = archived['settings']
  settings.publish_url = ''

  # archive both pieces, then throw the store away
  for slug in ('one-piece', 'another-piece'):
    pipeline.archive_piece(settings, slug)
  for slug in ('one-piece', 'another-piece'):
    PieceStore(settings).delete(slug)
  assert PieceStore(settings).slugs() == []

  _configure_restore_env(monkeypatch)
  monkeypatch.setattr(Settings, 'archive_client', lambda self: archived['store'])
  assert main(['--restore-from', 'note-seq', '--data', str(archived['tmp'] / 'data')]) == 0

  back = PieceStore(Settings(data_dir=archived['tmp'] / 'data'))
  assert back.slugs() == ['another-piece', 'one-piece']
  assert (back.directory('one-piece') / 'audio.wav').is_file()
  assert len(list((back.directory('one-piece') / 'pages').glob('*.svg'))) == 2
  printed = capsys.readouterr().out
  assert '2 restored, 0 already here, 0 failed' in printed


def test_restore_skips_what_is_already_here_and_overwrites_only_on_request(archived,
                                                                          monkeypatch,
                                                                          capsys):
  from service.app import pipeline
  from service.app.manage import main

  settings = archived['settings']
  settings.publish_url = ''
  pipeline.archive_piece(settings, 'one-piece')

  _configure_restore_env(monkeypatch)
  monkeypatch.setattr(Settings, 'archive_client', lambda self: archived['store'])
  assert main(['--restore-from', 'note-seq', '--data', str(archived['tmp'] / 'data')]) == 0
  assert '0 restored, 1 already here' in capsys.readouterr().out
  assert archived['store'].puts == 1              # nothing was re-uploaded either

  assert main(['--restore-from', 'note-seq', '--overwrite',
               '--data', str(archived['tmp'] / 'data')]) == 0
  assert '1 restored, 0 already here' in capsys.readouterr().out


def test_restore_refuses_to_guess_where_the_archive_is(archived, monkeypatch, capsys):
  from service.app.manage import main

  for key in ARCHIVE_ENV:
    monkeypatch.delenv(key, raising=False)
  assert main(['--restore-from', 'note-seq',
               '--data', str(archived['tmp'] / 'data')]) == 2
  assert 'the archive is not configured' in capsys.readouterr().err


def test_only_piece_packages_are_considered(archived, monkeypatch, capsys):
  """A bucket may hold other things; a restore must not try to install them."""
  from service.app.manage import main
  from service.app.pieces import PieceStore

  # the fixture's own pieces would otherwise still be here, and the assertion below would be
  # about them rather than about what the restore did
  for slug in ('one-piece', 'another-piece'):
    PieceStore(archived['settings']).delete(slug)
  archived['store'].objects['backups/old.tar.gz'] = b'not a piece'
  archived['store'].objects['pieces/'] = b''
  archived['store'].objects['pieces/nested/deep.zip'] = b'nope'
  _configure_restore_env(monkeypatch)
  monkeypatch.setattr(Settings, 'archive_client', lambda self: archived['store'])

  assert main(['--restore-from', 'note-seq', '--data', str(archived['tmp'] / 'data')]) == 0
  assert PieceStore(Settings(data_dir=archived['tmp'] / 'data')).slugs() == []
  assert '0 piece(s)' in capsys.readouterr().out


# --------------------------------------------------------------------------- the names

def test_archive_names_round_trip_and_reject_anything_else():
  from service.app.s3 import archive_name, slug_of

  assert archive_name('serenade-7') == 'pieces/serenade-7.zip'
  assert slug_of('pieces/serenade-7.zip') == 'serenade-7'
  for foreign in ('backups/x.zip', 'pieces/x.tar', 'pieces//x.zip', 'pieces/a/b.zip', '',
                  'pieces/.zip'):
    assert slug_of(foreign) == '', foreign


# ----------------------------------------------------------------------- against a store

needs_archive = pytest.mark.skipif(
    not os.environ.get('SERVICE_ARCHIVE_URL'),
    reason='set SERVICE_ARCHIVE_URL (and _BUCKET/_KEY/_SECRET) to exercise a real store')


@needs_archive
def test_the_signing_works_against_a_real_store(tmp_path):
  """SigV4 cannot be validated by a fake -- this is the test that says the signature is right.

  Written after two of its own bugs: `dict(response.headers)` loses the case-insensitive
  lookup, so a `HEAD` of a real object reported zero bytes, and Garage answers
  `AccessDenied` (not AWS's `SignatureDoesNotMatch`) to a wrong secret.
  """
  import uuid

  from service.app.s3 import Client, S3Error

  settings = Settings()
  client = settings.archive_client()
  name = 'pieces/sigv4-check-%s.zip' % uuid.uuid4().hex[:8]
  try:
    assert client.put(name, b'signed round trip')['bytes'] == 17
    assert client.get(name) == b'signed round trip'
    assert client.head(name)['bytes'] == 17
    assert any(item['name'] == name for item in client.list('pieces/'))

    wrong = Client(settings.archive_url, settings.archive_bucket, settings.archive_key,
                   'definitely-not-the-secret', region=settings.archive_region)
    with pytest.raises(S3Error) as caught:
      wrong.put(name + '.x', b'x')
    assert caught.value.status == 403
  finally:
    client.delete(name)
  assert client.head(name) is None
