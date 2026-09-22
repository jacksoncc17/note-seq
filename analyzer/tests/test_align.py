"""Tests for the alignment module's own machinery.

The real-recording contract lives in `test_align_integration.py`. What is tested
here is the logic that does not need a fixture: the arithmetic of the baseline,
the clamping that makes a warped path usable, the transposition estimate, and a
whole-pipeline run where the "performance" is the rendered score itself, so the
expected answer is known exactly.
"""

import pathlib
import shutil

import numpy as np
import pytest

from analyzer.config import AnalyzerConfig
from analyzer.schema import SyncResult
from analyzer import align, measures, syncmap


# ---------------------------------------------------------------------------
# fixtures made in the test, so the suite needs nothing from the machine
# ---------------------------------------------------------------------------

def write_midi(path, bars=8, beats=3, qpm=120, pitch=60):
  """A tiny steady MIDI: one quarter note per beat, `bars` bars long."""
  import mido
  midi = mido.MidiFile(ticks_per_beat=480)
  track = mido.MidiTrack()
  midi.tracks.append(track)
  track.append(mido.MetaMessage('time_signature', numerator=beats, denominator=4, time=0))
  track.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(qpm), time=0))
  for bar in range(bars):
    for beat in range(beats):
      track.append(mido.Message('note_on', note=pitch + bar % 5, velocity=80, time=0))
      track.append(mido.Message('note_off', note=pitch + bar % 5, velocity=0, time=480))
  midi.save(str(path))
  return path


@pytest.fixture
def cfg(tmp_path):
  config = AnalyzerConfig(cache_dir=tmp_path / 'cache', output_dir=tmp_path / 'out')
  config.ensure_dirs()
  return config


needs_fluidsynth = pytest.mark.skipif(
    shutil.which('fluidsynth') is None or align.find_soundfont() is None,
    reason='rendering the score needs fluidsynth and a soundfont')


# ---------------------------------------------------------------------------
# the baseline
# ---------------------------------------------------------------------------

def test_linear_estimate_arithmetic():
  result = align.linear_estimate([1, 2, 3, 4], 8.0)
  assert result.method == SyncResult.METHOD_LINEAR
  assert result.seconds_per_measure == pytest.approx(2.0)
  assert [m.start_s for m in result.measures] == pytest.approx([0.0, 2.0, 4.0, 6.0])
  assert [m.end_s for m in result.measures] == pytest.approx([2.0, 4.0, 6.0, 8.0])
  assert result.warnings and 'guess' in result.warnings[0]
  assert all(m.confidence is None for m in result.measures)
  assert result.is_trusted is False


def test_linear_estimate_and_the_map_layers_helper_agree():
  """Two implementations of the same guess must not drift apart."""
  mine = align.linear_estimate([1, 2, 3, 2, 3, 4], 9.0, lead_in_s=1.0)
  theirs = syncmap.uniform_map([1, 2, 3, 2, 3, 4], 9.0, lead_in_s=1.0)
  assert mine.seconds_per_measure == pytest.approx(theirs.seconds_per_measure)
  assert [m.start_s for m in mine.measures] == pytest.approx(
      [m.start_s for m in theirs.measures])


def test_linear_estimate_keeps_a_lead_in_outside_the_division():
  result = align.linear_estimate([1, 2, 3, 4], 10.0, lead_in_s=2.0)
  assert result.measures[0].start_s == pytest.approx(2.0)
  assert result.measures[-1].end_s == pytest.approx(10.0)
  assert result.offset_s == pytest.approx(2.0)


def test_linear_estimate_rejects_nonsense():
  with pytest.raises(ValueError):
    align.linear_estimate([], 10.0)
  with pytest.raises(ValueError):
    align.linear_estimate([1, 2], 1.0, lead_in_s=2.0)


def test_progress_callback_that_raises_cannot_break_the_estimate():
  def angry(percent, message):
    raise RuntimeError('the window went away')
  assert align.linear_estimate([1, 2, 3, 4], 8.0)          # baseline unaffected
  align._report(angry, 50.0, 'ignored')                    # must not raise


