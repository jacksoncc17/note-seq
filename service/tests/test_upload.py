"""Getting a finished piece to the page that serves it.

Two halves, and they are tested apart because they fail apart:

* **the endpoint** -- `PUT /api/v1/pieces/{slug}`, what it accepts, what it refuses, and
  who is allowed to call it. Built here from hand-made pieces: no analysis, no analyzer,
  so these tests run in a second on any machine, which is the same property the serving
  deployment has.
* **the outbox** -- what happens when the page is not there. A fake opener covers the
  failures a real server cannot be asked to produce on cue, and one test goes over real
  HTTP to a socket, because "it works against a mock" is not the claim being made.

The one test that ties them together is `test_serving_an_uploaded_piece_needs_no_analyzer`:
it blocks every import of the analyzer and then uploads a piece and serves it, which is
the whole point of the format -- the machine that analysed the music needs the analyzer,
and the machine that shows it must not.
"""

from __future__ import annotations

import http.server
import json
import pathlib
import threading
import urllib.error
import zipfile

import pytest

TOKEN = 'a-shared-secret'
OTHER_TOKEN = 'the-other-workers-secret'


# --------------------------------------------------------------------------- a piece

def make_piece(directory: pathlib.Path, slug: str = 'hand-made', *, pages: int = 2,
               title: str = 'A hand-made piece', bars: int = 8,
               seconds_per_bar: float = 1.5) -> pathlib.Path:
  """A servable piece, written by hand: the five things the page reads, nothing else."""
  import numpy as np
  import soundfile as sf

  directory.mkdir(parents=True, exist_ok=True)
  (directory / 'pages').mkdir(exist_ok=True)
  duration = bars * seconds_per_bar
  sf.write(str(directory / 'audio.wav'), np.zeros(int(22050 * duration), dtype='float32'),
           22050)
  measures = [{'measure': bar, 'play_index': bar - 1,
               'start_s': round((bar - 1) * seconds_per_bar, 3),
               'end_s': round(bar * seconds_per_bar, 3), 'confidence': 1.0}
              for bar in range(1, bars + 1)]
  (directory / 'map.json').write_text(json.dumps(
      {'method': 'hand-made', 'seconds_per_measure': seconds_per_bar,
       'audio_duration_s': duration, 'measures': measures, 'warnings': []}), encoding='utf-8')
  (directory / 'geometry.json').write_text(json.dumps(
      {'slug': slug, 'pages': [{'number': n, 'width': 100.0, 'height': 200.0,
                                'measures': []} for n in range(1, pages + 1)],
       'warnings': []}), encoding='utf-8')
  (directory / 'meta.json').write_text(json.dumps(
      {'slug': slug, 'title': title, 'composer': '', 'bars': bars, 'duration_s': duration,
       'method': 'hand-made', 'seconds_per_measure': seconds_per_bar, 'pages': pages,
       'created_at': 1700000000.0, 'job_id': '',
       'score': {'name': 'hand-made.musicxml', 'sha256': 'deadbeefdeadbeef'},
       'audio': {'name': 'audio.wav', 'duration_s': duration}, 'quality': {},
       'warnings': []}, indent=1), encoding='utf-8')
  for number in range(1, pages + 1):
    (directory / 'pages' / ('%04d.svg' % number)).write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="200"/>',
        encoding='utf-8')
  return directory


def package(directory: pathlib.Path) -> bytes:
  from service.app.uploads import pack_piece
  return pack_piece(directory)


@pytest.fixture
def piece_dir(tmp_path):
  return make_piece(tmp_path / 'source' / 'hand-made')


# --------------------------------------------------------------------------- clients

def serving_client(tmp_path, monkeypatch, **env):
  """The API as a serving deployment: a token required, no worker, no analyzer."""
  from fastapi.testclient import TestClient

  from service.app.api import create_app
  from service.app.config import Settings

  monkeypatch.setenv('SERVICE_UPLOAD_TOKEN', env.pop('upload_token', TOKEN))
  monkeypatch.setenv('SERVICE_EMBEDDED_WORKER', '0')
  for key, value in env.items():
    monkeypatch.setenv(key, str(value))
  made = Settings(data_dir=tmp_path / 'data')
  made.ensure_dirs()
  return TestClient(create_app(made)), made


