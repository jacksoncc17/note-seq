"""The analysis and what it stores, on real material.

The important test in here is `test_the_map_is_the_one_the_desktop_app_makes`: the
service runs the same alignment the desktop app runs, so its measure map must agree with
the map that is already on this machine for the same score and recording. If that ever
diverges, the two halves of this repository have drifted apart -- and that is exactly the
kind of thing a green test suite made of stubs would never notice.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from conftest import RECORDING, SCORE, have_material

MAP_ON_DISK = (pathlib.Path(__file__).resolve().parents[2] / 'analyzer' / 'output' / 'sync'
               / 'chopin-waltz-in-a-minor-op-34-no-2.json')


# --------------------------------------------------------------------------- the steps

@have_material
def test_the_recording_is_converted_to_the_one_format_everything_assumes(settings, tmp_path):
  from service.app.pipeline import audio_path, read_audio_meta

  job_dir = tmp_path / 'job'
  job_dir.mkdir()
  saved = job_dir / 'upload.wav'
  saved.write_bytes(RECORDING.read_bytes())
  messages = []

  def report(percent, message=''):
    messages.append((percent, message))

  wav = audio_path(settings, job_dir, str(saved), '', object(), report, lambda line: None)
  meta = read_audio_meta(wav)
  assert meta['sample_rate'] == 22050, 'the analyzer aligns at 22050 Hz and only that'
  assert meta['channels'] == 1
  assert meta['duration_s'] == pytest.approx(309.7, abs=0.5)
  assert messages[-1][0] == 100.0, 'the step reports completion'


def test_an_upload_that_is_not_audio_fails_with_a_readable_message(settings, tmp_path):
  from service.app.pipeline import PipelineError, audio_path

  job_dir = tmp_path / 'job'
  job_dir.mkdir()
  junk = job_dir / 'upload.mp3'
  junk.write_bytes(b'this is not an mp3, whatever the extension says')
  with pytest.raises(PipelineError) as caught:
    audio_path(settings, job_dir, str(junk), '', object(), lambda *a: None, lambda line: None)
  assert 'could not read that audio file' in str(caught.value)


@have_material
def test_the_reference_midi_comes_from_the_script_the_readme_names(settings, tmp_path):
  """`memorization/analyze.py --midi-only`, with the checkout's own interpreter."""
  from analyzer import align

  from service.app.pipeline import build_reference_midi

  job_dir = tmp_path / 'job'
  job_dir.mkdir()
  steady = build_reference_midi(settings, SCORE, job_dir, lambda *a: None, lambda line: None)
  assert steady.is_file() and steady.name.endswith('_steady.mid')
  seconds_per_bar, beats, qpm = align.midi_grid(steady)
  assert qpm == pytest.approx(120.28, abs=0.01), 'what the desktop app gets for this score'
  assert beats == 3, 'a waltz'
  assert seconds_per_bar == pytest.approx(1.4965, abs=0.001)


def test_the_score_says_its_own_title_and_composer():
  from service.app.pipeline import read_score_meta

  if not SCORE.is_file():
    pytest.skip('the Chopin score is needed')
  meta = read_score_meta(SCORE)
  assert meta['composer'] == 'Frédéric Chopin'
  assert 'Waltz' in meta['title']


def test_a_score_that_says_nothing_is_not_a_crash(tmp_path):
  from service.app.pipeline import read_score_meta

  bare = tmp_path / 'bare.musicxml'
  bare.write_text('<?xml version="1.0"?><score-partwise version="4.0"></score-partwise>')
  assert read_score_meta(bare) == {}


# --------------------------------------------------------------------------- the piece

@have_material
def test_a_job_runs_and_stores_a_piece_on_disk(analyzed):
  settings, slug = analyzed['settings'], analyzed['slug']
  assert slug == 'chopin-waltz-in-a-minor', 'the title decides the slug'
  directory = settings.piece_dir(slug)
  for name in ('score.musicxml', 'score.mid', 'audio.wav', 'map.json', 'meta.json',
               'geometry.json'):
    assert (directory / name).is_file(), name
  assert len(list((directory / 'pages').glob('*.svg'))) == 9

  meta = json.loads((directory / 'meta.json').read_text())
  assert meta['bars'] == 204
  assert meta['method'] == 'dtw'
  assert meta['duration_s'] == pytest.approx(309.7, abs=0.5)
  assert meta['audio']['source'] == 'upload'
  assert meta['score']['sha256'], 'the upload is fingerprinted'


