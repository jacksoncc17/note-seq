"""Fixtures for the service's tests.

Two rules here, both learned while writing them:

* **A fresh data directory per test.** The queue is state, and a test that inherits
  another test's jobs is testing the leftovers rather than the code -- the first version
  of this file shared one database, and the claim tests failed because a *previous*
  test's job was still sitting in the queue.
* **The slow analysis runs once, for the whole session.** Aligning 204 bars takes a few
  seconds, and the result is what most of these tests want; re-running it per test would
  make the suite about the alignment rather than about the service.

The tests run the real pipeline -- real Verovio, real DTW alignment, real ffmpeg -- on
real material, because every step of it is the thing being tested. When the score and
recording are not on this machine, the tests that need them say so and skip.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

SERVICE = pathlib.Path(__file__).resolve().parents[1]
REPO = SERVICE.parent
for path in (str(REPO), str(REPO / 'analyzer'), str(SERVICE)):
  if path not in sys.path:
    sys.path.insert(0, path)

# The broker plugin lives in its own directory, exactly as the reference worker loads
# it. Importing it by name here means a test and the loader get the *same* module
# object: two copies would give two distinct NoteSeqError classes, and `pytest.raises`
# would then fail on an exception that looks identical.
PLUGIN_DIR = SERVICE / 'broker'
if str(PLUGIN_DIR) not in sys.path:
  sys.path.insert(0, str(PLUGIN_DIR))

# The score and recording the analyzer's own tests use: 204 bars, nine engraved pages, a
# real piano performance of 309.7 s. Small enough to align inside a test, real enough
# that everything about it is the real thing.
SCORE = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
SHORT_SCORE = pathlib.Path('/home/wei/sheet_music/Intermezzo_left.mxl')
RECORDING = REPO / 'analyzer' / 'cache' / 'audio' / 'chopin-waltz-in-a-minor-op-34-no-2.wav'

have_material = pytest.mark.skipif(
    not (SCORE.is_file() and RECORDING.is_file()),
    reason='the Chopin score and its cached recording are needed')


def make_piece(directory, slug='hand-made', *, pages=2, title='A hand-made piece',
               bars=8, seconds_per_bar=1.5):
  """A servable piece written by hand: the five things the page reads, and nothing else.

  No analysis and no analyzer, so a test about the *store* -- serving, uploading, deleting
  -- runs in milliseconds on any machine. Shared because two test files need one: the
  upload tests, and the housekeeping tests.
  """
  import json

  import numpy as np
  import soundfile as sf

  directory = pathlib.Path(directory)
  directory.mkdir(parents=True, exist_ok=True)
  (directory / 'pages').mkdir(exist_ok=True)
  duration = bars * seconds_per_bar
  sf.write(str(directory / 'audio.wav'),
           np.zeros(int(22050 * duration), dtype='float32'), 22050)
  measures = [{'measure': bar, 'play_index': bar - 1,
               'start_s': round((bar - 1) * seconds_per_bar, 3),
               'end_s': round(bar * seconds_per_bar, 3), 'confidence': 1.0}
              for bar in range(1, bars + 1)]
  (directory / 'map.json').write_text(json.dumps(
      {'method': 'hand-made', 'seconds_per_measure': seconds_per_bar,
       'audio_duration_s': duration, 'measures': measures, 'warnings': []}),
      encoding='utf-8')
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


def package(directory):
  """That piece as one zip, the way a worker sends it."""
  from service.app.uploads import pack_piece
  return pack_piece(directory)


def settings_for(directory):
  """Settings on a directory, with the worker left for the test to drive."""
  from service.app.config import Settings
  made = Settings(data_dir=pathlib.Path(directory))
  made.embedded_worker = False          # a test claims and runs jobs itself
  made.ensure_dirs()
  return made


@pytest.fixture
def settings(tmp_path):
  return settings_for(tmp_path / 'data')


@pytest.fixture
def store(settings):
  from service.app.jobs import JobStore
  return JobStore(settings)


@pytest.fixture
def client(settings):
  """The API over an in-process transport: no port, no server, the same code path."""
  from fastapi.testclient import TestClient

  from service.app.api import create_app
  with TestClient(create_app(settings)) as test_client:
    yield test_client


@pytest.fixture(scope='session')
def analyzed(tmp_path_factory):
  """One piece, analysed through the real API and the real worker.

  Returns the client, the settings, the finished job and the slug, so a test can ask for
  whatever part of it it is about without redoing the analysis.
  """
  from fastapi.testclient import TestClient

  from service.app.api import create_app
  from service.app.worker import Worker

  if not (SCORE.is_file() and RECORDING.is_file()):
    pytest.skip('the Chopin score and its recording are needed')
  settings = settings_for(tmp_path_factory.mktemp('analyzed'))
  client = TestClient(create_app(settings))
  with open(SCORE, 'rb') as score, open(RECORDING, 'rb') as audio:
    response = client.post('/api/v1/jobs',
                           files={'score': (SCORE.name, score, 'application/octet-stream'),
                                  'audio': (RECORDING.name, audio, 'audio/wav')},
                           data={'title': 'Chopin, Waltz in A minor'})
  assert response.status_code == 201, response.text
  job = response.json()
  assert Worker(settings).run_once(), 'the job should have been waiting in the queue'
  detail = client.get('/api/v1/jobs/%s' % job['id']).json()
  if detail['status'] != 'done':
    pytest.fail('the analysis failed: %s\n%s' % (detail.get('error'), detail.get('log')))
  return {'client': client, 'settings': settings, 'job': detail, 'slug': detail['slug']}
