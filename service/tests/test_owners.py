"""Who may see which piece.

The rule, in one line: **a piece belongs to the broker account whose job produced it, and
you see the pieces you own.** This file is the matrix around that line, because every case
in it is a way to get it wrong in a direction that matters -- showing somebody else's
recording, or hiding your own.

Two things changed when pieces gained an owner, and both are tested here rather than
described:

* `SERVICE_ALLOWED_SUBJECTS` stopped being an access list. It used to 403 anybody not on
  it; now it lists *operators*, who see everything including pieces whose owner is unknown.
  That matters because a broker's sign-up is often open, so "signed in" is not a small set.
* a piece with **no owner** is visible to operators only. Every piece published before this
  existed is in that state, so failing closed is what makes the change safe; the adoption
  path (`manage.py --adopt-unowned`) is how they get claimed.
"""

from __future__ import annotations

import time

import jwt
import pytest

from conftest import make_piece, package

SECRET = 'the-brokers-own-key'
OWNER = 'user-1'
OTHER = 'user-2'
OPERATOR = 'the-operator'


def token(subject: str, secret: str = SECRET, *, expires_in: float = 600) -> str:
  return jwt.encode({'sub': subject, 'exp': int(time.time()) + expires_in}, secret,
                    algorithm='HS256')


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
  """A data directory with four pieces: one owned by each of two users, and two unowned."""
  import json

  from service.app.config import Settings

  monkeypatch.setenv('SERVICE_DATA', str(tmp_path / 'data'))
  monkeypatch.setenv('SERVICE_EMBEDDED_WORKER', '0')
  monkeypatch.delenv('SERVICE_TRUST_JWT', raising=False)
  monkeypatch.delenv('SERVICE_OWNER', raising=False)
  path = tmp_path / 'data'
  settings = Settings(data_dir=path)
  settings.ensure_dirs()
  for slug, owner in (('mine', OWNER), ('theirs', OTHER), ('nobody-1', ''),
                      ('nobody-2', '')):
    directory = make_piece(settings.pieces_dir / slug, slug=slug, title=slug)
    if owner:
      meta_path = directory / 'meta.json'
      meta = json.loads(meta_path.read_text())
      meta['owner'] = owner
      meta_path.write_text(json.dumps(meta, indent=1))
  return path


def serving(path, monkeypatch, *, trust=SECRET, allowed=''):
  """A client for a deployment that shares the broker's login.

  The settings are built *after* the environment is set: the gate reads its secret and its
  operator list once, when `Settings` is constructed, so a client built on an older object
  would be ungated. Getting this wrong once is why the fixture hands out a path rather than
  a ready-made `Settings`.
  """
  from fastapi.testclient import TestClient

  from service.app.api import create_app
  from service.app.config import Settings

  if trust:
    monkeypatch.setenv('SERVICE_TRUST_JWT', trust)
  else:
    monkeypatch.delenv('SERVICE_TRUST_JWT', raising=False)
  monkeypatch.setenv('SERVICE_ALLOWED_SUBJECTS', allowed)
  return TestClient(create_app(Settings(data_dir=path)))


def slugs(client, who: str = OWNER) -> list:
  response = client.get('/api/v1/pieces', headers={'Authorization': 'Bearer %s' % token(who)})
  assert response.status_code == 200, response.text
  return sorted(row['slug'] for row in response.json()['pieces'])


# --------------------------------------------------------------------------- the matrix

def test_you_see_the_pieces_you_own(store_dir, monkeypatch):
  client = serving(store_dir, monkeypatch, allowed=OPERATOR)
  assert slugs(client, OWNER) == ['mine']
  assert slugs(client, OTHER) == ['theirs']


def test_an_operator_sees_everything_including_what_nobody_owns(store_dir, monkeypatch):
  client = serving(store_dir, monkeypatch, allowed='%s,%s' % (OPERATOR, OWNER))
  assert slugs(client, OPERATOR) == ['mine', 'nobody-1', 'nobody-2', 'theirs']


def test_an_account_with_no_pieces_gets_an_empty_list_not_a_refusal(store_dir, monkeypatch):
  """The change that matters: the allow-list no longer stands between a valid token and the
  service. Somebody who has never analysed anything sees nothing, which is the honest
  answer -- and it is what lets a closed deployment work without 403ing everybody."""
  client = serving(store_dir, monkeypatch, allowed=OPERATOR)
  response = client.get('/api/v1/pieces',
                        headers={'Authorization': 'Bearer %s' % token('a-stranger')})
  assert response.status_code == 200
  assert response.json()['pieces'] == []


