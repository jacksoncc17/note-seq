"""The broker plugin: registered, validated and dispatched by the reference worker's code.

The plugin is a contract with another program, so the tests use that program's own code
rather than a re-statement of it:

* `worker_plugins.loader.load_plugins` -- the real loader, which enforces the
  name/module match and the presence of every attribute;
* `app.param_schema.check_schema` -- the broker's real validator, which refuses a schema
  it cannot fully enforce; a plugin that ships one would be rejected at registration;
* `worker.process_one` -- the real dispatch, with a stand-in for the broker, which
  stages inputs, calls the plugin, and uploads what it returns.

Those three live in the `selfCommentary` checkout, which is read-only from here and may
not exist on every machine, so these tests skip when it is absent. Everything else --
that the artifacts hold the right music -- runs regardless.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

from note_seq_align.service import (EVERYTHING, KIND_LINK, KIND_REPORT,
                                    RESULT_ONLY, WITH_MAP, WITH_PAGES,
                                    NoteSeqError)

from conftest import RECORDING, SCORE, have_material

# The reference stack: the broker/worker half of selfCommentary. Read-only, and only
# imported for the contract tests.
REFERENCE = pathlib.Path('/home/wei/playground/goGames/selfCommentary/'
                         'full-stack-fastapi-template')
PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1] / 'broker'

needs_reference = pytest.mark.skipif(
    not (REFERENCE / 'worker_plugins' / 'loader.py').is_file(),
    reason='the reference broker checkout is not on this machine')


@pytest.fixture
def plugin(settings):
  """The plugin as the worker would get it, loaded from its directory by name.

  It is handed the test's `settings`, so a piece it publishes lands in this test's
  temporary data directory. `plugin()` with no arguments -- what the worker does -- uses
  `SERVICE_DATA` or `service/data`, which is right in a deployment and wrong in a test:
  without this, running the suite wrote pieces into the service's real store.
  """
  sys.path.insert(0, str(PLUGIN_DIR))
  try:
    import importlib
    module = importlib.import_module('note_seq_align')
    return module.plugin(settings=settings)
  finally:
    sys.path.remove(str(PLUGIN_DIR))


# --------------------------------------------------------------------------- the contract

@needs_reference
def test_the_reference_loader_accepts_it():
  """Their loader, their validation: name matches the directory, all four attributes."""
  sys.path.insert(0, str(REFERENCE))
  try:
    from worker_plugins.loader import describe, load_plugins
    plugins = load_plugins([PLUGIN_DIR])
  finally:
    sys.path.remove(str(REFERENCE))
  assert 'note_seq_align' in plugins, describe(plugins)
  loaded = plugins['note_seq_align']
  assert loaded.title and isinstance(loaded.artifact_kinds, tuple)
  assert callable(loaded.run)
  assert 'measure_map' in loaded.artifact_kinds


@needs_reference
def test_the_broker_can_enforce_the_parameter_schema(plugin):
  """A schema the broker cannot fully check is refused at registration, so this is the
  check that decides whether the service can be published at all.

  `validate` is keyword-only on their side (`instance=`, `schema=`) -- found by reading
  it rather than by guessing, which is the whole point of testing against their module.
  """
  sys.path.insert(0, str(REFERENCE / 'backend'))
  try:
    from app import param_schema
  finally:
    sys.path.remove(str(REFERENCE / 'backend'))

  param_schema.check_schema(plugin.param_schema)       # raises SchemaError if it cannot
  good = {'score': ['a.mxl']}
  param_schema.validate(instance=good, schema=plugin.param_schema)
  filled = param_schema.apply_defaults(dict(good), plugin.param_schema)
  assert filled['score'] == ['a.mxl']

  for bad in ({},                                        # the score is required
              {'score': ['a.mxl'], 'first_bar': 0},      # below the minimum
              {'score': ['a.mxl'], 'nonsense': 1}):      # additionalProperties: false
    with pytest.raises(param_schema.ValidationError):
      param_schema.validate(instance=bad, schema=plugin.param_schema)


def test_every_declared_kind_is_one_the_plugin_can_produce(plugin):
  """`artifact_kinds` is what the UI shows before anything has run."""
  assert set(plugin.artifact_kinds) == {'range_audio', 'page_url', 'score_page',
                                        'measure_map', 'report'}
  assert plugin.name == 'note_seq_align'
  assert plugin.name == PLUGIN_DIR.joinpath('note_seq_align').name, \
      'the loader requires the service name to be the package name'
  assert plugin.param_schema['properties']['score']['format'] == 'files', \
      'the form needs this to know it is a file field'


# --------------------------------------------------------------------------- refusals

def test_a_submission_with_no_recording_is_refused(plugin, tmp_path):
  staged = _stage(tmp_path, score=SCORE)
  with pytest.raises(NoteSeqError) as caught:
    plugin.run({'score': ''}, tmp_path, tmp_path / 'out', lambda *a: None, staged)
  assert 'audio file or give a YouTube URL' in str(caught.value)


def test_both_a_recording_and_a_url_is_refused(plugin, tmp_path):
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  with pytest.raises(NoteSeqError) as caught:
    plugin.run({'youtube_url': 'https://example.com/x'}, tmp_path, tmp_path / 'out',
               lambda *a: None, staged)
  assert 'not both' in str(caught.value)


def test_a_score_is_required(plugin, tmp_path):
  with pytest.raises(NoteSeqError) as caught:
    plugin.run({}, tmp_path, tmp_path / 'out', lambda *a: None, {})
  assert 'no score file' in str(caught.value)


def test_half_a_range_is_refused(plugin, tmp_path):
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  with pytest.raises(NoteSeqError) as caught:
    plugin.run({'first_bar': 40}, tmp_path, tmp_path / 'out', lambda *a: None, staged)
  assert 'both first_bar and last_bar' in str(caught.value)


@have_material
def test_what_a_job_returns_is_the_submitters_choice(plugin, tmp_path):
  """The broker's dashboard lists every artifact a job uploads and there is no hidden
  flag on an artifact, so the way to keep the workings out of it is not to send them."""
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  out = tmp_path / 'out'

  def run(include, name):
    produced = plugin.run({'first_bar': 40, 'last_bar': 41, 'include': include},
                          tmp_path / ('work-' + name), out / name, lambda *a: None, staged)
    return {a['kind'] for a in produced}

  assert run(RESULT_ONLY, 'result') == {'range_audio', 'page_url'}
  assert run(WITH_PAGES, 'pages') == {'range_audio', 'page_url', 'score_page'}
  assert run(WITH_MAP, 'map') == {'range_audio', 'page_url', 'measure_map'}
  assert run(EVERYTHING, 'all') == {'range_audio', 'page_url', 'score_page', 'measure_map',
                                    'report'}
  # An unknown value is treated as the default rather than failing a job.
  assert run('nonsense', 'other') == {'range_audio', 'page_url'}


@have_material
def test_the_link_artifact_is_a_file_that_says_where_to_go(plugin, settings, tmp_path):
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  out = tmp_path / 'out'
  produced = plugin.run({'first_bar': 40, 'last_bar': 41}, tmp_path / 'work', out,
                        lambda *a: None, staged)
  link = pathlib.Path(next(a['path'] for a in produced if a['kind'] == KIND_LINK))
  # `.uri` is load-bearing: that extension is what makes the worker send
  # `text/uri-list`, which is how a frontend can tell a link from a download.
  assert link.name == 'open-the-page.uri'
  import mimetypes
  assert mimetypes.guess_type(link.name)[0] == 'text/uri-list'
  lines = link.read_text().splitlines()
  assert lines[0] == 'http://localhost:8100/pieces/waltz-op-34-no-2-in-a-minor-lento', lines
  assert all(line.startswith('#') for line in lines[1:] if line), \
      'the rest of a uri-list is comments'
  assert '204 bars' in lines[1]


@have_material
def test_a_job_always_returns_at_least_one_artifact(plugin, tmp_path):
  """Their worker refuses a job that produced nothing, and it is right to: an empty
  result is not a result. With publishing off and no range, the report is what is left."""
  import note_seq_align

  off = note_seq_align.plugin(settings=plugin.settings, viewer=False)
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  produced = off.run({}, tmp_path / 'work', tmp_path / 'out', lambda *a: None, staged)
  assert [a['kind'] for a in produced] == [KIND_REPORT]
  assert all(pathlib.Path(a['path']).is_file() for a in produced)


def test_the_fake_run_needs_no_music_at_all(plugin, tmp_path):
  """`--fake-pipeline` is how the queue plumbing is tested; it must not touch audio."""
  produced = plugin.fake_run({}, tmp_path, tmp_path / 'out', lambda *a: None, {})
  kinds = {artifact['kind'] for artifact in produced}
  assert kinds == {'measure_map', 'range_audio', 'score_page', KIND_LINK, 'report'}
  for artifact in produced:
    assert pathlib.Path(artifact['path']).is_file()
  assert json.loads(pathlib.Path(produced[0]['path']).read_text())['method'] == 'fake'


# --------------------------------------------------------------------------- the work

@have_material
def test_it_produces_the_map_the_clip_and_the_highlighted_page(plugin, tmp_path):
  """One real job: the three artifacts, and what is actually in them."""
  import soundfile as sf

  out = tmp_path / 'out'
  messages = []
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  produced = plugin.run({'first_bar': 40, 'last_bar': 83, 'title': 'Chopin'},
                        tmp_path / 'work', out, lambda pct, msg: messages.append((pct, msg)),
                        staged)
  by_kind = {artifact['kind']: pathlib.Path(artifact['path']) for artifact in produced}
  # The default is the result of the work -- the passage and where to open it -- not the
  # workings: the map, the engraved pages and the report are all in the page, and the
  # broker's dashboard lists every artifact a job uploads.
  assert set(by_kind) == {'range_audio', 'page_url'}, sorted(by_kind)
  assert all(path.is_file() and path.stat().st_size > 0 for path in by_kind.values())

  # Bars 40-83 span three engraved pages when the pages are asked for: the broker keys an
  # artifact by (job, kind), and a passage that crosses a page turn has to be visible on
  # both sides of it.
  everything = {a['kind'] for a in plugin.run({'first_bar': 40, 'last_bar': 83, 'title': 'Chopin',
                                               'include': 'everything'},
                                              tmp_path / 'work2', out / 'everything',
                                              lambda *a: None, staged)}
  assert {'score_page', 'score_page_2', 'score_page_3'} <= everything, sorted(everything)

  # The map: the analyzer's own numbers, which the desktop app also produces.
  payload = json.loads((out / 'everything' / 'measure_map.json').read_text())
  assert payload['method'] == 'dtw'
  assert len(payload['measures']) == 204
  bar40 = next(m for m in payload['measures'] if m['measure'] == 40)
  assert bar40['start_s'] == pytest.approx(62.38, abs=0.1)
  assert payload['quality']['normalized_cost'] == pytest.approx(0.7046, abs=0.001)
  assert payload['title'] == 'Chopin'
  assert payload['composer'] == 'Frédéric Chopin'

  # The clip: the passage, plus the run-in and run-out the desktop app plays it with.
  audio, rate = sf.read(str(by_kind['range_audio']), dtype='float32')
  measures = {m['measure']: m for m in payload['measures']}
  music = measures[83]['end_s'] - measures[40]['start_s']
  assert rate == 22050
  assert len(audio) / 22050 == pytest.approx(music + 0.37, abs=0.05)
  assert abs(audio).max() > 0.01, 'the clip is a recording, not silence'

  # The pages that came with the map: what a consumer needs to put a bar on the image.
  assert [page['number'] for page in payload['pages']] == list(range(1, 10))
  assert len([box for page in payload['pages'] for box in page['measures']]) == 204

  # The image: a real PNG, with the wash *on the selected bars*. Those are two claims,
  # and the second is the one that catches a highlight drawn in the wrong coordinate
  # space -- Verovio's outer SVG is in pixels and the boxes are in viewBox units, so a
  # rectangle placed one level too high lands off the page while the file still has
  # blue in it somewhere.
  page_png = out / 'everything' / 'score_page_2.png'
  image = page_png.read_bytes()
  assert image[:8] == b'\x89PNG\r\n\x1a\n'
  page_two = next(page for page in payload['pages'] if page['number'] == 2)
  assert _tinted_at(page_png, page_two, 40), \
      'the middle of bar 40, which was asked for, should be tinted'
  before = next(box['number'] for box in page_two['measures'] if box['number'] < 40)
  assert not _tinted_at(page_png, page_two, before), \
      'bar %d is on the same page but outside the passage, and must not be tinted' % before

  # And the report says what a person needs to know before trusting it.
  report = json.loads((out / 'everything' / 'report.json').read_text())
  assert report['passage'] == {'first_bar': 40, 'last_bar': 83}
  assert report['bars'] == 204
  assert 'clamped' in report['reading']
  # The report lists what the job produced, besides itself: it is written before it
  # exists, and a list that claims a file it might not have written would be worse than
  # one that stops at the others.
  assert 'measure_map.json' in {a['name'] for a in report['artifacts']}
  assert {a['kind'] for a in report['artifacts']} == everything - {'report'}

  # Progress reached the worker, and never claimed the whole of it.
  assert messages and max(pct for pct, _ in messages) <= 95
  assert [pct for pct, _ in messages] == sorted(pct for pct, _ in messages)


@have_material
def test_no_range_means_no_clip(plugin, tmp_path):
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  produced = plugin.run({}, tmp_path / 'work', tmp_path / 'out', lambda *a: None, staged)
  kinds = {artifact['kind'] for artifact in produced}
  assert 'range_audio' not in kinds, 'a clip only exists if a passage was asked for'
  assert kinds == {'page_url'}, sorted(kinds)


@have_material
def test_a_range_the_map_does_not_have_is_an_error(plugin, tmp_path):
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  with pytest.raises(NoteSeqError) as caught:
    plugin.run({'first_bar': 900, 'last_bar': 901}, tmp_path / 'work', tmp_path / 'out',
               lambda *a: None, staged)
  assert 'nothing for bars 900-901' in str(caught.value)


@have_material
def test_a_range_across_pages_highlights_every_page_it_touches(plugin, tmp_path):
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  produced = plugin.run({'first_bar': 30, 'last_bar': 40, 'include': EVERYTHING},
                        tmp_path / 'work', tmp_path / 'out', lambda *a: None, staged)
  kinds = sorted(a['kind'] for a in produced if a['kind'].startswith('score_page'))
  assert kinds == ['score_page', 'score_page_2'], \
      'bars 30 and 40 are on different pages, so two artifacts'


# --------------------------------------------------------------------------- dispatch

@have_material
@needs_reference
def test_the_reference_worker_runs_it_and_uploads_the_artifacts(plugin, tmp_path, monkeypatch):
  """Their worker's own `process_one`, with a broker that records instead of posting.

  This is the strongest check available without a live broker: staging by field name,
  the plugin call, the artifact upload loop, and the completion report are all their
  code. What is stood in for is only the HTTP client.
  """
  sys.path.insert(0, str(REFERENCE))
  try:
    import worker
  finally:
    sys.path.remove(str(REFERENCE))

  broker = FakeBroker(score=SCORE, recording=RECORDING)
  claimed = {
      'job_id': 'abcdef012345',
      'lease_token': 'token-1',
      'attempts': 1,
      'service': 'note_seq_align',
      'params': {'first_bar': 40, 'last_bar': 41},
      'inputs': ['score', 'recording'],
  }
  ok = worker.process_one(broker, plugin, claimed, lease_seconds=600, fake=False,
                          extra=[])
  assert ok, 'the job should have succeeded: %s' % broker.error
  assert broker.completed and broker.completed['success'] is True

  kinds = [upload['kind'] for upload in broker.uploads]
  assert 'range_audio' in kinds and 'page_url' in kinds, kinds
  assert 'measure_map' not in kinds, 'the workings are not what the dashboard should list'
  for upload in broker.uploads:
    assert upload['data'], '%s was uploaded empty' % upload['kind']
    assert upload['mime'] in ('application/json', 'audio/x-wav', 'audio/wav', 'image/png',
                              'text/uri-list')
  assert broker.progress_reported, 'progress should reach the broker'
  assert max(pct for pct, _ in broker.progress_reported) <= 95


@have_material
@needs_reference
def test_the_reference_worker_reports_a_failure_it_can_read(plugin, tmp_path):
  sys.path.insert(0, str(REFERENCE))
  try:
    import worker
  finally:
    sys.path.remove(str(REFERENCE))

  broker = FakeBroker(score=SCORE)                      # no recording staged
  claimed = {'job_id': 'abcdef012345', 'lease_token': 'token-1', 'attempts': 1,
             'service': 'note_seq_align', 'params': {}, 'inputs': ['score']}
  assert worker.process_one(broker, plugin, claimed, lease_seconds=600, fake=False,
                            extra=[]) is False
  assert broker.completed and broker.completed['success'] is False
  assert 'audio file or give a YouTube URL' in broker.completed['error']


# --------------------------------------------------------------------------- helpers

def _stage(workdir: pathlib.Path, **files) -> dict:
  """What the worker's `stage_input` would return: field name -> staged paths."""
  workdir.mkdir(parents=True, exist_ok=True)
  staged = {}
  for field, source in files.items():
    target = workdir / pathlib.Path(source).name
    target.write_bytes(pathlib.Path(source).read_bytes())
    staged[field] = [target]
  return staged


