"""The API: what it accepts, what it refuses, and the four things the page asks for.

Nothing here runs the analysis -- that is `test_pipeline`, and the `analyzed` fixture in
`conftest` -- so these stay fast and say what they mean. What they cover is the layer
that a browser talks to, including the failure paths, because those are the ones a
person actually meets: a wrong file type, an empty upload, a range of bars the map does
not know, a page that does not exist.
"""

from __future__ import annotations

import pathlib

import pytest

from conftest import RECORDING, SCORE, have_material


def upload(client, *, score=SCORE, audio=RECORDING, url='', title='a piece',
           score_name=None, audio_name=None):
  files = {}
  handles = []
  try:
    if score is not None:
      handle = open(score, 'rb')
      handles.append(handle)
      files['score'] = (score_name or pathlib.Path(score).name, handle,
                        'application/octet-stream')
    if audio is not None:
      handle = open(audio, 'rb')
      handles.append(handle)
      files['audio'] = (audio_name or pathlib.Path(audio).name, handle, 'audio/wav')
    return client.post('/api/v1/jobs', files=files or None,
                       data={'youtube_url': url, 'title': title})
  finally:
    for handle in handles:
      handle.close()


# --------------------------------------------------------------------------- status

def test_status_says_what_this_machine_can_do(client):
  body = client.get('/api/v1/status').json()
  assert body['service'] == 'note-seq'
  assert set(body['capabilities']) == {'python', 'ffmpeg', 'yt_dlp', 'analyze', 'analyzer'}
  assert body['jobs']['queued'] == 0
  assert body['pieces'] == 0


def test_the_api_documents_itself(client):
  schema = client.get('/openapi.json').json()
  assert '/api/v1/jobs' in schema['paths']
  assert '/api/v1/pieces/{slug}/audio' in schema['paths']


def test_the_page_is_served_from_the_build_directory(tmp_path, monkeypatch):
  """`/`, an asset, a client-side route, and a path that is not a file of the page."""
  dist = _fake_build(tmp_path)
  client = _client_for(dist, monkeypatch)

  page = client.get('/')
  assert page.status_code == 200 and 'the page of the service' in page.text

  asset = client.get('/assets/app-abc123.js')
  assert asset.status_code == 200, asset.text
  assert 'console.log(1)' in asset.text

  # A client-side route is not a file: it is the page, which routes it itself.
  deeper = client.get('/pieces/serenade-7')
  assert deeper.status_code == 200 and 'the page of the service' in deeper.text


def test_the_page_is_served_when_a_proxy_strips_a_prefix(tmp_path, monkeypatch):
  """The deployment this service is built for: a path prefix, stripped by the proxy.

  Under `Host(domain) && PathPrefix('/note-seq')` with Traefik's `stripPrefix`, the app
  sees `/assets/app-abc123.js` and `SERVICE_ROOT_PATH` says `/note-seq`. That combination
  -- and only that combination -- made every asset a 404 when the assets were a
  `StaticFiles` mount: `Mount.matches` sets the child scope's `root_path` to
  `/note-seq/assets`, `get_route_path` cannot strip that from a path that no longer has
  the prefix, and `StaticFiles.lookup_path` then refuses the absolute path it is left
  with. The page loaded, and came up blank. Empty `SERVICE_ROOT_PATH` answered 200 for the
  same request, which is why serving on the LAN never showed it.
  """
  dist = _fake_build(tmp_path)
  plain = _client_for(dist, monkeypatch)
  assert plain.get('/assets/app-abc123.js').status_code == 200

  prefixed = _client_for(dist, monkeypatch, root_path='/note-seq')
  asset = prefixed.get('/assets/app-abc123.js')
  assert asset.status_code == 200, (
      'the page cannot load its own JavaScript under a stripped prefix: %s' % asset.text)
  assert 'console.log(1)' in asset.text
  assert prefixed.get('/pieces/serenade-7').status_code == 200


def test_a_path_that_climbs_out_of_the_build_directory_is_not_served(tmp_path, monkeypatch):
  """`dist / path` with `../../` in it reads outside the directory, so it is resolved.

  This route is the page's catch-all, and the page is on the internet in the deployment
  this service is written for. `%2e%2e%2f` reaches the handler as `../`, which Starlette
  does not collapse.
  """
  dist = _fake_build(tmp_path)
  secret = tmp_path / 'secret.txt'
  secret.write_text('not for the browser')
  client = _client_for(dist, monkeypatch)

  response = client.get('/%2e%2e/secret.txt')
  assert 'not for the browser' not in response.text


def _fake_build(tmp_path) -> pathlib.Path:
  dist = tmp_path / 'dist'
  (dist / 'assets').mkdir(parents=True)
  (dist / 'index.html').write_text('<html>the page of the service</html>')
  (dist / 'assets' / 'app-abc123.js').write_text('console.log(1)')
  return dist


