"""Integration tests for the map the app actually hands the user.

These are deliberately about the *contract the UI depends on*, not about
alignment accuracy: whatever `align_to_audio` returns must be safe to draw as
markers and to turn into a playable time range. Accuracy on a real recording has
no ground truth here -- there is no reference annotation for a YouTube
performance -- so what is asserted is plausibility and internal consistency, and
those are exactly the properties whose absence made earlier prototypes unusable
(bar markers 0.0 s apart, or a 5.8 s jump between neighbouring bars).
"""

import pathlib

import pytest

from analyzer.config import AnalyzerConfig
from analyzer.schema import SyncResult
from analyzer import measures, syncmap

align = pytest.importorskip('analyzer.align', reason='the alignment module is not installed')

BANK = pathlib.Path(__file__).resolve().parents[2] / 'memorization' / 'output' / 'pieces'
SLUG = 'chopin-waltz-in-a-minor-op-34-no-2'
AUDIO = AnalyzerConfig().audio_path(SLUG)
MIDI = BANK / SLUG / 'fingerprint_steady.mid'
SCORE = pathlib.Path('/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl')

needs_real = pytest.mark.skipif(
    not (AUDIO.is_file() and MIDI.is_file() and SCORE.is_file()),
    reason='the real recording and reference MIDI are not both on this machine')


def test_linear_estimate_arithmetic():
  """The baseline is pure arithmetic, so it must be exactly right."""
  result = align.linear_estimate([1, 2, 3, 4], duration_s=8.0)
  assert isinstance(result, SyncResult)
  assert result.seconds_per_measure == pytest.approx(2.0)
  assert [m.start_s for m in result.measures] == pytest.approx([0.0, 2.0, 4.0, 6.0])
  assert [m.end_s for m in result.measures] == pytest.approx([2.0, 4.0, 6.0, 8.0])
  assert result.method == SyncResult.METHOD_LINEAR
  # an average-tempo guess must never look like a measurement
  assert result.warnings, 'the estimate should say it is a guess'
  assert all(m.confidence is None for m in result.measures)


def test_linear_estimate_respects_a_lead_in():
  result = align.linear_estimate([1, 2, 3, 4], duration_s=10.0, lead_in_s=2.0)
  assert result.measures[0].start_s == pytest.approx(2.0)
  assert result.measures[-1].end_s == pytest.approx(10.0)


def test_linear_estimate_of_a_real_piece_and_recording():
  """309.7 s over 204 bars is ~1.5 s per bar; the arithmetic must say so."""
  played = measures.played_measures(SCORE)
  assert len(played) == 204
  result = align.linear_estimate(played, duration_s=309.7)
  assert result.seconds_per_measure == pytest.approx(1.518, abs=0.01)
  assert result.measures[0].start_s == pytest.approx(0.0)
  assert result.measures[-1].end_s == pytest.approx(309.7, abs=0.01)


@needs_real
def test_alignment_of_the_real_recording_is_plausible():
  """The map must be drawable and playable, whatever its accuracy."""
  played = measures.played_measures(SCORE)
  result = align.align_to_audio(played, str(AUDIO), str(MIDI), cfg=AnalyzerConfig())

  assert result.method == SyncResult.METHOD_DTW
  assert len(result.measures) == len(played)

  # strictly increasing, no zero-length bars, nothing past the end of the audio
  assert syncmap.check_plausibility(result) == [], (
      'the aligner returned a map the UI would have to warn about: %s'
      % syncmap.check_plausibility(result))
  earlier = result.measures[0]
  for current in result.measures[1:]:
    assert current.start_s > earlier.start_s
    assert current.end_s >= current.start_s
    earlier = current
  assert result.measures[-1].end_s <= result.audio_duration_s + 1.0

  # the fit must be reported with the numbers the UI shows
  for key in ('hop_ms', 'score_frames', 'audio_frames'):
    assert key in result.quality, 'quality is missing %r' % key

  # and every bar in the piece must resolve to a playable range
  span = result.range_for_measures(40, 83)
  assert span is not None
  start, end = span
  assert 0.0 <= start < end <= result.audio_duration_s + 1.0
  assert 20.0 < end - start < 120.0        # 44 bars of a 1.5 s/bar waltz


@needs_real
def test_alignment_beats_or_matches_the_estimate_on_global_span():
  """The fit should not float away from the recording's own start and end."""
  played = measures.played_measures(SCORE)
  result = align.align_to_audio(played, str(AUDIO), str(MIDI), cfg=AnalyzerConfig())
  assert result.measures[0].start_s < 10.0
  assert result.measures[-1].end_s > result.audio_duration_s - 20.0
