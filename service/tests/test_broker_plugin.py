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

from note_seq_align.service import NoteSeqError

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
def plugin():
  """The plugin as the worker would get it, loaded from its directory by name."""
  sys.path.insert(0, str(PLUGIN_DIR))
  try:
    import importlib
    module = importlib.import_module('note_seq_align')
    return module.plugin()
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
  assert set(plugin.artifact_kinds) == {'measure_map', 'range_audio', 'score_page',
                                        'report'}
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


def test_the_fake_run_needs_no_music_at_all(plugin, tmp_path):
  """`--fake-pipeline` is how the queue plumbing is tested; it must not touch audio."""
  produced = plugin.fake_run({}, tmp_path, tmp_path / 'out', lambda *a: None, {})
  kinds = {artifact['kind'] for artifact in produced}
  assert kinds == {'measure_map', 'range_audio', 'score_page', 'report'}
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
  # Bars 40-83 span three engraved pages, so three page artifacts: the broker keys an
  # artifact by (job, kind), and a passage that crosses a page turn has to be visible on
  # both sides of it.
  page_kinds = sorted(kind for kind in by_kind if kind.startswith('score_page'))
  assert page_kinds == ['score_page', 'score_page_2', 'score_page_3'], page_kinds
  assert set(by_kind) == {'measure_map', 'range_audio', 'report', *page_kinds}
  assert all(path.is_file() and path.stat().st_size > 0 for path in by_kind.values())

  # The map: the analyzer's own numbers, which the desktop app also produces.
  payload = json.loads(by_kind['measure_map'].read_text())
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
  image = by_kind['score_page'].read_bytes()
  assert image[:8] == b'\x89PNG\r\n\x1a\n'
  page_two = next(page for page in payload['pages'] if page['number'] == 2)
  assert _tinted_at(by_kind['score_page'], page_two, 40), \
      'the middle of bar 40, which was asked for, should be tinted'
  before = next(box['number'] for box in page_two['measures'] if box['number'] < 40)
  assert not _tinted_at(by_kind['score_page'], page_two, before), \
      'bar %d is on the same page but outside the passage, and must not be tinted' % before

  # And the report says what a person needs to know before trusting it.
  report = json.loads(by_kind['report'].read_text())
  assert report['passage'] == {'first_bar': 40, 'last_bar': 83}
  assert report['bars'] == 204
  assert 'clamped' in report['reading']
  # The report lists what the job produced, besides itself: it is written before it
  # exists, and a list that claims a file it might not have written would be worse than
  # one that stops at the others.
  assert {a['kind'] for a in report['artifacts']} == set(by_kind) - {'report'}
  assert 'measure_map.json' in {a['name'] for a in report['artifacts']}

  # Progress reached the worker, and never claimed the whole of it.
  assert messages and max(pct for pct, _ in messages) <= 95
  assert [pct for pct, _ in messages] == sorted(pct for pct, _ in messages)


@have_material
def test_no_range_means_no_clip(plugin, tmp_path):
  staged = _stage(tmp_path, score=SCORE, recording=RECORDING)
  produced = plugin.run({}, tmp_path / 'work', tmp_path / 'out', lambda *a: None, staged)
  kinds = {artifact['kind'] for artifact in produced}
  assert 'range_audio' not in kinds, 'a clip only exists if a passage was asked for'
  assert {'measure_map', 'score_page', 'report'} <= kinds


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
  produced = plugin.run({'first_bar': 30, 'last_bar': 40}, tmp_path / 'work',
                        tmp_path / 'out', lambda *a: None, staged)
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
  assert 'measure_map' in kinds and 'range_audio' in kinds and 'score_page' in kinds
  for upload in broker.uploads:
    assert upload['data'], '%s was uploaded empty' % upload['kind']
    assert upload['mime'] in ('application/json', 'audio/x-wav', 'audio/wav', 'image/png')
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