def _client_for(dist, monkeypatch, root_path=''):
  """A client on a service serving `dist`, optionally behind a stripped prefix."""
  from fastapi.testclient import TestClient
  from service.app.api import create_app
  from service.app.config import Settings

  monkeypatch.setenv('SERVICE_FRONTEND', str(dist))
  if root_path:
    monkeypatch.setenv('SERVICE_ROOT_PATH', root_path)
  else:
    monkeypatch.delenv('SERVICE_ROOT_PATH', raising=False)
  made = Settings(data_dir=dist.parent / 'data')
  made.embedded_worker = False
  made.ensure_dirs()
  return TestClient(create_app(made))


# ------------------------------------------------------- what a container has to install

# The analyzer, and the tools only its pipeline touches: fetching a YouTube URL and
# importing the analysis both belong to the *worker*. A serving deployment has none of
# them, and `test_serving_a_piece_needs_no_analyzer` is what keeps it that way.
WORKER_ONLY = {'analyzer', 'note_seq', 'librosa', 'verovio', 'yt_dlp'}
# Brought in by fastapi, and deliberately not pinned here: a version chosen by the
# framework is more likely to be right than one chosen by this file.
TRANSITIVE = {'starlette', 'pydantic', 'anyio'}


def _imported_modules(package: pathlib.Path) -> set[str]:
  """Every top-level module named by an import statement in `package`.

  `ast.walk` rather than a line scan on purpose: the import that broke the first
  container (`import soundfile as sf` inside `slice_file`) is two levels down inside a
  function, which is exactly the kind of import a line scan of the module headers misses.
  """
  import ast
  modules: set[str] = set()
  for path in sorted(package.glob('*.py')):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
      if isinstance(node, ast.Import):
        modules.update(alias.name.split('.')[0] for alias in node.names)
      elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        modules.add(node.module.split('.')[0])
  return modules


def _declared_distributions() -> set[str]:
  import re
  text = (pathlib.Path(__file__).resolve().parents[1] / 'requirements.txt').read_text()
  names = set()
  for line in text.splitlines():
    line = line.split('#', 1)[0].strip()
    if line and not line.startswith('-'):
      names.add(re.split(r'[<>=!\[;]', line, 1)[0].strip().lower().replace('_', '-'))
  return names


def test_requirements_declare_every_module_the_service_imports():
  """The image is built from `requirements.txt`, so a missing line is a broken image.

  Necessary because the *development* environment cannot show the problem: the
  checkout's venv has soundfile, numpy and yt-dlp for the analyzer's sake, so a missing
  declaration is invisible on this machine and fails only in the container -- which is
  how it happened. This compares the list against the imports rather than against a
  hand-kept note.
  """
  import importlib.metadata
  import sys

  declared = _declared_distributions()
  distributions = importlib.metadata.packages_distributions()
  missing = {}
  for module in sorted(_imported_modules(pathlib.Path(__file__).resolve().parents[1] / 'app')):
    if module in sys.stdlib_module_names or module in WORKER_ONLY or module in TRANSITIVE:
      continue
    # Not installed here at all (a worker-only machine, say): nothing to compare against,
    # and the module is the analyzer's business anyway.
    found = {name.lower().replace('_', '-') for name in distributions.get(module, [])}
    if found and not (found & declared):
      missing[module] = sorted(found)
  assert not missing, (
      'imported by service/app but not in requirements.txt: %s -- a container built from '
      'that file will fail at the first request that needs them' % missing)


# --------------------------------------------------------------------------- uploads

@have_material
def test_an_upload_is_queued_with_its_files_on_disk(client, settings):
  response = upload(client, title='Chopin, Waltz in A minor')
  assert response.status_code == 201, response.text
  job = response.json()
  assert job['status'] == 'queued'
  assert job['title'] == 'Chopin, Waltz in A minor'

  job_dir = settings.job_dir(job['id'])
  assert (job_dir / 'score.mxl').is_file()
  assert (job_dir / 'upload.wav').is_file()
  assert job['params']['score'].endswith('score.mxl')
  assert client.get('/api/v1/jobs').json()['jobs'][0]['id'] == job['id']


@have_material
def test_a_score_without_a_recording_is_refused(client):
  response = upload(client, audio=None)
  assert response.status_code == 400
  assert 'audio file or a YouTube URL' in response.json()['detail']
  assert client.get('/api/v1/jobs').json()['jobs'] == [], 'nothing was queued'


@have_material
def test_an_audio_file_without_a_score_is_refused(client):
  response = client.post('/api/v1/jobs',
                         files={'audio': ('a.wav', b'not really audio', 'audio/wav')})
  assert response.status_code == 422, 'the score is required'