class FakeBroker:
  """The worker's broker, recording instead of talking to a server.

  Only the surface `worker.process_one` uses: input staging, progress, artifact upload
  and completion. `stage_input` is not called here, so the input methods are minimal.
  """

  def __init__(self, **inputs):
    self.inputs = inputs
    self.uploads = []
    self.progress_reported = []
    self.completed = None
    self.error = None

  def input_meta(self, job_id, name):
    path = pathlib.Path(self.inputs[name])
    return {'filename': path.name, 'bytes': path.stat().st_size}

  def input_data(self, job_id, name):
    return pathlib.Path(self.inputs[name]).read_bytes()

  def progress(self, job_id, token, pct, message):
    self.progress_reported.append((pct, message))

  def upload(self, job_id, token, kind, filename, data, mime):
    self.uploads.append({'kind': kind, 'filename': filename, 'data': data, 'mime': mime})

  def complete(self, job_id, token, success=True, error=None):
    self.completed = {'success': success, 'error': error}
    self.error = error

  def renew(self, job_id, token, lease_seconds):
    return True


def _tinted_at(png: pathlib.Path, page: dict, bar: int) -> bool:
  """Is the middle of `bar`'s box on this page tinted towards the highlight colour?

  The box comes from the map artifact and the scale from the page's own width against
  the image's -- the same arithmetic the browser does with percentages, done on pixels.
  Sampling the middle of a bar rather than counting blue pixels is what makes this a
  test of *where* the wash went, not just that there is one.
  """
  from PIL import Image

  box = next(b for b in page['measures'] if b['number'] == bar)
  with Image.open(str(png)) as image:
    pixels = image.convert('RGB')
    width, height = pixels.size
    factor = width / float(page['width'])
    x = int((box['x0'] + box['x1']) / 2 * factor)
    y = int((box['y0'] + box['y1']) / 2 * factor)
    red, green, blue = pixels.getpixel((max(0, min(width - 1, x)),
                                        max(0, min(height - 1, y))))
  # The wash is the highlight colour at 18% over white paper: bluer than it is red, and
  # not the paper, which is a very light warm grey.
  return blue > red + 10 and blue > 140 and blue >= green