# --------------------------------------------------------------------------- uploads

def test_a_piece_can_be_uploaded_and_served(tmp_path, monkeypatch, piece_dir):
  """The whole hop: pack it here, send it, read it back through the page's own API."""
  client, settings = serving_client(tmp_path, monkeypatch)
  response = client.put('/api/v1/pieces/hand-made', content=package(piece_dir),
                        headers={'Authorization': 'Bearer %s' % TOKEN})
  assert response.status_code == 201, response.text
  body = response.json()
  assert body['installed'] and not body['replaced']
  assert (body['title'], body['bars'], body['pages']) == ('A hand-made piece', 8, 2)
  assert body['bytes'] > 22050 * 12 - 1000

  piece = client.get('/api/v1/pieces/hand-made')
  assert piece.status_code == 200, piece.text
  assert len(piece.json()['map']['measures']) == 8
  assert piece.json()['meta']['title'] == 'A hand-made piece'

  page = client.get('/api/v1/pieces/hand-made/pages/1.svg')
  assert page.status_code == 200 and page.text.startswith('<svg')

  clip = client.get('/api/v1/pieces/hand-made/audio?first_bar=2&last_bar=3')
  assert clip.status_code == 200, clip.text
  assert clip.content[:4] == b'RIFF'
  assert float(clip.headers['X-Clip-Start']) < 1.5        # bar 2 starts at 1.5 s

  listing = client.get('/api/v1/pieces').json()['pieces']
  assert [row['slug'] for row in listing] == ['hand-made']
  assert settings.pieces_dir.joinpath('hand-made').is_dir()


def test_the_upload_needs_the_token(tmp_path, monkeypatch, piece_dir):
  """A serving deployment publishes for whoever holds the token, and nobody else."""
  client, _ = serving_client(tmp_path, monkeypatch)
  payload = package(piece_dir)

  assert client.put('/api/v1/pieces/hand-made', content=payload).status_code == 401
  assert client.put('/api/v1/pieces/hand-made', content=payload,
                    headers={'Authorization': 'Bearer wrong'}).status_code == 401
  assert client.put('/api/v1/pieces/hand-made', content=payload,
                    headers={'X-Upload-Token': 'wrong'}).status_code == 401
  # Either header carries it: the bearer one is shared with the broker's own tokens, and
  # an explicit one exists so a mis-sent credential is not a confusing 401.
  assert client.put('/api/v1/pieces/hand-made', content=payload,
                    headers={'X-Upload-Token': TOKEN}).status_code == 201
  assert client.put('/api/v1/pieces/other', content=payload,
                    headers={'Authorization': 'Bearer %s' % TOKEN}).status_code == 201


def test_each_publishing_machine_can_have_its_own_token(tmp_path, monkeypatch, piece_dir):
  """The list exists so revoking one worker does not rotate everyone's secret."""
  client, _ = serving_client(tmp_path, monkeypatch,
                             upload_token='%s,%s' % (TOKEN, OTHER_TOKEN))
  payload = package(piece_dir)
  for token in (TOKEN, OTHER_TOKEN):
    response = client.put('/api/v1/pieces/hand-made', content=payload,
                          headers={'Authorization': 'Bearer %s' % token})
    assert response.status_code == 201, token
  assert client.put('/api/v1/pieces/hand-made', content=payload,
                    headers={'Authorization': 'Bearer %s' % 'a-third'}).status_code == 401


