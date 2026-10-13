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
  assert grid.seconds_per_bar > 1.0
  assert grid.beats_per_bar == 3, 'a waltz: the grid says three beats to the bar'
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
  assert positions[-1].bar == 1 + int(seconds / grid.seconds_per_bar)


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
  assert abs(position.reference_s - before.reference_s) < 2.0 * grid.seconds_per_bar, \
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
# a real reference: the recording, with the bar timeline from the map
# ---------------------------------------------------------------------------

@needs_real
def test_the_bar_grid_can_come_from_the_measure_map(real):
  """A performance's bars are as uneven as the player made them, so the grid is the
  map's own spans rather than one length per bar."""
  cfg, piece, _chroma, _grid, _path = real
  grid = follow.FollowGrid.from_map(piece.sync.result, beats=3)
  measures = piece.sync.result.measures
  assert len(grid) == len(measures)
  assert not grid.uniform, 'a recording is not played at a metronome'
  assert grid.first_bar == min(m.measure for m in measures)
  assert grid.start_of(grid.first_bar) == pytest.approx(measures[0].start_s)
  for measure in measures[:5]:
    bar, _beat, start, end = grid.bar_at(measure.start_s + 0.001)
    assert bar == measure.measure
    assert (start, end) == pytest.approx((measure.start_s, measure.end_s))
  assert grid.bar_at(-5.0)[0] == grid.first_bar, 'before the music: the first bar'
  assert grid.bar_at(1e6)[0] == max(m.measure for m in measures), 'after it: the last'


def test_an_even_grid_needs_a_length_per_bar():
  grid = follow.FollowGrid.evenly(1.5, first_bar=1, bars=4, beats=3)
  assert len(grid) == 4 and grid.seconds_per_bar == pytest.approx(1.5)
  assert grid.bar_at(1.55)[:2] == (2, 1), 'just inside bar 2 is its first beat'
  assert grid.bar_at(2.05)[:2] == (2, 2), 'a third of the way in is beat 2 of 3'
  assert grid.bar_at(0.0)[0] == 1
  with pytest.raises(follow.FollowError):
    follow.FollowGrid.evenly(0.0)


@needs_real
def test_the_recording_can_be_the_reference(real):
  """What a microphone needs: the piece as a real piano played it, not as we render it."""
  cfg, piece, _chroma, _grid, _path = real
  chroma, grid, path = follow.reference_for(cfg, piece, source='recording')
  assert pathlib.Path(path) == cfg.audio_path(piece.slug), 'the recording is the reference'
  assert chroma.shape[1] > 1000
  assert not grid.uniform and len(grid) == len(piece.sync.result.measures)

  start = time.perf_counter()
  again, grid_again, _path_again = follow.reference_for(cfg, piece, source='recording')
  assert time.perf_counter() - start < 0.2, 'the reference is cached on disk'
  assert again.shape == chroma.shape and len(grid_again) == len(grid)

  track, track_grid, track_path = follow.reference_for(cfg, piece, source='track')
  assert pathlib.Path(track_path) != pathlib.Path(path), 'two references, two files'
  assert track.shape != chroma.shape or track_path != path
  assert track_grid.uniform and not grid.uniform, 'the track is even, the recording is not'


@needs_real
def test_it_follows_a_recording_against_its_own_map(real):
  """Self-follow, which is the only way to check here that the map and the follower
  agree: the recording is played to the follower and its position compared with the
  map's own timeline.

  Measured: reached 309.4 s of 309.5 s of audio; the position is within 0.09 s of where
  the audio is (median -0.09 s), which lands the bar exactly 93.9% of frames and within
  one bar 100% of them. The 6% that are off by a frame or two are the frames sitting on
  a bar line, where a tenth of a second is the difference between two bars -- worth
  knowing before reading the bar number as exact.
  """
  import numpy as np
  cfg, piece, _chroma, _grid, _path = real
  chroma, grid, path = follow.reference_for(cfg, piece, source='recording')
  follower = follow.OnlineFollower(chroma, grid)
  positions = follow.follow_file(follower, path)
  audio_s = len(positions) * follow.HOP / follow.SAMPLE_RATE
  assert len(positions) > 6000

  errors, exact, within = [], 0, 0
  for index, position in enumerate(positions, start=1):
    want = index * follow.HOP / follow.SAMPLE_RATE
    bar = grid.bar_at(want)[0]
    exact += position.bar == bar
    within += abs(position.bar - bar) <= 1
    errors.append(position.reference_s - want)
  errors = np.array(errors)
  frames = len(positions)
  assert exact / frames > 0.90, 'only %.1f%% of bars were exact' % (100 * exact / frames)
  assert within == frames, 'every bar should be right or one out, %d were not' % (frames - within)
  assert abs(errors[-1]) < 1.0, 'it should end where the audio ends'
  assert np.abs(errors).max() < 0.5, 'worst lag %.2f s' % np.abs(errors).max()
  assert np.median([p.quality for p in positions]) > 0.9


@needs_real
def test_a_real_instrument_at_a_different_tempo_is_followed(real):
  """The case a microphone is for, measured without a pianist: the recording itself,
  re-timed to a different tempo -- a real piano, not a rendering, playing the piece
  slower or faster than the reference.

  This is the one measured case in which a real instrument *is* followed: the same
  instrument on both sides, so the chroma contrast survives the tempo change (a
  rendering against a real piano does not -- see the test at the bottom). Measured over
  40 s: 0.85x was 96.9% of bars exact with a worst lag of 0.20 s, 1.15x 93.4% exact with
  a worst lag of 0.31 s, and 100% within one bar in both. A *different* piano is not
  something that can be tested on this machine: that needs the player's own instrument.
  """
  import numpy as np
  cfg, piece, _chroma, _grid, _path = real
  chroma, grid, path = follow.reference_for(cfg, piece, source='recording')
  data, rate = _audio(path)
  seconds = 40.0
  for factor in (0.85, 1.15):
    follower = follow.OnlineFollower(chroma, grid)
    played = follow.follow_file(follower, _stretched(data, rate, factor, seconds=seconds))
    errors, exact, within = [], 0, 0
    for index, position in enumerate(played, start=1):
      want = factor * index * follow.HOP / follow.SAMPLE_RATE
      bar = grid.bar_at(want)[0]
      exact += position.bar == bar
      within += abs(position.bar - bar) <= 1
      errors.append(position.reference_s - want)
    errors = np.array(errors)
    frames = len(played)
    assert exact / frames > 0.90, \
        'at %.2fx only %.1f%% of bars were exact' % (factor, 100 * exact / frames)
    assert within == frames, 'at %.2fx %d bars were more than one out' % (factor, frames - within)
    assert np.abs(errors).max() < 0.6, \
        'at %.2fx the worst lag was %.2f s' % (factor, np.abs(errors).max())


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