# ---------------------------------------------------------------------------
# the page the artifacts come with
# ---------------------------------------------------------------------------

@have_material
def test_a_job_publishes_a_piece_its_own_page_can_serve(plugin, settings, tmp_path):
  """A broker job leaves more than a folder of downloads: the piece goes into the
  standalone service's store, and this is that store's API answering for it.

  `settings` points at a temporary data directory, so this exercises the real writing and
  the real reading without touching the service's own data.
  """
  from fastapi.testclient import TestClient

  from service.app.api import create_app

  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  out = tmp_path / 'out'
  produced = plugin.run({'first_bar': 40, 'last_bar': 83, 'title': 'Chopin, from the broker'},
                        tmp_path / 'work', out, lambda *a: None, staged)
  link = next(a for a in produced if a['kind'] == KIND_LINK)
  assert 'http://localhost:8100/pieces/' in pathlib.Path(link['path']).read_text()
  slug = pathlib.Path(link['path']).read_text().split('/pieces/')[1].split()[0].strip()
  assert slug == 'chopin-from-the-broker'
  assert slug == 'chopin-from-the-broker'

  # The piece is on disk, and the API serves it exactly as it serves its own.
  assert (settings.piece_dir(slug) / 'map.json').is_file()
  assert any((settings.piece_dir(slug) / name).is_file()
             for name in ('audio.flac', 'audio.wav'))
  assert len(list((settings.piece_dir(slug) / 'pages').glob('*.svg'))) == 9

  client = TestClient(create_app(settings))
  piece = client.get('/api/v1/pieces/%s' % slug).json()
  assert piece['meta']['title'] == 'Chopin, from the broker'
  assert len(piece['map']['measures']) == 204
  assert len(piece['geometry']['pages']) == 9
  bar40 = next(m for m in piece['map']['measures'] if m['measure'] == 40)
  assert bar40['start_s'] == pytest.approx(62.38, abs=0.1)

  page = client.get('/api/v1/pieces/%s/pages/1.svg' % slug)
  assert page.status_code == 200 and 'class="measure">' in page.text

  clip = client.get('/api/v1/pieces/%s/audio' % slug, params={'first_bar': 40, 'last_bar': 83})
  assert clip.status_code == 200
  assert len(clip.content) / (22050 * 2.0) == pytest.approx(60.98, abs=0.05)
  assert client.get('/api/v1/pieces').json()['pieces'][0]['slug'] == slug