# ---------------------------------------------------------------------------
# clamping a warped path into a possible performance
# ---------------------------------------------------------------------------

def test_clamping_leaves_a_sane_map_alone():
  times = [i * 1.5 for i in range(10)]
  adjusted, moved, median = align.clamp_bar_times(times, 15.0)
  assert moved == 0
  assert adjusted == pytest.approx(times)
  assert median == pytest.approx(1.5)


def test_clamping_removes_zero_length_bars():
  times = [0.0, 1.5, 1.5, 1.5, 6.0, 7.5, 9.0, 10.5, 12.0, 13.5]   # two bars squeezed flat
  adjusted, moved, median = align.clamp_bar_times(times, 15.0)
  gaps = np.diff(list(adjusted) + [15.0])
  assert moved > 0
  assert gaps.min() > 0.0
  assert gaps.min() >= 0.4 * median - 1e-6
  assert all(b > a for a, b in zip(adjusted, adjusted[1:]))


def test_clamping_bounds_how_fast_the_bar_length_may_change():
  times = [0.0] + list(np.cumsum([1.5] * 5 + [7.0] + [1.5] * 5))
  adjusted, moved, median = align.clamp_bar_times(times, times[-1] + 1.5)
  gaps = np.diff(list(adjusted) + [times[-1] + 1.5])
  ratios = [b / a for a, b in zip(gaps, gaps[1:])]
  assert moved > 0
  assert max(ratios) <= align._MAX_NEIGHBOUR_RATIO + 1e-6
  assert min(ratios) >= 1.0 / align._MAX_NEIGHBOUR_RATIO - 1e-6


def test_clamping_still_fills_the_whole_performance():
  """The bars must reach the end of the music, not leave a gap at the end."""
  times = [0.0, 0.2, 5.0, 5.1, 9.0]
  adjusted, _moved, _median = align.clamp_bar_times(times, 12.0)
  assert adjusted[0] == pytest.approx(0.0)
  assert adjusted[-1] < 12.0
  assert 12.0 - adjusted[-1] <= 2.5 * _median_of(adjusted, 12.0)


def _median_of(times, end_s):
  durations = list(np.diff(list(times) + [end_s]))
  return sorted(durations)[len(durations) // 2]


def test_clamping_an_empty_or_degenerate_input():
  assert align.clamp_bar_times([], 10.0) == ([], 0, 0.0)
  times, moved, median = align.clamp_bar_times([5.0, 5.0, 5.0], 5.0)
  assert moved >= 0 and median == 0.0        # nothing to measure against


# ---------------------------------------------------------------------------
# transposition
# ---------------------------------------------------------------------------

def _chroma_from(notes, frames=200):
  chroma = np.zeros((12, frames), dtype='float32')
  for index, pitch in enumerate(notes):
    chroma[pitch % 12, index::len(notes)] += 1.0
  return chroma


def test_transposition_estimate_is_zero_for_the_same_music():
  score = _chroma_from([60, 64, 67, 72])
  assert align.estimate_chroma_shift(score, score.copy()) == 0


def test_transposition_estimate_finds_a_shifted_recording():
  score = _chroma_from([60, 64, 67, 72])
  for shift in (1, 2, 5, 7, 11):
    # the *audio* is the score moved up `shift`; the score must move up by the same
    assert align.estimate_chroma_shift(score, np.roll(score, shift, axis=0)) == shift


def test_transposition_estimate_survives_empty_input():
  assert align.estimate_chroma_shift(np.zeros((12, 0)), np.zeros((12, 10))) == 0


# ---------------------------------------------------------------------------
# reading the score's timing
# ---------------------------------------------------------------------------

def test_midi_grid_agrees_with_midos_own_length(tmp_path):
  import mido
  path = write_midi(tmp_path / 'steady.mid', bars=8, beats=3, qpm=120)
  bar_s, beats, qpm = align.midi_grid(path)
  assert beats == 3.0 and qpm == pytest.approx(120.0)
  assert bar_s == pytest.approx(1.5)
  assert bar_s * 8 == pytest.approx(mido.MidiFile(str(path)).length, rel=0.02)


def test_midi_grid_rejects_a_file_with_no_notes(tmp_path):
  import mido
  midi = mido.MidiFile(ticks_per_beat=480)
  track = mido.MidiTrack()
  midi.tracks.append(track)
  track.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(100), time=0))
  path = tmp_path / 'empty.mid'
  midi.save(str(path))
  with pytest.raises(align.AlignError):
    align.midi_grid(path)