def test_a_score_that_is_not_musicxml_is_refused(client, tmp_path):
  wrong = tmp_path / 'score.pdf'
  wrong.write_bytes(b'%PDF-1.4 not a score')
  response = upload(client, score=wrong, audio=None, url='https://example.com/x')
  assert response.status_code == 400
  assert 'MusicXML' in response.json()['detail']


def test_an_audio_format_that_is_not_audio_is_refused(client, tmp_path):
  score = tmp_path / 'score.musicxml'
  score.write_text('<score-partwise/>')
  notes = tmp_path / 'notes.txt'
  notes.write_text('this is not a recording')
  response = upload(client, score=score, audio=notes, audio_name='notes.txt')
  assert response.status_code == 400
  assert 'format is not recognised' in response.json()['detail']


def test_an_empty_upload_is_refused(client, tmp_path):
  empty = tmp_path / 'empty.musicxml'
  empty.write_bytes(b'')
  response = upload(client, score=empty, audio=None, url='https://example.com/x')
  assert response.status_code == 400
  assert 'empty' in response.json()['detail']


def test_a_score_larger_than_the_limit_is_refused(client, settings, tmp_path):
  """Refused while it is being written, not after it has been read into memory."""
  settings.max_score_mb = 0.001                     # about a kilobyte
  big = tmp_path / 'big.musicxml'
  big.write_bytes(b'<score-partwise>' + b'x' * 4096)
  response = upload(client, score=big, audio=None, url='https://example.com/x')
  assert response.status_code == 413
  assert 'larger than' in response.json()['detail']
  leftovers = [f for d in settings.jobs_dir.iterdir() for f in d.rglob('*') if f.is_file()]
  assert not leftovers, 'the half-written upload is deleted, not left in the job'


def test_a_youtube_url_is_accepted_without_an_audio_file(client):
  if not client.get('/api/v1/status').json()['capabilities']['yt_dlp']:
    pytest.skip('yt-dlp is not installed')
  response = upload(client, audio=None, url='https://www.youtube.com/watch?v=dQw4w9WgXcQ')
  assert response.status_code == 201
  assert response.json()['params']['youtube_url'].startswith('https://www.youtube.com')


# --------------------------------------------------------------------------- jobs

def test_an_unknown_job_is_a_404(client):
  assert client.get('/api/v1/jobs/nope').status_code == 404


def test_only_a_failed_job_can_be_queued_again(client, store):
  job = store.create({'score': '/tmp/x.mxl'})
  assert client.post('/api/v1/jobs/%s/retry' % job.id).status_code == 409
  store.fail(job.id, store.claim('worker').token, 'it went wrong')
  assert client.post('/api/v1/jobs/%s/retry' % job.id).json()['status'] == 'queued'


def test_the_job_list_can_be_filtered(client, store):
  done = store.create({'score': '/tmp/a.mxl'})
  store.finish(done.id, store.claim('worker').token, 'a-piece')
  store.create({'score': '/tmp/b.mxl'})
  assert len(client.get('/api/v1/jobs').json()['jobs']) == 2
  assert len(client.get('/api/v1/jobs?status=done').json()['jobs']) == 1
  assert len(client.get('/api/v1/jobs?status=queued').json()['jobs']) == 1


# --------------------------------------------------------------------------- pieces

def test_an_unknown_piece_is_a_404(client):
  assert client.get('/api/v1/pieces/nope').status_code == 404
  assert client.get('/api/v1/pieces/nope/pages/1.svg').status_code == 404
  assert client.get('/api/v1/pieces/nope/audio').status_code == 404


def test_no_pieces_yet_is_an_empty_list(client):
  assert client.get('/api/v1/pieces').json() == {'pieces': []}


# --------------------------------------------------------------------------- frontend

def test_the_page_is_served_when_it_has_been_built(client, settings):
  """Either the built page or an explanation of how to build it -- never a bare 404."""
  if not (settings.frontend_dist / 'index.html').is_file():
    pytest.skip('the frontend has not been built')
  page = client.get('/')
  assert page.status_code == 200
  assert '<div id="root">' in page.text
  assert 'assets/' in page.text
  deep = client.get('/pieces/anything')
  assert deep.status_code == 200, 'client-side routes fall back to the page'
  asset = page.text.split('src="')[1].split('"')[0]
  assert client.get(asset).status_code == 200
  assert client.get('/api/v1/nonexistent').status_code == 404, \
      'an unknown API path is a 404, not the page'


def test_the_page_that_is_not_built_yet_explains_itself(client, settings):
  settings.frontend_dist = settings.data_dir / 'no-frontend-here'
  from fastapi.testclient import TestClient

  from service.app.api import create_app
  with TestClient(create_app(settings)) as fresh:
    page = fresh.get('/')
    assert page.status_code == 200
    assert 'npm run build' in page.text