@have_material
def test_the_piece_belongs_to_whoever_asked_for_the_job(plugin, settings, tmp_path,
                                                       monkeypatch):
  """`owner_id` in the params is this side of the ownership contract, end to end.

  The broker's worker puts the submitting account into the params it hands a plugin (that
  is the patch in `service/broker/patches/owner-through-the-worker.patch`), the plugin
  writes it into the piece, and the page then shows that piece to that account and to
  nobody else. Verified the only way that means anything: the real plugin, a real
  analysis, and the API asked as two different accounts.
  """
  import json
  import time

  import jwt
  from fastapi.testclient import TestClient

  from service.app.api import create_app
  from service.app.config import Settings

  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  plugin.run({'first_bar': 40, 'last_bar': 41, 'title': 'Owned by the asker',
              'owner_id': 'user-42'},
             tmp_path / 'work', tmp_path / 'out', lambda *a: None, staged)

  slug = 'owned-by-the-asker'
  meta = json.loads((settings.piece_dir(slug) / 'meta.json').read_text(encoding='utf-8'))
  assert meta['owner'] == 'user-42'

  monkeypatch.setenv('SERVICE_TRUST_JWT', 'the-brokers-key')
  monkeypatch.setenv('SERVICE_ALLOWED_SUBJECTS', '')
  client = TestClient(create_app(Settings(data_dir=settings.data_dir)))

  def bearer(subject):
    signed = jwt.encode({'sub': subject, 'exp': int(time.time()) + 600},
                        'the-brokers-key', algorithm='HS256')
    return {'Authorization': 'Bearer %s' % signed}

  theirs = client.get('/api/v1/pieces', headers=bearer('user-42'))
  assert [row['slug'] for row in theirs.json()['pieces']] == [slug]
  assert client.get('/api/v1/pieces/%s' % slug, headers=bearer('user-42')).status_code == 200

  somebody_else = client.get('/api/v1/pieces', headers=bearer('user-7'))
  assert somebody_else.json()['pieces'] == []
  assert client.get('/api/v1/pieces/%s' % slug, headers=bearer('user-7')).status_code == 404
  assert client.get('/api/v1/pieces/%s/audio' % slug,
                    headers=bearer('user-7')).status_code == 404