def test_a_signed_in_person_may_publish_too(tmp_path, monkeypatch, piece_dir):
  """Publishing is a write, so it is a different door from reading -- but a user token
  that opens the pages may also put a piece there, which is what makes `curl` enough."""
  import time

  import jwt

  secret = 'the-brokers-own-key'
  client, _ = serving_client(tmp_path, monkeypatch, SERVICE_TRUST_JWT=secret)
  payload = package(piece_dir)
  user = jwt.encode({'sub': '7', 'exp': int(time.time()) + 600}, secret, algorithm='HS256')

  assert client.put('/api/v1/pieces/hand-made', content=payload).status_code == 401
  assert client.put('/api/v1/pieces/hand-made', content=payload,
                    headers={'Authorization': 'Bearer %s' % user}).status_code == 201
  # ... and the worker's own token still works, because it is not a user.
  assert client.put('/api/v1/pieces/hand-made', content=payload,
                    headers={'X-Upload-Token': TOKEN}).status_code == 201


def test_publishing_is_open_when_nothing_is_configured(tmp_path, monkeypatch, piece_dir):
  """Loopback with no token configured behaves like the rest of the service: no gate."""
  monkeypatch.delenv('SERVICE_UPLOAD_TOKEN', raising=False)
  monkeypatch.delenv('SERVICE_TRUST_JWT', raising=False)
  from fastapi.testclient import TestClient

  from service.app.api import create_app
  from service.app.config import Settings

  made = Settings(data_dir=tmp_path / 'data')
  made.ensure_dirs()
  client = TestClient(create_app(made))
  assert client.put('/api/v1/pieces/hand-made',
                    content=package(piece_dir)).status_code == 201


def test_a_read_only_deployment_still_accepts_pieces(tmp_path, monkeypatch, piece_dir):
  """`SERVICE_READ_ONLY` is about the queue, not about being unable to receive anything.

  A serving deployment has no worker and must refuse to queue an analysis -- and must
  accept the very pieces it exists to show. Changing the read-only middleware to block
  every POST would be an easy way to break this and only notice in production.
  """
  client, _ = serving_client(tmp_path, monkeypatch, SERVICE_READ_ONLY='1')
  assert client.post('/api/v1/jobs', files={'score': ('a.musicxml', b'<x/>')}).status_code == 405
  assert client.put('/api/v1/pieces/hand-made', content=package(piece_dir),
                    headers={'X-Upload-Token': TOKEN}).status_code == 201


def test_accepting_pieces_can_be_switched_off(tmp_path, monkeypatch, piece_dir):
  client, _ = serving_client(tmp_path, monkeypatch, SERVICE_ACCEPT_PIECES='0')
  response = client.put('/api/v1/pieces/hand-made', content=package(piece_dir),
                        headers={'X-Upload-Token': TOKEN})
  assert response.status_code == 405 and 'SERVICE_ACCEPT_PIECES' in response.text


# --------------------------------------------------------------------------- refusals

def test_the_slug_is_validated_rather_than_trusted(tmp_path, monkeypatch, piece_dir):
  """A slug is a directory name, so this is a security boundary and not a naming rule."""
  client, settings = serving_client(tmp_path, monkeypatch)
  payload = package(piece_dir)
  for slug in ('Hand-Made', 'hand_made', 'hand made', '-leading', 'x' * 65):
    response = client.put('/api/v1/pieces/%s' % slug, content=payload,
                          headers={'X-Upload-Token': TOKEN})
    assert response.status_code == 400, '%s -> %s' % (slug, response.status_code)

  # An encoded `../` never reaches the validation at all: a path parameter does not span a
  # slash, so this matches no route and the catch-all answers 405 for a PUT. What matters
  # is that nothing was written, which is what the two assertions below check -- and the
  # names *inside* a package are the ones that do reach the code (next test).
  escaped = client.put('/api/v1/pieces/%2e%2e%2fescape', content=payload,
                       headers={'X-Upload-Token': TOKEN})
  assert escaped.status_code in (400, 404, 405), escaped.status_code
  assert not settings.pieces_dir.joinpath('escape').exists()
  assert not settings.data_dir.joinpath('escape').exists()