@have_material
def test_the_map_is_the_one_the_desktop_app_makes(analyzed):
  """The same alignment, so the same map: bar 40 at 62.38 s, not "about a minute"."""
  slug = analyzed['slug']
  ours = analyzed['client'].get('/api/v1/pieces/%s' % slug).json()['map']
  assert MAP_ON_DISK.is_file(), 'the desktop app has a map for this piece to compare with'
  stored = json.loads(MAP_ON_DISK.read_text())       # the sidecar: a SyncResult plus provenance

  theirs = {m['measure']: m for m in stored['measures']}
  assert len(theirs) == len(ours['measures']), 'the two maps cover different bars'
  worst = 0.0
  for measure in ours['measures']:
    other = theirs.get(measure['measure'])
    assert other is not None, 'a bar in the service map is missing from the desktop map'
    worst = max(worst, abs(measure['start_s'] - other['start_s']),
                abs(measure['end_s'] - other['end_s']))
  # Measured at 0.000 s on all 204 bars: the same code on the same input, so anything
  # above a frame or two here means the two halves of the repository have drifted apart.
  assert worst < 0.02, 'the two maps differ by up to %.3f s' % worst

  bar40 = next(m for m in ours['measures'] if m['measure'] == 40)
  assert bar40['start_s'] == pytest.approx(62.38, abs=0.1)
  assert ours['quality']['normalized_cost'] == pytest.approx(
      stored['quality']['normalized_cost'], abs=0.001)
  assert ours['method'] == 'dtw'


@have_material
def test_every_bar_on_every_page_can_be_clicked(analyzed):
  slug = analyzed['slug']
  geometry = analyzed['client'].get('/api/v1/pieces/%s' % slug).json()['geometry']
  assert len(geometry['pages']) == 9
  numbers = [box['number'] for page in geometry['pages'] for box in page['measures']]
  assert sorted(numbers) == list(range(1, 205)), 'every bar, exactly once'
  for page in geometry['pages']:
    assert page['width'] > 0 and page['height'] > 0
    for box in page['measures']:
      assert box['x1'] > box['x0'] and box['y1'] > box['y0'], 'a box to click'
      assert 0 <= box['x0'] < page['width'], 'the box is inside the page it is on'


@have_material
def test_a_page_is_served_as_the_engraving_verovio_wrote(analyzed):
  slug = analyzed['slug']
  page = analyzed['client'].get('/api/v1/pieces/%s/pages/1.svg' % slug)
  assert page.status_code == 200
  assert page.headers['content-type'].startswith('image/svg+xml')
  assert page.text.startswith('<svg')
  assert page.text.count('class="measure">') == 31, 'the measure groups are in the document'
  assert 'viewBox="0 0 21000' in page.text
  assert analyzed['client'].get('/api/v1/pieces/%s/pages/99.svg' % slug).status_code == 404


# --------------------------------------------------------------------------- the clip

@have_material
def test_the_clip_for_a_range_of_bars_is_the_music_of_those_bars(analyzed):
  """The feature the service exists for: bars in, the sound of those bars out."""
  slug, client = analyzed['slug'], analyzed['client']
  piece = client.get('/api/v1/pieces/%s' % slug).json()
  measures = {m['measure']: m for m in piece['map']['measures']}
  first, last = 40, 83
  music = measures[last]['end_s'] - measures[first]['start_s']

  clip = client.get('/api/v1/pieces/%s/audio' % slug,
                    params={'first_bar': first, 'last_bar': last})
  assert clip.status_code == 200
  assert clip.headers['content-type'] == 'audio/wav'
  seconds = len(clip.content) / (22050 * 2.0)          # 16-bit mono at 22050 Hz
  assert seconds == pytest.approx(music + 0.37, abs=0.05), \
      'the clip is the passage plus the 0.12 s run-in and 0.25 s run-out'
  assert float(clip.headers['X-Clip-Start']) == pytest.approx(measures[first]['start_s'] - 0.12,
                                                             abs=0.01)
  assert float(clip.headers['X-Clip-End']) == pytest.approx(measures[last]['end_s'] + 0.25,
                                                            abs=0.01)
  # And it really is that part of the recording, sample for sample.
  import soundfile as sf
  whole = sf.read(str(analyzed['settings'].piece_dir(slug) / 'audio.wav'), dtype='float32')[0]
  start = int(round(float(clip.headers['X-Clip-Start']) * 22050))
  import io
  piece_of_clip = sf.read(io.BytesIO(clip.content), dtype='float32')[0]
  assert abs(float(whole[start + 22050]) - float(piece_of_clip[22050])) < 1e-4