@have_material
def test_the_switch_turns_the_page_off_and_the_job_still_works(plugin, settings, tmp_path):
  """A machine that only runs broker workers should be able to say no."""
  import note_seq_align

  off = note_seq_align.plugin(settings=settings, viewer=False)
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  out = tmp_path / 'out'
  produced = off.run({'first_bar': 40, 'last_bar': 41}, tmp_path / 'work', out,
                     lambda *a: None, staged)
  assert {a['kind'] for a in produced} == {'range_audio'}
  assert not list(settings.pieces_dir.glob('*')), 'nothing was published'
  assert not (out / 'report.json').exists(), \
      'switching it off is a setting, not a fault, so nothing needs explaining'
  assert not (out / 'open-the-page.txt').exists(), 'and there is no link to hand out'


@have_material
def test_publishing_that_fails_does_not_fail_the_job(plugin, settings, tmp_path, monkeypatch):
  """The artifacts are the contract; the page is a bonus. A read-only data directory
  must cost you the link, not the job."""
  from service.app import pieces as pieces_module

  def explode(*args, **kwargs):
    raise OSError('read-only file system')

  monkeypatch.setattr(pieces_module.PieceStore, 'write', explode)
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  out = tmp_path / 'out'
  produced = plugin.run({}, tmp_path / 'work', out, lambda *a: None, staged)
  report = json.loads((out / 'report.json').read_text())
  assert report['viewer']['published'] is False
  assert 'read-only file system' in report['viewer']['reason']
  assert {a['kind'] for a in produced} == {'report'}, \
      'the report is the only honest thing left, and a job must not return nothing'
  for artifact in produced:
    assert pathlib.Path(artifact['path']).is_file()


