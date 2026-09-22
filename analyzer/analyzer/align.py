"""Fitting the measure map to what is actually played.

The score gives every measure a position; the recording gives every moment a
time. This module joins them with dynamic time warping over chroma features:
both sides are reduced to 12 pitch-class vectors per 93 ms frame, DTW finds the
cheapest monotone path between them, and each measure boundary is read off that
path.

Three things are deliberately unglamorous, because each one was measured to
matter more than the choice of features:

**The score side is rendered to audio first.** Comparing a sparse piano-roll
against the spectrum of a real piano (pedal, resonance, voicing) produced
erratic maps in testing: bar durations from 0.0 s to 7.2 s with 5.8 s jumps.
Rendering the reference MIDI with FluidSynth and extracting the *same* features
from both sides keeps the comparison honest, and falls back to the piano roll
only if FluidSynth is unavailable.

**Silence is trimmed, then put back.** A 2.5 s lead-in off else offsets every
measure by exactly 2.5 s; trimming without adding the offset back is worse than
not trimming at all.

**The result is clamped to be physically possible.** Standard DTW has no notion
of a bar, so it will happily squash one bar to nothing and stretch its
neighbour. Bars shorter than 0.4x or longer than 2.5x the median are pulled
back, and the caller is told how many were touched. What comes out is a *usable
starting point* for a human to drag, not a measured truth -- on real recordings
expect a few hundred milliseconds per bar, with the worst errors at fermatas,
rolled chords and repeated material.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import AnalyzerConfig
from .schema import MeasureTime, SyncResult


Progress = Optional[Callable[[float, str], None]]

HOP_LENGTH = 2048              # ~93 ms per frame at 22 kHz
_MIN_BAR_FRACTION = 0.4        # a bar may not be shorter than this x the median
_MAX_BAR_FRACTION = 2.5        # ... nor longer
_MAX_NEIGHBOUR_RATIO = 1.6     # ... nor change this much from the bar before it
_SILENCE_FRACTION = 0.05       # RMS gate for finding where playing starts
SOUNDFONTS = (
    pathlib.Path('/home/wei/soundfonts/Sonatina_Symphonic_Orchestra.sf2'),
    pathlib.Path('/usr/share/sounds/sf2/FluidR3_GM.sf2'),
    pathlib.Path('/etc/alternatives/default-GM.sf2'),
    pathlib.Path('/etc/alternatives/default-GM.sf3'),
)


class AlignError(RuntimeError):
  """Raised when the recording or the score cannot be used for alignment."""


def _report(progress: Progress, percent: float, message: str) -> None:
  """Call the progress callback, ignoring a callback that raises.

  A GUI that closes mid-alignment must not take the worker down with it.
  """
  if progress is None:
    return
  try:
    progress(float(percent), message)
  except Exception:
    pass


# ---------------------------------------------------------------------------
# audio and features
# ---------------------------------------------------------------------------

def _mono(path) -> Tuple[np.ndarray, int]:
  import soundfile as sf
  try:
    data, rate = sf.read(str(path), dtype='float32', always_2d=False)
  except Exception as e:
    raise AlignError('could not read %s: %s' % (pathlib.Path(path).name, e))
  if data.ndim > 1:
    data = data.mean(axis=1)
  return np.ascontiguousarray(data, dtype='float32'), int(rate)


def _resample(samples: np.ndarray, rate: int, target: int) -> np.ndarray:
  if target == rate:
    return samples
  import librosa
  return librosa.resample(samples, orig_sr=rate, target_sr=target)


def _chroma_of(samples: np.ndarray, rate: int, target: int,
               hop_length: int = HOP_LENGTH) -> np.ndarray:
  import librosa
  chroma = librosa.feature.chroma_cens(y=_resample(samples, rate, target),
                                       sr=target, hop_length=hop_length)
  return librosa.util.normalize(chroma, axis=0)


def audio_chroma(wav_path, sr: Optional[int] = None, hop_length: int = HOP_LENGTH) -> np.ndarray:
  """CENS chroma of an audio file: 12 pitch classes per frame, L2-normalised."""
  samples, rate = _mono(wav_path)
  return _chroma_of(samples, rate, int(sr or rate), hop_length)


def _span_of(samples: np.ndarray, rate: int, target: int,
             hop_length: int = HOP_LENGTH) -> Tuple[float, float]:
  import librosa
  samples = _resample(samples, rate, target)
  duration = len(samples) / float(target)
  rms = librosa.feature.rms(y=samples, frame_length=hop_length * 2,
                            hop_length=hop_length)[0]
  if rms.size == 0 or rms.max() <= 0:
    return 0.0, duration
  indices = np.flatnonzero(rms > (_SILENCE_FRACTION * rms.max()))
  if indices.size == 0:
    return 0.0, duration
  frame_s = hop_length / float(target)
  start = max(0.0, indices[0] * frame_s - frame_s)
  end = min(duration, (indices[-1] + 1) * frame_s + frame_s)
  return float(start), float(end)


def performance_span(wav_path, sr: Optional[int] = None,
                     hop_length: int = HOP_LENGTH) -> Tuple[float, float]:
  """Where playing starts and stops, so silence is not counted as music.

  Returns:
    (start_s, end_s), pulled in slightly from the first and last frames above a
    loudness gate. A recording that is loud throughout returns (0, duration).

  Raises:
    AlignError: if the file cannot be read.
  """
  samples, rate = _mono(wav_path)
  return _span_of(samples, rate, int(sr or rate), hop_length)


def find_soundfont() -> Optional[pathlib.Path]:
  """The first usable soundfont, or None."""
  for candidate in SOUNDFONTS:
    if candidate.is_file():
      return candidate
  return None


def render_midi_to_wav(midi_path, out_path, soundfont=None,
                       sr: Optional[int] = None) -> str:
  """Render a MIDI file to WAV with FluidSynth, and return the output path.

  Raises:
    AlignError: when FluidSynth or a soundfont is missing, or it fails.
  """
  binary = shutil.which('fluidsynth')
  if binary is None:
    raise AlignError('fluidsynth is not installed, so the score cannot be rendered')
  font = pathlib.Path(soundfont) if soundfont else find_soundfont()
  if font is None or not font.is_file():
    raise AlignError('no soundfont found; set one with render_midi_to_wav(..., soundfont=...)')
  out_path = pathlib.Path(out_path)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  command = [binary, '-ni', '-g', '0.6', '-F', str(out_path),
             '-r', str(int(sr or 22050)), str(font), str(midi_path)]
  result = subprocess.run(command, capture_output=True, text=True)
  if result.returncode != 0 or not out_path.is_file():
    raise AlignError('fluidsynth could not render %s: %s'
                     % (pathlib.Path(midi_path).name,
                        (result.stderr or result.stdout or '').strip()[:200]))
  return str(out_path)


# ---------------------------------------------------------------------------
# the score's own timing
# ---------------------------------------------------------------------------

def midi_grid(midi_path) -> Tuple[float, float, float]:
  """Read a steady reference MIDI's timing.

  Returns:
    (seconds_per_bar, beats_per_bar, qpm). The reference render written by
    `memorization/analyze.py` has one tempo and one time signature, so the bar
    grid is uniform and this is exact.

  Raises:
    AlignError: if the file cannot be read or has no notes.
  """
  import mido
  try:
    midi = mido.MidiFile(str(midi_path))
  except Exception as e:
    raise AlignError('could not read the reference MIDI: %s' % e)

  # Iterating a multi-track file yields per-track deltas, which do not add up;
  # merging first and accumulating with the running tempo does, and the total is
  # checked against mido's own length below.
  seconds, qpm, beats = 0.0, 120.0, 4.0
  notes = 0
  for message in mido.merge_tracks(midi.tracks):
    seconds += mido.tick2second(message.time, midi.ticks_per_beat,
                                mido.bpm2tempo(qpm))
    if message.type == 'set_tempo':
      qpm = mido.tempo2bpm(message.tempo)
    elif message.type == 'time_signature':
      beats = float(message.numerator)
    elif message.type == 'note_on' and message.velocity > 0:
      notes += 1
  if notes == 0:
    raise AlignError('the reference MIDI has no notes')
  if midi.length > 1.0 and abs(seconds - midi.length) > 0.25 * midi.length:
    # Defensive: a pathological tempo event would freeze the clock silently.
    seconds = float(midi.length)
  return (beats * 60.0 / qpm), beats, qpm


# ---------------------------------------------------------------------------
# the trivial map, and the machinery both paths share
# ---------------------------------------------------------------------------

def _measures_from_times(played: Sequence[int], times: Sequence[float],
                         end_s: float, confidence: Optional[Sequence[Optional[float]]] = None,
                         method: str = SyncResult.METHOD_DTW) -> List[MeasureTime]:
  """Build MeasureTimes from the start time of each played measure."""
  out: List[MeasureTime] = []
  count = len(played)
  for index in range(count):
    start = float(times[index])
    stop = float(times[index + 1]) if index + 1 < count else float(end_s)
    if stop < start:
      stop = start
    conf = None if confidence is None else confidence[index]
    out.append(MeasureTime(measure=int(played[index]), play_index=index,
                           start_s=start, end_s=stop, confidence=conf))
  return out


def linear_estimate(played, duration_s: float, lead_in_s: float = 0.0,
                    tail_s: float = 0.0) -> SyncResult:
  """Spread the recording evenly across the measures.

  Pure arithmetic, no analysis: useful as a starting point and as the baseline
  every refined map is compared against. It is a guess -- measured against a real
  performance it is one to two seconds per bar off -- so the result carries a
  warning saying so and no per-measure confidence.

  Args:
    played: printed measure numbers in performance order.
    duration_s: length of the recording in seconds.
    lead_in_s: seconds before playing starts, excluded from the division.
    tail_s: seconds after playing stops, likewise.

  Returns:
    A `SyncResult` with method 'linear'.

  Raises:
    ValueError: with no measures, or a span that is not positive.
  """
  played = list(played)
  if not played:
    raise ValueError('no measures to map')
  span = float(duration_s) - float(lead_in_s) - float(tail_s)
  if span <= 0:
    raise ValueError('the recording is shorter than the lead-in and tail')
  step = span / len(played)
  times = [float(lead_in_s) + i * step for i in range(len(played))]
  measures = _measures_from_times(played, times, float(lead_in_s) + span,
                                  method=SyncResult.METHOD_LINEAR)
  return SyncResult(
      method=SyncResult.METHOD_LINEAR, measures=measures, seconds_per_measure=step,
      audio_duration_s=float(duration_s), offset_s=float(lead_in_s),
      warnings=['this map spreads the recording evenly over the measures; it is an '
                'average-tempo guess, not a measurement of the performance'])


def estimate_chroma_shift(score: np.ndarray, audio: np.ndarray) -> int:
  """How many semitones the recording sits away from the score.

  Transposed uploads are common, and an uncorrected two-semitone difference was
  measured to put *no* measure within half a second of the truth. Comparing the
  average chroma of each side over the twelve rotations is cheap and decisive.

  Returns:
    A shift in 0..11 to apply to the *score* side.
  """
  if score.size == 0 or audio.size == 0:
    return 0
  score_mean = score.mean(axis=1)
  audio_mean = audio.mean(axis=1)
  if not score_mean.any() or not audio_mean.any():
    return 0
  best_similarity, best_shift = -1.0, 0
  for shift in range(12):
    similarity = float(np.dot(np.roll(score_mean, shift), audio_mean)) / (
        float(np.linalg.norm(score_mean)) * float(np.linalg.norm(audio_mean)) + 1e-12)
    if similarity > best_similarity:
      best_similarity, best_shift = similarity, shift
  return best_shift


def clamp_bar_times(times: Sequence[float], end_s: float,
                    min_fraction: float = _MIN_BAR_FRACTION,
                    max_fraction: float = _MAX_BAR_FRACTION,
                    neighbour_ratio: float = _MAX_NEIGHBOUR_RATIO
                    ) -> Tuple[List[float], int, float]:
  """Pull a ragged alignment back into something a real performance could be.

  DTW will happily give one bar 0.00 s and the next 7.24 s. Two passes make the
  result usable: every bar is first squeezed into
  `[min_fraction, max_fraction]` of the median bar length, and then neighbouring
  bars are prevented from differing by more than `neighbour_ratio`, because
  tempo in real playing drifts smoothly and a sudden 4:1 change between adjacent
  bars is a fitting artefact, not a performance. The sequence stays strictly
  increasing.

  This deliberately distorts the raw warping path -- the caller keeps the raw
  numbers in `quality` so nothing is hidden.

  Args:
    times: start time of each bar, in order.
    end_s: where the performance ends, used to bound the final bar.
    min_fraction: shortest allowed bar, as a multiple of the median.
    max_fraction: longest allowed bar.
    neighbour_ratio: largest allowed change between adjacent bar lengths.

  Returns:
    (adjusted times, how many bars were moved, the median bar length).
  """
  if not times:
    return [], 0, 0.0
  times = [float(t) for t in times]
  durations = [max(0.0, times[i + 1] - times[i]) for i in range(len(times) - 1)]
  durations.append(max(0.0, float(end_s) - times[-1]))
  positive = sorted(d for d in durations if d > 0)
  median = positive[len(positive) // 2] if positive else 0.0
  if median <= 0:
    return times, 0, 0.0

  low, high = min_fraction * median, max_fraction * median
  adjusted_durations = [min(max(d, low), high) for d in durations]

  # Neighbour pass, both directions so no pair is left violating the ratio.
  for index in range(1, len(adjusted_durations)):
    previous = adjusted_durations[index - 1]
    adjusted_durations[index] = min(max(adjusted_durations[index],
                                        previous / neighbour_ratio),
                                    previous * neighbour_ratio)
  for index in range(len(adjusted_durations) - 2, -1, -1):
    following = adjusted_durations[index + 1]
    adjusted_durations[index] = min(max(adjusted_durations[index],
                                        following / neighbour_ratio),
                                    following * neighbour_ratio)

  moved = sum(1 for original, new in zip(durations, adjusted_durations)
              if abs(original - new) > 1e-6)

  # Rescale so the bars still fill exactly the performance. Without this the
  # reconstruction would drop the final duration and the *last* bar would absorb
  # whatever drift the clamping introduced, which showed up as a 9.6 s final bar.
  target_total = float(end_s) - times[0]
  total = sum(adjusted_durations)
  if target_total > 0 and total > 0:
    scale = target_total / total
    adjusted_durations = [d * scale for d in adjusted_durations]

  adjusted = [times[0]]
  for duration in adjusted_durations[:-1]:
    adjusted.append(adjusted[-1] + max(duration, 0.05))
  return adjusted, moved, median


# ---------------------------------------------------------------------------
# the alignment itself
# ---------------------------------------------------------------------------

def score_chroma(score_midi, played, sr: Optional[int] = None,
                 hop_length: int = HOP_LENGTH, cfg: Optional[AnalyzerConfig] = None,
                 progress: Progress = None) -> np.ndarray:
  """Chroma of the score, rendered so it can be compared with a recording.

  FluidSynth renders the reference MIDI to audio (cached under
  `cfg.render_dir`), and the same CENS features are then extracted from both
  sides. If FluidSynth is unavailable the MIDI's own note timings are laid out as
  a piano roll instead, which is less accurate but needs nothing extra.

  Args:
    score_midi: the steady reference MIDI for the piece.
    played: printed measure numbers in performance order (the render already
      follows the score, so this is accepted for symmetry and validation).
    sr: sample rate for the features.
    hop_length: frames step.
    cfg: where to cache the render.
    progress: optional callback.

  Returns:
    A (12, frames) matrix.

  Raises:
    AlignError: if neither path can produce features.
  """
  import librosa
  midi_path = pathlib.Path(score_midi)
  target = int(sr or (cfg.target_sample_rate if cfg else 22050))
  try:
    if cfg is not None:
      cfg.ensure_dirs()
      rendered = cfg.render_dir / ('%s-%d.wav' % (midi_path.stem, target))
    else:
      rendered = pathlib.Path(tempfile.gettempdir()) / ('%s-%d.wav' % (midi_path.stem, target))
    if not rendered.is_file() or rendered.stat().st_mtime < midi_path.stat().st_mtime:
      _report(progress, 10.0, 'rendering the score')
      render_midi_to_wav(midi_path, rendered, sr=target)
    _report(progress, 20.0, 'reading the score')
    return audio_chroma(rendered, sr=target, hop_length=hop_length)
  except AlignError:
    pass

  # Fallback: piano-roll chroma straight from the MIDI's note events.
  import mido
  _report(progress, 20.0, 'reading the score (piano roll)')
  midi = mido.MidiFile(str(midi_path))
  seconds, qpm, active, notes = 0.0, 120.0, {}, []
  for message in mido.merge_tracks(midi.tracks):
    seconds += mido.tick2second(message.time, midi.ticks_per_beat, mido.bpm2tempo(qpm))
    if message.type == 'set_tempo':
      qpm = mido.tempo2bpm(message.tempo)
    elif message.type == 'note_on' and message.velocity > 0:
      active[message.note] = seconds
    elif message.type in ('note_off',) or (message.type == 'note_on' and message.velocity == 0):
      if message.note in active:
        notes.append((active.pop(message.note), seconds, message.note))
  if not notes:
    raise AlignError('the reference MIDI has no usable notes')
  frames = int(max(end for _, end, _ in notes) * target / hop_length) + 8
  chroma = np.zeros((12, frames), dtype='float32')
  for start, stop, pitch in notes:
    first = int(start * target / hop_length)
    last = max(first + 1, int(stop * target / hop_length))
    chroma[pitch % 12, first:last] += 1.0
  return librosa.util.normalize(chroma, axis=0)


def align_to_audio(played, wav_path, score_midi, cfg: Optional[AnalyzerConfig] = None,
                   progress: Progress = None,
                   baseline: Optional[SyncResult] = None) -> SyncResult:
  """Fit every played measure to a time in the recording.

  Args:
    played: printed measure numbers in performance order.
    wav_path: the cached recording (any format libsndfile can read).
    score_midi: the steady reference MIDI for the score.
    cfg: paths, for caching the rendered score and choosing a sample rate.
    progress: optional `(percent, message)` callback.
    baseline: an existing map to compare against; the linear estimate is
      computed when this is omitted.

  Returns:
    A `SyncResult` with method 'dtw', per-measure confidence, and `quality`
    carrying the numbers a caller needs to judge it (match cost, key shift,
    frames, how many bars had to be clamped).

  Raises:
    ValueError: when `played` is empty.
    AlignError: when the recording cannot be read at all.
  """
  import librosa

  played = list(played)
  if not played:
    raise ValueError('no measures to map')

  target = int(cfg.target_sample_rate if cfg else 22050)
  score = score_chroma(score_midi, played, sr=target, cfg=cfg,
                       progress=lambda p, m: _report(progress, p * 0.5, m))
  _report(progress, 55.0, 'analysing the recording')
  samples, rate = _mono(wav_path)               # decoded once, used for both
  duration = len(samples) / float(rate)
  audio = _chroma_of(samples, rate, target)
  start_s, end_s = _span_of(samples, rate, target)
  warnings: List[str] = []

  if score.shape[1] == 0 or audio.shape[1] == 0:
    raise AlignError('one of the two sides has no audio to compare')

  # A transposed upload would otherwise never line up; correct the score side.
  shift = estimate_chroma_shift(score, audio)
  if shift:
    score = np.roll(score, shift, axis=0)
    warnings.append('the recording sounds %d semitone(s) away from the score; the '
                    'score side was shifted to match — check it is the same piece' % shift)

  # Align the *trimmed* performance, then add the trim back: aligning across
  # silence drags the first bars late by exactly the length of the silence.
  trimmed = audio[:, int(start_s * target / HOP_LENGTH):]
  if trimmed.shape[1] < 4:
    trimmed = audio

  cells = score.shape[1] * trimmed.shape[1]
  if cells > 60_000_000:
    raise AlignError('this recording is too long to align in one pass '
                     '(%d x %d frames); split it or raise the hop length'
                     % (score.shape[1], trimmed.shape[1]))

  _report(progress, 70.0, 'aligning (this is the slow part)')
  distance, path = librosa.sequence.dtw(X=score, Y=trimmed, subseq=False,
                                        metric='euclidean', backtrack=True)
  path = np.asarray(path)[::-1]            # librosa hands the path back end-first
  if path.size == 0:
    raise AlignError('the alignment produced an empty path')

  # Read each measure boundary off the path. Using the first path point at or
  # after the bar's score frame keeps the mapping monotone; interpolating
  # instead lets neighbouring bars cross.
  frames_per_second = target / float(HOP_LENGTH)
  bar_s, beats, qpm = midi_grid(score_midi)
  boundaries = []
  for index in range(len(played)):
    frame = index * bar_s * frames_per_second
    position = min(int(np.searchsorted(path[:, 0], frame, side='left')), len(path) - 1)
    boundaries.append(start_s + path[position, 1] / frames_per_second)

  raw_gaps = list(np.diff(boundaries)) if len(boundaries) > 1 else [0.0]
  raw_durations = list(np.diff(boundaries)) + \
      [max(0.0, float(end_s or duration) - boundaries[-1])]
  boundaries, clamped, median_bar = clamp_bar_times(boundaries, end_s or duration)
  final_durations = list(np.diff(boundaries)) + \
      [max(0.0, float(end_s or duration) - boundaries[-1])]
  # A bar counts as forced when its length changed by more than a few percent; the
  # whole span is rescaled by a near-1.0 factor, which must not count as forcing.
  forced = [abs(before - after) > max(0.05, 0.15 * max(before, after))
            for before, after in zip(raw_durations, final_durations)]
  if clamped:
    warnings.append('the automatic fit was erratic in %d bar(s) (raw bar lengths ran '
                    '%.2f-%.2f s); those markers were pulled into a plausible range — '
                    'drag them if they still look wrong'
                    % (clamped, min(raw_gaps), max(raw_gaps)))

  cost = float(distance[-1, -1] / max(1, len(path)))
  # NOTE: this number is *not* a usable "is this the right piece" test, however
  # tempting. Calibrated on this machine: an identical render scores 0.000, the
  # same music warped with rubato scores 1.694, the same music transposed scores
  # 1.007, and *different* music scores 1.407. A correctly aligned rubato
  # performance therefore scores worse than the wrong piece, because the metric
  # is dominated by how many frames the path has to spend, not by whether the
  # notes agree. It is kept as a diagnostic only; the honest structural checks
  # are the bar-length clamping and the duration comparison below.

  # Does the recording last about as long as the score says it should? This is a
  # cheap, interpretable structural test: a score implying 305 s matched against
  # a 500 s recording is a different edition, an unnotated repeat, or not this
  # piece at all.
  expected_s = bar_s * len(played)
  ratio = (duration / expected_s) if expected_s > 0 else 1.0
  if ratio < 0.75 or ratio > 1.35:
    warnings.append('the recording lasts %.0f%% of what the score implies '
                    '(%.0f s vs %.0f s): check it is the same edition, and whether '
                    'it plays repeats the score does not notate'
                    % (100.0 * ratio, duration, expected_s))

  if baseline is None:
    try:
      baseline = linear_estimate(played, duration)
    except ValueError:
      baseline = None
  deviations = []
  if baseline is not None and len(baseline.measures) == len(boundaries):
    deviations = [abs(boundaries[i] - baseline.measures[i].start_s)
                  for i in range(len(boundaries))]

  # Confidence says how much the machinery had to intervene, not the odds of
  # being right: a bar whose length had to be forced back into range is flagged
  # low, everything else is flagged high but scaled down when the recording's
  # overall length disagrees with the score.
  length_penalty = min(0.35, abs(ratio - 1.0) * 1.5)
  confidences = []
  for index in range(len(boundaries)):
    value = (0.35 if (index < len(forced) and forced[index]) else 0.9) - length_penalty
    confidences.append(round(max(0.0, min(1.0, value)), 3))

  measures = _measures_from_times(played, boundaries, end_s or duration, confidences)
  gaps = list(np.diff(boundaries)) if len(boundaries) > 1 else [0.0]

  quality: Dict[str, object] = {
      'hop_ms': round(1000.0 * HOP_LENGTH / target, 1),
      'score_frames': int(score.shape[1]),
      'audio_frames': int(trimmed.shape[1]),
      'dtw_cells': int(cells),
      'normalized_cost': round(cost, 4),
      'chroma_shift': int(shift),
      'bars_clamped': int(clamped),
      # how many bars the machinery had to overrule, and how well the recording's
      # length agrees with the score: the two things per-bar confidence rests on
      'bars_forced': int(sum(1 for flag in forced if flag)),
      'duration_ratio': round(float(ratio), 4),
      'expected_duration_s': round(float(expected_s), 3),
      'median_bar_s': round(float(median_bar), 3),
      'min_bar_s': round(float(min(gaps)), 3),
      'max_bar_s': round(float(max(gaps)), 3),
      'worst_jump_s': round(float(np.abs(np.diff(gaps)).max()), 3) if len(gaps) > 1 else 0.0,
      # the unclamped figures, so the correction is visible rather than hidden
      'raw_min_bar_s': round(float(min(raw_gaps)), 3),
      'raw_max_bar_s': round(float(max(raw_gaps)), 3),
      'raw_worst_jump_s': round(float(np.abs(np.diff(raw_gaps)).max()), 3)
      if len(raw_gaps) > 1 else 0.0,
      'seconds_per_bar': round(float(bar_s), 4),
      'score_qpm': round(float(qpm), 2),
      'beats_per_bar': float(beats),
      'trim_start_s': round(float(start_s), 3),
      'trim_end_s': round(float(end_s or duration), 3),
      'confidence_median': round(float(np.median(confidences)), 3) if confidences else 0.0,
  }
  if deviations:
    quality['median_abs_change_vs_linear_s'] = round(float(np.median(deviations)), 3)
    quality['p90_abs_change_vs_linear_s'] = round(float(np.percentile(deviations, 90)), 3)

  _report(progress, 100.0, 'done')
  return SyncResult(method=SyncResult.METHOD_DTW, measures=measures,
                    seconds_per_measure=float(np.median(gaps)) if gaps else bar_s,
                    audio_duration_s=float(duration), offset_s=float(boundaries[0]),
                    quality=quality, warnings=warnings)