def test_a_package_that_climbs_out_of_its_directory_is_refused(tmp_path, monkeypatch):
  """The names inside a zip are chosen by whoever built it, so none of them are trusted."""
  import io

  client, settings = serving_client(tmp_path, monkeypatch)
  buffer = io.BytesIO()
  with zipfile.ZipFile(buffer, 'w') as archive:
    archive.writestr('meta.json', '{"title": "escape"}')
    archive.writestr('map.json', '{"measures": []}')
    archive.writestr('audio.wav', b'RIFF')
    archive.writestr('pages/0001.svg', '<svg/>')
    archive.writestr('../escaped.txt', 'this must never be written')
    archive.writestr('/absolute.txt', 'nor this')

  response = client.put('/api/v1/pieces/escape', content=buffer.getvalue(),
                        headers={'X-Upload-Token': TOKEN})
  assert response.status_code == 400, response.text
  assert 'leaves the piece directory' in response.text
  assert not (settings.data_dir / 'escaped.txt').exists()
  assert not settings.data_dir.joinpath('pieces', 'escaped.txt').exists()
  assert not settings.pieces_dir.joinpath('escape').exists()


def test_a_package_missing_something_is_refused(tmp_path, monkeypatch):
  """Each of these is a piece that would fail later, in front of somebody reading it."""
  import io

  client, settings = serving_client(tmp_path, monkeypatch)

  def build(**members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
      for name, content in members.items():
        archive.writestr(name.replace('__', '/'), content)
    return buffer.getvalue()

  whole = {'meta.json': '{"title": "t"}', 'map.json': '{"measures": []}',
           'audio.wav': b'RIFF', 'pages__0001.svg': '<svg/>'}
  cases = {
      'no meta': {k: v for k, v in whole.items() if k != 'meta.json'},
      'no map': {k: v for k, v in whole.items() if k != 'map.json'},
      'no audio': {k: v for k, v in whole.items() if k != 'audio.wav'},
      'no pages': {k: v for k, v in whole.items() if k != 'pages__0001.svg'},
      'not json': dict(whole, **{'meta.json': 'not json at all'}),
      'unexpected file': dict(whole, **{'notes.txt': 'hello'}),
  }
  for description, members in cases.items():
    response = client.put('/api/v1/pieces/case', content=build(**members),
                          headers={'X-Upload-Token': TOKEN})
    assert response.status_code == 400, '%s -> %s' % (description, response.text)

  assert client.put('/api/v1/pieces/case', content=b'not a zip at all',
                    headers={'X-Upload-Token': TOKEN}).status_code == 400
  assert client.put('/api/v1/pieces/case', content=b'',
                    headers={'X-Upload-Token': TOKEN}).status_code == 400
  # None of it left debris for the next attempt to trip over.
  assert not settings.pieces_dir.joinpath('case').exists()
  assert not settings.pieces_dir.joinpath('case.building').exists()


def test_a_package_larger_than_the_limit_is_refused(tmp_path, monkeypatch, piece_dir):
  client, _ = serving_client(tmp_path, monkeypatch, SERVICE_MAX_PIECE_MB='0.0005')
  response = client.put('/api/v1/pieces/hand-made', content=package(piece_dir),
                        headers={'X-Upload-Token': TOKEN})
  assert response.status_code == 413, response.text


def test_uploading_a_piece_twice_replaces_it(tmp_path, monkeypatch, tmp_path_factory):
  """Why this is a PUT: the same package twice leaves the same piece, so the sending
  side can retry a send it never saw the answer to."""
  client, settings = serving_client(tmp_path, monkeypatch)
  first = make_piece(tmp_path / 'first', title='First title')
  second = make_piece(tmp_path / 'second', title='Second title')

  one = client.put('/api/v1/pieces/hand-made', content=package(first),
                   headers={'X-Upload-Token': TOKEN})
  assert one.status_code == 201 and one.json()['replaced'] is False
  two = client.put('/api/v1/pieces/hand-made', content=package(second),
                   headers={'X-Upload-Token': TOKEN})
  assert two.status_code == 201 and two.json()['replaced'] is True

  assert client.get('/api/v1/pieces/hand-made').json()['meta']['title'] == 'Second title'
  assert len(client.get('/api/v1/pieces').json()['pieces']) == 1
  # the swap left no `.previous` copy behind
  leftovers = [p.name for p in settings.pieces_dir.iterdir() if p.name != 'hand-made']
  assert leftovers == []


def test_serving_an_uploaded_piece_needs_no_analyzer(tmp_path, monkeypatch, piece_dir):
  """The claim the whole format exists for: analysing needs the analyzer, serving does not.

  This is the same trick as `test_pipeline.test_serving_a_piece_needs_no_analyzer` -- make
  every import of `analyzer` raise -- applied to the *upload* path, which is the one a
  container in a deployment actually runs.
  """
  import importlib.abc
  import sys

  class Blocked(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
      if name == 'analyzer' or name.startswith('analyzer.'):
        raise ImportError('the analyzer is not installed in a serving-only deployment')
      return None

  for name in [m for m in list(sys.modules) if m == 'analyzer' or m.startswith('analyzer.')]:
    monkeypatch.delitem(sys.modules, name)
  monkeypatch.setattr(sys, 'meta_path', [Blocked()] + list(sys.meta_path))

  client, _ = serving_client(tmp_path, monkeypatch)
  assert client.put('/api/v1/pieces/hand-made', content=package(piece_dir),
                    headers={'X-Upload-Token': TOKEN}).status_code == 201
  assert client.get('/api/v1/pieces/hand-made').status_code == 200
  assert client.get('/api/v1/pieces/hand-made/pages/1.svg').status_code == 200
  clip = client.get('/api/v1/pieces/hand-made/audio?first_bar=1&last_bar=2')
  assert clip.status_code == 200 and clip.content[:4] == b'RIFF'


# ----------------------------------------------------------------------- the outbox

def test_a_push_that_fails_keeps_the_piece_waiting(tmp_path, monkeypatch, piece_dir):
  """The page being down must not lose the piece, and must be visible rather than silent."""
  from service.app import outbox

  settings = outbox_settings(tmp_path, monkeypatch)
  outbox.enqueue(settings, 'hand-made', package(piece_dir), url=settings.publish_url)
  assert outbox.count(settings) == 1

  def refuse(request, timeout=None):
    raise urllib.error.URLError('connection refused')

  outcome = outbox.push(settings, outbox.pending(settings)[0], opener=refuse)
  assert outcome['ok'] is False and 'URLError' in outcome['reason']
  waiting = outbox.pending(settings)
  assert [entry['slug'] for entry in waiting] == ['hand-made']
  assert waiting[0]['attempts'] == 1 and 'connection refused' in waiting[0]['last_error']

  # ... and a later attempt, when the page is back, delivers and clears it.
  class Answer:
    status = 201
    def read(self, _n=None):
      return b'{"installed": true}'
    def __enter__(self):
      return self
    def __exit__(self, *_exc):
      return False

  sent = []
  outcome = outbox.push(settings, outbox.pending(settings)[0],
                        opener=lambda request, timeout=None: (sent.append(request),
                                                             Answer())[1])
  assert outcome['ok'] is True
  assert outbox.count(settings) == 0
  assert not (settings.outbox_dir / 'hand-made.zip').exists()
  assert sent[0].get_full_url() == settings.publish_url + '/api/v1/pieces/hand-made'
  assert sent[0].get_header('Authorization') == 'Bearer %s' % settings.publish_token


def test_a_piece_that_did_not_arrive_is_retried_before_newer_ones(tmp_path, monkeypatch,
                                                                 piece_dir):
  """Oldest first, so a page that was down for an hour catches up in order."""
  from service.app import outbox

  settings = outbox_settings(tmp_path, monkeypatch)
  for slug in ('first-piece', 'second-piece'):
    outbox.enqueue(settings, slug, package(piece_dir), url=settings.publish_url)
  order = []
  outbox.drain(settings, opener=lambda request, timeout=None: (
      order.append(request.get_full_url()),
      type('A', (), {'status': 201, 'read': lambda self, n=None: b'{}',
                     '__enter__': lambda self: self,
                     '__exit__': lambda self, *e: False})())[1])
  assert order == [settings.publish_url + '/api/v1/pieces/first-piece',
                   settings.publish_url + '/api/v1/pieces/second-piece']
  assert outbox.count(settings) == 0


def test_publishing_over_real_http(tmp_path, monkeypatch, piece_dir):
  """One test that uses a socket, because a mock cannot fail the way a network does."""
  from service.app import outbox
  from service.app.uploads import unpack_piece

  received = []
  server = RecordingPage(received)
  try:
    settings = outbox_settings(tmp_path, monkeypatch, url=server.url + '/note-seq')
    outcome = outbox.push(settings, outbox.enqueue(
        settings, 'hand-made', package(piece_dir), url=server.url + '/note-seq') and
        outbox.pending(settings)[0])
    assert outcome['ok'] is True, outcome
  finally:
    server.stop()

  assert len(received) == 1
  request, body = received[0]
  assert request['path'] == '/note-seq/api/v1/pieces/hand-made'
  assert request['authorization'] == 'Bearer %s' % TOKEN
  target = tmp_path / 'arrived'
  summary = unpack_piece(body, target, max_bytes=64 << 20)
  assert summary['title'] == 'A hand-made piece' and summary['pages'] == 2
  assert outbox.count(settings) == 0


def test_a_send_with_no_token_says_so_rather_than_broken_pipe(tmp_path, monkeypatch,
                                                              piece_dir):
  """Found by doing it: a refused upload answers before reading the body, so the sender
  sees a closed socket and no status code. `Broken pipe` on its own names neither end of
  the wire nor the cause."""
  from service.app import outbox

  monkeypatch.delenv('SERVICE_PUBLISH_TOKEN', raising=False)
  monkeypatch.delenv('SERVICE_UPLOAD_TOKEN', raising=False)
  settings = outbox_settings(tmp_path, monkeypatch)
  settings.publish_token = ''
  outbox.enqueue(settings, 'hand-made', package(piece_dir), url=settings.publish_url)

  def refused(request, timeout=None):
    raise BrokenPipeError(32, 'Broken pipe')

  outcome = outbox.push(settings, outbox.pending(settings)[0], opener=refused)
  assert outcome['ok'] is False
  assert 'no upload token is configured' in outcome['reason']

  # With a token there is nothing to explain away: the network simply failed.
  outcome = outbox.push(settings, outbox.pending(settings)[0], opener=refused,
                        token=TOKEN)
  assert 'no upload token' not in outcome['reason']


def test_a_one_off_address_overrides_what_the_marker_remembers(tmp_path, monkeypatch,
                                                               piece_dir):
  """A marker records where it was queued for, so a later setting cannot misdirect it --
  and `--to` is how somebody says "not that one, this one" on purpose.

  Found by using it: the first `--to` run kept hammering the old address, because the
  marker's own address won.
  """
  from service.app import outbox

  settings = outbox_settings(tmp_path, monkeypatch)
  outbox.enqueue(settings, 'hand-made', package(piece_dir),
                 url='http://the-old-page.invalid')
  urls = []
  outbox.drain(settings, url='http://the-new-page.example',
               opener=lambda request, timeout=None: (
                   urls.append(request.get_full_url()),
                   type('A', (), {'status': 201, 'read': lambda self, n=None: b'{}',
                                  '__enter__': lambda self: self,
                                  '__exit__': lambda self, *e: False})())[1])
  assert urls == ['http://the-new-page.example/api/v1/pieces/hand-made']
  assert outbox.count(settings) == 0


def test_a_piece_is_queued_before_it_is_sent(tmp_path, monkeypatch, piece_dir):
  """Order matters: a job killed mid-send leaves a queued piece, never a lost one."""
  from service.app import outbox, pipeline

  settings = outbox_settings(tmp_path, monkeypatch)
  settings.pieces_dir.mkdir(parents=True, exist_ok=True)
  piece_dir.rename(settings.pieces_dir / 'hand-made')

  seen = {}

  def refuse(request, timeout=None):
    # By the time the push is attempted, the marker is already on disk.
    seen['markers_at_push_time'] = [p.name for p in settings.outbox_dir.glob('*.json')]
    raise urllib.error.URLError('no route to host')

  monkeypatch.setattr(outbox.urllib.request, 'urlopen', refuse)
  delivery = pipeline.publish_remote(settings, 'hand-made')

  assert delivery['configured'] is True
  assert delivery['sent'] is False and delivery['queued'] is True
  assert seen['markers_at_push_time'] == ['hand-made.json']
  assert delivery['pending'] == 1
  assert 'no route to host' in delivery['reason']


def test_publishing_is_off_unless_an_address_is_configured(tmp_path, monkeypatch):
  """The default is the previous behaviour: a piece stays where it was analysed."""
  from service.app import outbox, pipeline

  settings = outbox_settings(tmp_path, monkeypatch, url='')
  delivery = pipeline.publish_remote(settings, 'anything')
  assert delivery['configured'] is False
  assert outbox.count(settings) == 0
  assert 'SERVICE_PUBLISH_URL' in delivery['reason']


@pytest.mark.skipif(not pathlib.Path('/home/wei/sheet_music/'
                                     'chopin-waltz-in-a-minor-op-34-no-2.mxl').is_file(),
                    reason='the Chopin score is needed')
def test_a_real_piece_packs_and_arrives(tmp_path, monkeypatch, analyzed):
  """The genuine article: an analysed piece, packed, sent, and unpacked on the far side."""
  from service.app import outbox, pipeline
  from service.app.uploads import unpack_piece

  received = []
  server = RecordingPage(received)
  try:
    settings = analyzed['settings']
    settings.publish_url = server.url + '/note-seq'
    settings.publish_token = TOKEN
    delivery = pipeline.publish_remote(settings, analyzed['slug'])
    assert delivery['sent'] is True, delivery
  finally:
    server.stop()

  assert len(received) == 1
  target = tmp_path / 'arrived'
  summary = unpack_piece(received[0][1], target, max_bytes=256 << 20)
  assert summary['pages'] == 9                       # the Chopin waltz's nine pages
  assert summary['bars'] == 204
  assert (target / 'pages' / '0009.svg').is_file()
  assert (target / 'audio.wav').stat().st_size > 10_000_000
  assert outbox.count(settings) == 0


# --------------------------------------------------------------------------- helpers

def outbox_settings(tmp_path, monkeypatch, url=None):
  """Settings with an address to publish to and a token to publish with."""
  from service.app.config import Settings

  monkeypatch.setenv('SERVICE_PUBLISH_URL',
                     url if url is not None else 'http://page.invalid/note-seq')
  monkeypatch.setenv('SERVICE_PUBLISH_TOKEN', TOKEN)
  settings = Settings(data_dir=tmp_path / 'data')
  settings.ensure_dirs()
  return settings


class RecordingPage:
  """A page that accepts packages, on a real socket, and remembers what it got."""

  def __init__(self, received):
    self.received = received
    page = self

    class Handler(http.server.BaseHTTPRequestHandler):
      protocol_version = 'HTTP/1.1'

      def do_PUT(self):                    # noqa: N802 - the name http.server calls
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length)
        page.received.append(({'path': self.path,
                               'authorization': self.headers.get('Authorization'),
                               'content_type': self.headers.get('Content-Type')}, body))
        self.send_response(201)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', '22')
        self.end_headers()
        self.wfile.write(b'{"installed": true}\n\n')

      def log_message(self, *_args):
        pass

    self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
    self.thread.start()

  @property
  def url(self) -> str:
    return 'http://127.0.0.1:%d' % self.server.server_address[1]

  def stop(self) -> None:
    self.server.shutdown()
    self.server.server_close()