def test_the_viewer_url_can_be_configured(monkeypatch, tmp_path):
  """The worker and the page need not be on the same machine."""
  import importlib

  monkeypatch.setenv('NOTE_SEQ_VIEWER_URL', 'http://piano.local:9000/')
  import note_seq_align.service as service_module
  reloaded = importlib.reload(service_module)
  try:
    made = reloaded.plugin()
    assert made.viewer_url == 'http://piano.local:9000'
  finally:
    monkeypatch.undo()
    importlib.reload(service_module)


# ---------------------------------------------------------------------------
# the schema has to be publishable by the OLDEST broker we talk to
# ---------------------------------------------------------------------------

# What a broker that predates `accept` accepts, copied verbatim from the 422 it answered:
#
#   unsupported param_schema: root/properties/score: unsupported schema keyword 'accept'.
#   Supported: additionalProperties, allOf, anyOf, const, default, description, enum,
#   exclusiveMaximum, exclusiveMinimum, format, items, maxItems, maxLength, maximum,
#   minItems, minLength, minimum, oneOf, pattern, properties, required, title, type
#
# The local checkout's validator allows `accept` too (it is a rendering hint for the file
# picker), which is exactly the trap: a schema that registers against the checkout fails
# to register against a deployment running the older revision. So this list is the one
# that matters, and a keyword outside it is a bug however new the local broker is.
OLDEST_BROKER_KEYWORDS = {
    'additionalProperties', 'allOf', 'anyOf', 'const', 'default', 'description', 'enum',
    'exclusiveMaximum', 'exclusiveMinimum', 'format', 'items', 'maxItems', 'maxLength',
    'maximum', 'minItems', 'minLength', 'minimum', 'oneOf', 'pattern', 'properties',
    'required', 'title', 'type',
}