@have_material
def test_a_single_bar_can_be_asked_for(analyzed):
  slug, client = analyzed['slug'], analyzed['client']
  clip = client.get('/api/v1/pieces/%s/audio' % slug, params={'first_bar': 40, 'last_bar': 40})
  assert clip.status_code == 200
  piece = client.get('/api/v1/pieces/%s' % slug).json()
  bar = next(m for m in piece['map']['measures'] if m['measure'] == 40)
  seconds = len(clip.content) / (22050 * 2.0)
  assert seconds == pytest.approx(bar['end_s'] - bar['start_s'] + 0.37, abs=0.05)
  assert seconds < 5, 'one bar of a waltz is a couple of seconds'


@have_material
def test_the_whole_recording_can_be_streamed_as_well(analyzed):
  slug, client = analyzed['slug'], analyzed['client']
  response = client.get('/api/v1/pieces/%s/audio' % slug)
  assert response.status_code == 200
  assert len(response.content) == pytest.approx(309.7 * 22050 * 2, rel=0.01)


@have_material
def test_a_range_that_is_not_in_the_map_is_refused(analyzed):
  slug, client = analyzed['slug'], analyzed['client']
  assert client.get('/api/v1/pieces/%s/audio' % slug,
                    params={'first_bar': 900, 'last_bar': 901}).status_code == 404
  assert client.get('/api/v1/pieces/%s/audio' % slug,
                    params={'first_bar': 40}).status_code == 400
  assert client.get('/api/v1/pieces/%s/audio' % slug,
                    params={'start': 30, 'end': 20}).status_code == 400


@have_material
def test_the_bars_to_seconds_rule_is_the_desktop_apps(analyzed):
  """`range_for_bars` is the analyzer's `range_for_measures`, fed from the stored map."""
  from analyzer.schema import MeasureTime, SyncResult
  from service.app.api import range_for_bars

  slug, client = analyzed['slug'], analyzed['client']
  map_dict = client.get('/api/v1/pieces/%s' % slug).json()['map']
  ours = range_for_bars(map_dict, 40, 83)

  result = SyncResult(method=map_dict['method'],
                      measures=[MeasureTime(measure=m['measure'], play_index=m['play_index'],
                                            start_s=m['start_s'], end_s=m['end_s'])
                                for m in map_dict['measures']])
  assert ours == result.range_for_measures(40, 83)
  assert ours == pytest.approx((62.38, 122.99), abs=0.1)
  assert range_for_bars({'measures': []}, 1, 2) is None


# --------------------------------------------------------------------------- psalms

def test_a_piece_with_no_audio_to_align_is_refused(settings, tmp_path):
  """A score that parses but has no measures must fail with a message, not a traceback."""
  from service.app.pipeline import PipelineError, analyze

  empty = tmp_path / 'empty.musicxml'
  empty.write_text('<?xml version="1.0"?><score-partwise version="4.0">'
                   '<part-list><score-part id="P1"><part-name>P</part-name>'
                   '</score-part></part-list><part id="P1"></part></score-partwise>')
  junk = tmp_path / 'audio.wav'
  junk.write_bytes(b'not audio')
  with pytest.raises(PipelineError):
    analyze(settings, tmp_path, {'score': str(empty), 'audio': str(junk)}, lambda *a: None,
            lambda line: None)