def test_a_piece_you_do_not_own_is_absent_rather_than_forbidden(store_dir, monkeypatch):
  """404, not 403: whether a slug exists is not this service's business to disclose."""
  client = serving(store_dir, monkeypatch, allowed=OPERATOR)
  headers = {'Authorization': 'Bearer %s' % token(OWNER)}
  assert client.get('/api/v1/pieces/theirs', headers=headers).status_code == 404
  assert client.get('/api/v1/pieces/theirs/pages/1.svg', headers=headers).status_code == 404
  assert client.get('/api/v1/pieces/theirs/audio', headers=headers).status_code == 404
  assert client.get('/api/v1/pieces/theirs/audio?first_bar=1&last_bar=2',
                    headers=headers).status_code == 404
  # and your own piece is there, page, audio and clip alike
  assert client.get('/api/v1/pieces/mine', headers=headers).status_code == 200
  assert client.get('/api/v1/pieces/mine/pages/1.svg', headers=headers).status_code == 200
  assert client.get('/api/v1/pieces/mine/audio?first_bar=1&last_bar=2',
                    headers=headers).status_code == 200


def test_an_example_is_visible_to_every_signed_in_account(store_dir, monkeypatch):
  """The demo piece: one recording shown to everybody, without making it everybody's.

  `public` is a flag on the piece, set by `manage.py --public`, and it does not change who
  owns it -- the same piece still belongs to whoever recorded it. What it changes is the
  answer to "may this viewer read it", which is what a shared example needs.
  """
  from service.app.config import Settings
  from service.app.pieces import PieceStore

  client = serving(store_dir, monkeypatch, allowed=OPERATOR)
  pieces = PieceStore(Settings(data_dir=store_dir))

  assert pieces.set_public('mine') is True
  assert pieces.set_public('mine') is False              # idempotent, not a second write

  assert slugs(client, 'a-stranger') == ['mine']
  headers = {'Authorization': 'Bearer %s' % token('a-stranger')}
  assert client.get('/api/v1/pieces/mine', headers=headers).status_code == 200
  assert client.get('/api/v1/pieces/mine/pages/1.svg', headers=headers).status_code == 200
  assert client.get('/api/v1/pieces/mine/audio?first_bar=1&last_bar=2',
                    headers=headers).status_code == 200

  # and it is not visible to nobody: the token is still what gets you in
  assert client.get('/api/v1/pieces').status_code == 401
  assert client.get('/api/v1/pieces/mine').status_code == 401

  # taking the mark off puts it back behind its owner
  assert pieces.set_public('mine', False) is True
  assert slugs(client, 'a-stranger') == []
  assert client.get('/api/v1/pieces/mine', headers=headers).status_code == 404


def test_an_unowned_piece_is_hidden_from_everyone_but_an_operator(store_dir, monkeypatch):
  """Fail closed. Every piece published before ownership existed is unowned, so the
  alternative is leaving the whole existing store world-readable."""
  client = serving(store_dir, monkeypatch, allowed=OPERATOR)
  for who in (OWNER, OTHER, 'a-stranger'):
    assert slugs(client, who) == (['mine'] if who == OWNER else
                                  ['theirs'] if who == OTHER else [])
  headers = {'Authorization': 'Bearer %s' % token(OWNER)}
  assert client.get('/api/v1/pieces/nobody-1', headers=headers).status_code == 404


def test_loopback_with_no_gate_still_shows_everything(store_dir, monkeypatch):
  """The LAN service is one person on one machine, and nothing about ownership should make
  it ask them who they are. No gate means no filtering."""
  client = serving(store_dir, monkeypatch, trust='')
  assert slugs(client, 'anyone') == ['mine', 'nobody-1', 'nobody-2', 'theirs']


def test_a_token_signed_with_something_else_is_still_refused(store_dir, monkeypatch):
  client = serving(store_dir, monkeypatch, allowed=OPERATOR)
  response = client.get('/api/v1/pieces',
                        headers={'Authorization': 'Bearer %s' % token(OWNER, 'not-the-key')})
  assert response.status_code == 401


def test_status_says_whether_pieces_are_filtered(store_dir, monkeypatch):
  client = serving(store_dir, monkeypatch, allowed='%s,%s' % (OPERATOR, OWNER))
  auth = client.get('/api/v1/status').json()['auth']
  assert auth['mode'] == 'token' and auth['owner_filter'] is True and auth['operators'] == 2

  open_client = serving(store_dir, monkeypatch, trust='')
  assert open_client.get('/api/v1/status').json()['auth']['owner_filter'] is False


# --------------------------------------------------------------------------- adoption

def test_adoption_claims_the_pieces_that_predate_ownership(store_dir, monkeypatch, capsys):
  """What you run once on a store published before ownership existed -- on the machine that
  analyses, before re-sending, or on the page's own host."""
  import json

  from service.app.config import Settings
  from service.app.manage import main
  from service.app.pieces import PieceStore

  store = PieceStore(Settings(data_dir=store_dir))
  assert store.owner('nobody-1') == ''
  assert main(['--adopt-unowned', '--owner', OWNER]) == 0
  assert store.owner('nobody-1') == OWNER and store.owner('nobody-2') == OWNER
  assert store.owner('theirs') == OTHER                  # an owner is never overwritten
  printed = capsys.readouterr().out
  assert '2 piece(s) adopted' in printed

  # ... and now the page shows them to that account, and to nobody else
  client = serving(store_dir, monkeypatch, allowed=OPERATOR)
  assert slugs(client, OWNER) == ['mine', 'nobody-1', 'nobody-2']
  assert slugs(client, OTHER) == ['theirs']
  assert json.loads((store_dir / 'pieces' / 'nobody-1' / 'meta.json').read_text())['owner'] \
      == OWNER

  # running it again has nothing to do, and says so rather than pretending
  assert main(['--adopt-unowned', '--owner', OWNER]) == 0
  assert 'nothing was unowned' in capsys.readouterr().out


