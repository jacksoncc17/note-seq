"""Tests for the streaming follower.

What is asserted here is what was actually measured, and the limits are in the tests
rather than only in the prose:

* it follows a **rendered** score at any tempo -- 0.7x and 1.3x as well as 1.0x -- to
  within a fraction of a beat, which is the whole reason for following instead of using a
  map;
* it holds its place through a pause, does not sprint through unrelated audio, and costs a
  fraction of the time it has between frames;
* and it does **not** follow a real piano recording against a *rendered* reference -- the
  chroma of a real instrument and of a rendering differ by more per frame than the
  positions differ from each other, so the cost landscape goes flat and the path crawls.
  That is the honest state of it, and `test_a_real_recording_is_not_followed_against_a_
  rendering` pins it so the day it is fixed, somebody notices.
"""

import pathlib
import time

import pytest

from analyzer import follow, library
from analyzer.config import AnalyzerConfig

SCORE = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')
SLUG = 'chopin-waltz-in-a-minor-op-34-no-2'
needs_real = pytest.mark.skipif(
    not (SCORE.is_file() and AnalyzerConfig().sync_path(SLUG).is_file()),
    reason='the Chopin score, its map and its practice track are needed')


@pytest.fixture(scope='module')
def real():
  cfg = AnalyzerConfig()
  piece = library.find(cfg, SLUG)
  chroma, grid, path = follow.reference_for(cfg, piece)
  return cfg, piece, chroma, grid, path


def _audio(path):
  import soundfile as sf
  data, rate = sf.read(str(path), dtype='float32')
  if getattr(data, 'ndim', 1) > 1:
    data = data.mean(axis=1)
  return data, rate


def _stretched(data, rate, factor, seconds=40.0):
  """A performance at `factor` times the reference's tempo, pitch unchanged."""
  import librosa
  import soundfile as sf
  import tempfile
  played = librosa.effects.time_stretch(data[:int(seconds * rate)], rate=factor)
  path = pathlib.Path(tempfile.mkdtemp(prefix='follow-')) / ('at-%.2f.wav' % factor)
  sf.write(str(path), played, rate)
  return path


# ---------------------------------------------------------------------------
# the reference
# ---------------------------------------------------------------------------

@needs_real
def test_the_reference_is_the_score_rendered_as_audio(real):
  cfg, piece, chroma, grid, path = real
  assert chroma.shape[0] == 12
  assert grid['seconds_per_bar'] > 1.0 and grid['beats'] == 3
  assert pathlib.Path(path).is_file(), 'the practice track is what gets followed'
  expected = pathlib.Path(path).stat().st_size / 4.0 / follow.SAMPLE_RATE
  frames = chroma.shape[1] * follow.HOP / follow.SAMPLE_RATE
  assert abs(frames - expected) < 30.0, 'the chroma should cover the track'


@needs_real
def test_the_reference_chroma_is_cached_on_disk(real):
  cfg, piece, _chroma, _grid, _path = real
  start = time.perf_counter()
  follow.reference_for(cfg, piece)
  assert time.perf_counter() - start < 0.2, 'the second call should not recompute anything'


def test_a_reference_without_a_bar_grid_is_refused():
  import numpy as np
  with pytest.raises(follow.FollowError):
    follow.OnlineFollower(np.zeros((12, 100), dtype='float32'), {})


def test_an_unknown_feature_is_refused(real):
  import numpy as np
  cfg, _piece, _chroma, grid, _path = real
  with pytest.raises(follow.FollowError):
    follow.OnlineFollower(np.zeros((12, 100), dtype='float32'), grid, feature='magic')


# ---------------------------------------------------------------------------
# following
# ---------------------------------------------------------------------------

@needs_real
def test_it_follows_a_rendering_at_the_reference_tempo(real):
  cfg, _piece, chroma, grid, path = real
  follower = follow.OnlineFollower(chroma, grid)
  positions = follow.follow_file(follower, path, duration=40.0)
  seconds = len(positions) * follow.HOP / follow.SAMPLE_RATE
  assert abs(positions[-1].reference_s - seconds) < 0.2, \
      'it should be where the music is, to within a couple of frames'
  assert positions[-1].bar == 1 + int(seconds / grid['seconds_per_bar'])


@needs_real
def test_it_follows_a_slower_and_a_faster_rendering(real):
  """The point of following rather than reading a map: the tempo is not assumed."""
  cfg, _piece, chroma, grid, path = real
  data, rate = _audio(path)
  for factor in (0.7, 1.3):
    follower = follow.OnlineFollower(chroma, grid)
    positions = follow.follow_file(follower, _stretched(data, rate, factor))
    seconds = len(positions) * follow.HOP / follow.SAMPLE_RATE
    reached = positions[-1].reference_s
    assert abs(reached - factor * seconds) < 0.5, \
        'at %.1fx after %.0f s of audio it should be at %.1f s of reference, was %.1f' \
        % (factor, seconds, factor * seconds, reached)