def test_midi_grid_rejects_a_non_midi_file(tmp_path):
  path = tmp_path / 'junk.mid'
  path.write_text('not a midi file')
  with pytest.raises(align.AlignError):
    align.midi_grid(path)


# ---------------------------------------------------------------------------
# audio analysis helpers
# ---------------------------------------------------------------------------

def _write_tone(path, seconds=6.0, lead=2.0, tail=1.0, sr=22050, freq=440.0):
  import soundfile as sf
  total = int(seconds * sr)
  t = np.arange(total) / sr
  wave = 0.4 * np.sin(2 * np.pi * freq * t).astype('float32')
  wave[: int(lead * sr)] = 0.0
  wave[int((seconds - tail) * sr):] = 0.0
  sf.write(str(path), wave, sr)
  return path


def test_performance_span_finds_the_sound_inside_the_silence(tmp_path):
  path = _write_tone(tmp_path / 'tone.wav', seconds=6.0, lead=2.0, tail=1.0)
  start, end = align.performance_span(path)
  assert start == pytest.approx(2.0, abs=0.4)
  assert end == pytest.approx(5.0, abs=0.4)


def test_performance_span_of_silence_is_the_whole_file(tmp_path):
  import soundfile as sf
  path = tmp_path / 'silence.wav'
  sf.write(str(path), np.zeros(22050, dtype='float32'), 22050)
  start, end = align.performance_span(path)
  assert (start, end) == (0.0, pytest.approx(1.0, abs=0.05))


def test_audio_chroma_shape_and_normalisation(tmp_path):
  path = _write_tone(tmp_path / 'tone.wav')
  chroma = align.audio_chroma(path)
  assert chroma.shape[0] == 12
  assert chroma.shape[1] > 10
  assert np.all(np.isfinite(chroma))
  assert chroma.max() <= 1.0 + 1e-6


def test_an_unreadable_recording_is_an_align_error(tmp_path):
  path = tmp_path / 'not-audio.wav'
  path.write_text('definitely not audio')
  with pytest.raises(align.AlignError):
    align.audio_chroma(path)


# ---------------------------------------------------------------------------
# the whole pipeline, against a performance whose answer we know
# ---------------------------------------------------------------------------

@needs_fluidsynth
def test_aligning_a_rendered_score_to_itself_recovers_the_bar_grid(tmp_path, cfg):
  """Render the score and align the render to itself: every bar must land on
  the grid the MIDI already tells us, which is the one case with known truth."""
  import soundfile as sf

  midi = write_midi(tmp_path / 'steady.mid', bars=12, beats=3, qpm=120)
  wav = tmp_path / 'perf.wav'
  align.render_midi_to_wav(midi, wav, sr=22050)
  samples, rate = sf.read(str(wav), dtype='float32')
  assert len(samples) / rate > 15.0          # 12 bars x 1.5 s + tail

  played = list(range(1, 13))
  result = align.align_to_audio(played, str(wav), str(midi), cfg=cfg)
  assert result.method == SyncResult.METHOD_DTW
  assert len(result.measures) == 12

  bar_s, _beats, _qpm = align.midi_grid(midi)
  expected = [i * bar_s for i in range(12)]
  got = [m.start_s for m in result.measures]
  errors = [abs(g - e) for g, e in zip(got, expected)]
  assert np.median(errors) < 0.35, 'median error %.3f s' % np.median(errors)

  assert syncmap.check_plausibility(result) == []
  assert result.quality['hop_ms'] == pytest.approx(92.9, abs=0.5)
  assert result.quality['score_frames'] > 0 and result.quality['audio_frames'] > 0
  assert result.quality['bars_clamped'] == 0