def test_adoption_refuses_without_saying_whose(store_dir, capsys):
  from service.app.config import Settings
  from service.app.manage import main
  from service.app.pieces import PieceStore

  assert main(['--adopt-unowned']) == 2
  assert PieceStore(Settings(data_dir=store_dir)).owner('nobody-1') == ''
  assert 'say who owns them' in capsys.readouterr().err


# --------------------------------------------------------------------------- uploads

def test_an_uploaded_piece_belongs_to_whoever_sent_it(store_dir, monkeypatch):
  """The owner travels inside the package: the worker states it, and the far side does not
  have to look anything up. The upload token is what makes that statement trustworthy."""
  import json

  from fastapi.testclient import TestClient

  from service.app.api import create_app

  from service.app.config import Settings

  monkeypatch.setenv('SERVICE_UPLOAD_TOKEN', 'the-shared-secret')
  monkeypatch.setenv('SERVICE_TRUST_JWT', SECRET)
  monkeypatch.setenv('SERVICE_ALLOWED_SUBJECTS', OPERATOR)
  client = TestClient(create_app(Settings(data_dir=store_dir)))

  source = make_piece(store_dir / 'incoming', slug='sent-by-a-worker',
                      title='sent by a worker')
  meta_path = source / 'meta.json'
  meta = json.loads(meta_path.read_text())
  meta['owner'] = OTHER
  meta_path.write_text(json.dumps(meta, indent=1))

  response = client.put('/api/v1/pieces/sent-by-a-worker', content=package(source),
                        headers={'X-Upload-Token': 'the-shared-secret'})
  assert response.status_code == 201, response.text
  assert response.json()['owner'] == OTHER

  assert 'sent-by-a-worker' in slugs(client, OTHER)
  assert 'sent-by-a-worker' not in slugs(client, OWNER)
  assert client.get('/api/v1/pieces/sent-by-a-worker',
                    headers={'Authorization': 'Bearer %s' % token(OWNER)}).status_code == 404


def fake_analysis(store_dir):
  """Only what `publish_piece` reads, so a test can stay about the thing it is testing."""
  clip = str(store_dir / 'pieces' / 'mine' / 'audio.wav')

  class FakeAnalysis:
    pages = []
    page_warnings = []
    score_meta = {'title': 'Analysed here'}
    audio = clip
    reference_midi = clip

    class result:                                       # noqa: N801 - it is a stand-in
      measures: list = []
      audio_duration_s = 1.0
      method = 'stand-in'
      seconds_per_measure = 1.0
      quality = {}
      warnings: list = []

      @staticmethod
      def to_dict():
        return {'measures': []}

  return FakeAnalysis()


def test_analysing_the_same_score_again_keeps_it_an_example(store_dir, monkeypatch):
  """The mark is about the piece, not about the analysis that produced it.

  Re-uploading the same score reuses the slug and replaces the piece -- that is what
  `claim_slug` is for -- so a demo that was re-analysed would otherwise drop off every
  other account's page for no reason anybody could see.
  """
  from service.app.config import Settings
  from service.app.pieces import PieceStore
  from service.app.pipeline import publish_piece

  settings = Settings(data_dir=store_dir)
  pieces = PieceStore(settings)
  score = store_dir / 'pieces' / 'mine' / 'audio.wav'

  # a title of its own: the fixture's 'mine' piece holds a different score under that name,
  # which is exactly the case `claim_slug` answers with '-2'
  slug, _ = publish_piece(settings, fake_analysis(store_dir), score=score,
                          title='Example waltz')
  assert slug == 'example-waltz'
  assert pieces.set_public(slug) is True

  slug_again, meta_again = publish_piece(settings, fake_analysis(store_dir), score=score,
                                         title='Example waltz')
  assert slug_again == slug
  assert meta_again.get('public') is True
  assert pieces.public(slug) is True


def test_a_piece_analysed_here_is_owned_by_whoever_this_machine_is(store_dir, monkeypatch):
  """`SERVICE_OWNER` is for a machine that analyses without a broker job to tell it whose
  work it is -- the LAN page, or `curl`."""
  from service.app.config import Settings
  from service.app.pipeline import publish_piece

  monkeypatch.setenv('SERVICE_OWNER', OPERATOR)
  settings = Settings(data_dir=store_dir)
  assert settings.owner == OPERATOR

  slug, meta = publish_piece(settings, fake_analysis(store_dir),
                             score=store_dir / 'pieces' / 'mine' / 'audio.wav',
                             title='Analysed here')
  assert meta['owner'] == OPERATOR
  assert (store_dir / 'pieces' / slug / 'meta.json').is_file()
  # visible to that account on the page, and not to the other one
  client = serving(store_dir, monkeypatch, allowed=OPERATOR)
  assert slug in slugs(client, OPERATOR)
  assert slug not in slugs(client, OTHER)