def _keywords(schema, path='root'):
  """Every keyword in a schema, at every depth, with where it was found."""
  found = []
  if not isinstance(schema, dict):
    return found
  for key, value in schema.items():
    found.append((key, path))
    if key == 'properties' and isinstance(value, dict):
      for name, sub in value.items():
        found.extend(_keywords(sub, '%s/properties/%s' % (path, name)))
    elif key == 'items':
      found.extend(_keywords(value, '%s/items' % path))
    elif key in ('anyOf', 'oneOf', 'allOf') and isinstance(value, list):
      for index, sub in enumerate(value):
        found.extend(_keywords(sub, '%s/%s/%d' % (path, key, index)))
  return found


def test_the_schema_only_uses_keywords_an_older_broker_accepts(plugin):
  """A keyword the broker cannot enforce makes it refuse the whole contract at
  registration -- which is a worker that publishes nothing and claims nothing."""
  offenders = [(key, where) for key, where in _keywords(plugin.param_schema)
               if key not in OLDEST_BROKER_KEYWORDS]
  assert not offenders, 'the broker would refuse this schema: %s' % offenders


def test_the_file_fields_say_what_they_take_in_their_description(plugin):
  """Since `accept` is unavailable, the formats have to be in the description -- which
  every broker renders as help text and none rejects."""
  fields = plugin.param_schema['properties']
  assert '.musicxml' in fields['score']['description']
  assert 'wav' in fields['recording']['description']
  assert fields['score']['format'] == 'files'
  assert fields['recording']['format'] == 'files'