@needs_fluidsynth
def test_a_transposed_performance_is_corrected(tmp_path, cfg):
  """Shift the rendered performance up two semitones and check the fit still
  works, which it cannot without the key correction."""
  import librosa
  import soundfile as sf

  midi = write_midi(tmp_path / 'steady.mid', bars=12, beats=3, qpm=120)
  wav = tmp_path / 'perf.wav'
  align.render_midi_to_wav(midi, wav, sr=22050)
  samples, rate = sf.read(str(wav), dtype='float32')
  if samples.ndim > 1:                     # fluidsynth renders a stereo pair
    samples = samples.mean(axis=1)
  shifted = librosa.effects.pitch_shift(y=samples, sr=rate, n_steps=2)
  transposed = tmp_path / 'perf-plus-2.wav'
  sf.write(str(transposed), shifted, rate)

  played = list(range(1, 13))
  result = align.align_to_audio(played, str(transposed), str(midi), cfg=cfg)
  assert result.quality['chroma_shift'] == 2
  assert any('semitone' in w for w in result.warnings)

  bar_s, _b, _q = align.midi_grid(midi)
  errors = [abs(m.start_s - i * bar_s) for i, m in enumerate(result.measures)]
  assert np.median(errors) < 0.5, 'median error after correction %.3f s' % np.median(errors)


def test_aligning_nothing_is_refused(tmp_path):
  with pytest.raises(ValueError):
    align.align_to_audio([], str(tmp_path / 'x.wav'), str(tmp_path / 'x.mid'))


@needs_fluidsynth
def test_a_recording_far_longer_than_the_score_is_flagged(tmp_path, cfg):
  """Length is a structural check that *does* discriminate, unlike match cost.

  A recording twice as long as the score implies is an unnotated repeat, a
  different edition, or a different piece -- and saying so costs nothing.
  """
  import soundfile as sf

  midi = write_midi(tmp_path / 'steady.mid', bars=12, beats=3, qpm=120)
  wav = tmp_path / 'perf.wav'
  align.render_midi_to_wav(midi, wav, sr=22050)
  samples, rate = sf.read(str(wav), dtype='float32')
  if samples.ndim > 1:
    samples = samples.mean(axis=1)
  doubled = tmp_path / 'twice.wav'
  sf.write(str(doubled), np.concatenate([samples, samples]), rate)

  result = align.align_to_audio(list(range(1, 13)), str(doubled), str(midi), cfg=cfg)
  assert any('score implies' in w for w in result.warnings), result.warnings
  assert result.quality['duration_ratio'] > 1.5
  assert result.quality['expected_duration_s'] == pytest.approx(18.0, abs=1.0)
  # the map must still be drawable, whatever the recording turns out to be
  assert syncmap.check_plausibility(result) == []


@needs_fluidsynth
def test_confidence_reflects_intervention_not_the_match_cost(tmp_path, cfg):
  """A fit that needed no forcing, on a recording of the right length, is high."""
  midi = write_midi(tmp_path / 'steady.mid', bars=12, beats=3, qpm=120)
  wav = tmp_path / 'perf.wav'
  align.render_midi_to_wav(midi, wav, sr=22050)
  result = align.align_to_audio(list(range(1, 13)), str(wav), str(midi), cfg=cfg)

  assert result.quality['bars_forced'] == 0
  assert result.quality['duration_ratio'] == pytest.approx(1.0, abs=0.2)
  assert min(m.confidence for m in result.measures) >= 0.8
  # and the raw diagnostic is still recorded, even though nothing gates on it
  assert 'normalized_cost' in result.quality