@needs_real
def test_it_holds_its_place_through_a_pause(real):
  import numpy as np
  cfg, _piece, chroma, grid, path = real
  data, rate = _audio(path)
  follower = follow.OnlineFollower(chroma, grid)
  follow.follow_file(follower, path, duration=10.0)
  before = follower.position()
  silence = np.zeros(int(3.0 * rate), dtype='float32')
  position = follower.feed(silence)
  assert position is not None
  assert abs(position.reference_s - before.reference_s) < 2.0 * grid['seconds_per_bar'], \
      'three seconds of silence must not carry the page three seconds forward'
  follow.follow_file(follower, path, duration=10.0)
  assert follower.position().reference_s > before.reference_s + 5.0, \
      'and playing on must carry on from where it was'


@needs_real
def test_unrelated_audio_does_not_sprint_through_the_piece(real):
  """Noise is not the piece, so the page must not race away with it."""
  import numpy as np
  cfg, _piece, chroma, grid, _path = real
  noise = np.random.default_rng(7).normal(0, 0.05, int(20.0 * follow.SAMPLE_RATE))
  follower = follow.OnlineFollower(chroma, grid)
  positions = follow.follow_file(follower, _wav(noise))
  seconds = len(positions) * follow.HOP / follow.SAMPLE_RATE
  assert follower.position().reference_s < seconds * 1.5, \
      'noise must not advance the position faster than time itself'
  # what it *reports* is weaker than one would like, and that is worth knowing: chroma
  # vectors are non-negative and broadly similar in shape, so a flat spectrum (noise) still
  # has a high cosine similarity to a chord. `quality` is not a reliable lost-signal.
  assert np.median([p.quality for p in positions]) > 0.5, \
      'noise still looks like a decent match to this measure -- documented, not a bug fix'


@needs_real
def test_silence_is_reported_as_not_knowing(real):
  """Silence is the case the quality measure *does* get right."""
  import numpy as np
  cfg, _piece, chroma, grid, _path = real
  follower = follow.OnlineFollower(chroma, grid)
  positions = follow.follow_file(follower, _wav(np.zeros(int(10.0 * follow.SAMPLE_RATE))))
  assert positions, 'it should still emit a position'
  assert all(p.unsure for p in positions), 'silence is not the piece'
  assert np.median([p.quality for p in positions]) < 0.1


def _wav(samples, rate=follow.SAMPLE_RATE):
  import soundfile as sf
  import tempfile
  path = pathlib.Path(tempfile.mkdtemp(prefix='follow-')) / 'audio.wav'
  sf.write(str(path), samples.astype('float32'), rate)
  return path


@needs_real
def test_starting_from_the_middle_of_the_piece(real):
  """Somebody practising bars 40-43 does not start at bar 1, and the band only extends
  forwards from wherever it is told to look."""
  cfg, _piece, chroma, grid, path = real
  data, rate = _audio(path)
  start_s = 60.0
  follower = follow.OnlineFollower(chroma, grid)
  follower.reset(reference_s=start_s)
  positions = follow.follow_file(follower, _wav(data[int(start_s * rate):int((start_s + 20) * rate)]))
  got = positions[-1].reference_s
  assert abs(got - (start_s + 20.0)) < 0.5, 'expected about %.0f s, got %.1f' % (start_s + 20.0, got)


@needs_real
def test_a_frame_costs_a_fraction_of_the_time_between_frames(real):
  cfg, _piece, chroma, grid, path = real
  follower = follow.OnlineFollower(chroma, grid)
  data, rate = _audio(path)
  block = data[:int(20.0 * rate)]
  start = time.perf_counter()
  positions = follow.follow_file(follower, _wav(block))
  elapsed = time.perf_counter() - start
  budget = follow.HOP / follow.SAMPLE_RATE
  per_frame = elapsed / max(1, len(positions))
  assert per_frame < budget / 4.0, \
      '%.2f ms a frame against a %.0f ms budget is too close for a live signal' \
      % (per_frame * 1000, budget * 1000)


# ---------------------------------------------------------------------------
# the limit that was measured, kept as a test so that fixing it is noticed
# ---------------------------------------------------------------------------

@needs_real
def test_a_real_recording_is_not_followed_against_a_rendering(real):
  """A real piano matched against a *rendered* score does not work with these features.

  Per frame, the chroma of a real recording sits about 0.017 away from the rendering of
  the same music at the same moment (a rendering against itself is 0.000) -- larger than
  the difference between being in the right place and the wrong one, so the cost landscape
  flattens and the path crawls: measured at 49 s of reference reached in 309 s of audio.
  The follower is not at fault and no penalty fixes it: a big enough step penalty makes it
  walk the reference at the reference's tempo instead, which tracks a *tempo* not at all.

  Matching a real instrument needs a real reference -- the piece's own recording -- which
  means the bar timeline comes from the map rather than from a uniform grid, and that is
  the next piece of work rather than something this test can assert today.
  """
  cfg, piece, chroma, grid, _path = real
  follower = follow.OnlineFollower(chroma, grid)
  positions = follow.follow_file(follower, cfg.audio_path(piece.slug))
  audio_s = len(positions) * follow.HOP / follow.SAMPLE_RATE
  reached = follower.position().reference_s
  assert reached < 0.5 * audio_s, \
      'if this now tracks the recording, the limitation is gone and this test should be ' \
      'replaced by one asserting that it does'
