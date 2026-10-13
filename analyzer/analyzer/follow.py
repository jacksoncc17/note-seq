"""Following a performance: where in the score are we, right now?

The measure map answers that question *afterwards*: it aligns the whole recording to the
whole score in one pass, and the Sync playing tab then reads bar times out of it. This
module answers it *while it happens* -- the page follows the microphone, at whatever
tempo and rubato is actually being played, and there is no map to drift in the bars where
the fit had to be clamped.

The method is online DTW, and the difference from the offline version we already have is
just what it is allowed to see:

    offline   the whole recording and the whole score, one pass over an N x M matrix,
              then read the path off it (11 million cells for the Chopin)
    online    one frame of audio at a time, a single row of costs kept in a narrow band
              around where we think we are, and the position emitted as it goes

Three things make it work, and each was a decision:

**Chroma, computed the same way on both sides.** The reference is the piece's *practice
track* -- the score rendered by FluidSynth at one tempo -- so the comparison is audio
against audio, which is what `align.py` does offline and for the same reason: a real
piano's spectrum and a piano roll are not comparable. Both sides go through the same
causal transform here (one FFT per hop, mapped to 12 pitch classes and L2-normalised),
because a follower cannot use the smoothed CENS features the offline alignment uses: their
smoothing filter looks at frames that have not happened yet.

**A bounded band, and a cost for unnatural steps.** Each live frame may advance the
reference by 0 to `MAX_ADVANCE` frames, paying a penalty for every step that is not 1:1 --
so the reference is free to run at 0.7x or 1.4x, but not to sprint. That penalty is what
stops the position running away on noise, and it is why the tempo is not assumed.

**The position never goes backwards.** The band only ever extends forwards, so the emitted
position is monotone by construction: a note that has been passed cannot be unplayed.

**What it can and cannot do, measured.** Fed a *rendered* score -- the piece's own practice
track, or that track slowed to 0.7x or sped to 1.3x -- it tracks to within 0.03-0.2 s over
a minute of audio, which is the property that matters: the tempo is not assumed. Fed a
*real piano recording* against that rendering it does **not** track: the two differ by more,
per frame, than the right position differs from a wrong one, so the cost landscape flattens
and the path crawls (measured: 55 bars behind, and no step penalty fixes it -- a big enough
one makes the follower walk the reference at the reference's own tempo instead, which tracks
a tempo not at all). Following a real instrument wants a real reference: with the recording
as its own reference the follower tracks it exactly, 309.4 s reached against 309.5 s of
audio. That is the next piece of work, and it needs the bar timeline from the measure map
rather than from a uniform grid.

It also does not know about repeats (the same music twice attracts the position to whichever
pass it is nearer, which is right when you are playing it), and it cannot find you again
after a jump to an unrelated part of the piece -- that wants a different model, not a better
DTW. `quality` is a plain cosine measure and is *not* a reliable "am I lost" signal: noise
scores 0.90, where silence scores 0.00.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Dict, List, NamedTuple, Optional, Tuple

from .config import AnalyzerConfig


# The analyzer caches every recording at 22050 Hz mono, so the follower listens at the
# same rate rather than resampling.
SAMPLE_RATE = 22050
# One FFT per hop; 1024 samples is 46 ms, about 21 positions a second. Finer than a beat
# at any tempo a person plays, and coarse enough that the work per frame is a single FFT
# and a few dozen dot products.
HOP = 1024
N_FFT = 4096
# How far the reference may advance per live frame, and how far back it may correct.
# A step of 1 is the reference's own tempo; a step of 0 is a held note or a pause; a
# negative step pulls the position back after an overshoot.
MAX_ADVANCE = 6
# How far back the position may move in one frame. Zero by default, and that was measured:
# a backward step invites the path into a degenerate equilibrium where it hovers around a
# well-matching bar instead of following the music -- with 8 frames of it the follower
# tracked half of a 0.7x rendering and then stopped, and with 20 frames it slid back to the
# beginning. It was added to fix an overshoot that turned out to be caused by something
# else entirely (see SPECTRUM_COMPRESSION): with the features fixed, no backtracking tracks
# a 0.7x rendering to within 0.1 s over 40 s.
BACKTRACK = 0
# What a step that is not 1:1 costs. This has to be *small* compared with the differences
# the music itself produces between the right position and the wrong one -- measured on
# this material at around 0.01 -- or the penalty, and not the audio, decides the path. At
# 0.12 (which is what "a modest penalty" looks like in the abstract) the follower simply
# walked the reference at the reference's own tempo, and scored well against a real
# recording only because that performance's average tempo happens to be the reference's.
STEP_PENALTY = 0.02
# A cost for *not* advancing that grows the longer the position stands still. Without it,
# a stretch of audio that matches nothing -- silence, noise -- has a flat cost landscape
# and the position simply stops; with it, the follower falls back on "keep going at the
# reference's own tempo" and says it is unsure (which it does: silence reports quality
# 0.00). It is deliberately small: it must not compete with a real match, or it walks the
# piece faster than the music.
STALL_PRESSURE = 0.004
RATE_MEMORY = 0.05
MIN_RATE, MAX_RATE = 0.6, 1.9
# The adaptive term is *off* by default, and that is a measured decision rather than a
# tidy one: it removes the systematic lag it was written for (a performance 7% faster
# than the reference no longer falls behind), but in a flat cost landscape -- which is
# what a real piano against a rendering gives -- it is a feedback loop that runs away,
# measured at 26 s of reference drift in 85 s of music. The absolute term alone tracks
# tempo changes correctly whenever the match is actually discriminative, which is the case
# for anything rendered, and errs on the side of lagging rather than racing.
RATE_ADAPTIVE = False
RATE_PENALTY = 0.12
# Frames averaged into each comparison frame, on both sides. Three is measurably better
# on a real recording than one (76% of frames land within a bar of the map, against 74%),
# and costs nothing.
SMOOTH_FRAMES = 3
# Quality (1 - cosine distance to the reference at the chosen position) below this counts
# as "not really matching": the page is being driven by something that is not this piece,
# or by nothing at all. It is only reported, never used to gate the position -- see
# `_step` for why an absolute gate was wrong.
UNSURE_QUALITY = 0.4


class FollowError(RuntimeError):
  """Raised when there is nothing to follow, with a message meant for a person."""


@dataclasses.dataclass
class FollowPosition:
  """Where the music has got to, in the score's own terms."""

  reference_s: float                 # seconds into the reference track
  bar: int
  beat: int                          # 1-based, counted in the meter's denominator
  beats_per_bar: int
  bar_start_s: float
  bar_end_s: float
  quality: float                     # 1.0 is a clean match, 0.0 is nothing like it
  frames: int                        # live frames fed so far

  @property
  def unsure(self) -> bool:
    return self.quality < UNSURE_QUALITY


# ---------------------------------------------------------------------------
# chroma, causally
# ---------------------------------------------------------------------------

_FILTERBANK: Dict[Tuple[int, int], object] = {}


def filterbank(rate: int = SAMPLE_RATE, n_fft: int = N_FFT):
  """The 12 x (n_fft/2 + 1) matrix that folds a spectrum into pitch classes."""
  key = (rate, n_fft)
  cached = _FILTERBANK.get(key)
  if cached is None:
    import librosa
    cached = librosa.filters.chroma(sr=rate, n_fft=n_fft)
    _FILTERBANK[key] = cached
  return cached


def _window(n_fft: int = N_FFT):
  import numpy as np
  return np.hanning(n_fft).astype('float32')


# Which transform turns a chunk of audio into the vector that gets compared. The
# difference matters more than any tuning of the DP: the two sides of this comparison are
# a *rendered* score and a *real* piano in a room, and what has to survive that is the
# sequence of chords, not the timbre. Measured both ways -- see the README -- and the
# default is the one that tracked a real recording and a tempo-changed rendering alike.
#
#   plain     the raw chroma, L2-normalised
#   sqrt      the same, square-rooted first: loud partials stop dominating
#   centered  sqrt, with the frame's own mean removed, so a frame says *which* chord
#             rather than *how much* of everything
#   running   centered, divided by a slow causal average -- local contrast, so a quiet
#             passage is not simply a small version of a loud one
#   delta     the change from the previous frame: the chord *progression*, which is the
#             most instrument-independent thing in the signal
FEATURES = ('plain', 'sqrt', 'centered', 'running', 'delta')
DEFAULT_FEATURE = 'plain'
RUNNING_FRAMES = 200              # about 9 s at 46 ms a frame

# Compressing the spectrum before folding it into pitch classes looks like the obvious
# thing to do -- a real piano and a rendering differ much more in the balance of their
# partials than in which notes are played -- and it is exactly backwards. What matters is
# *contrast*: how much cheaper the right position is than a wrong one. Measured on this
# material (median over 40 frames, right position against twelve random ones):
#
#     compression   rendered vs rendered     real recording vs rendered
#     none          0.000 / 0.336  (+0.336)  0.235 / 0.322  (+0.086)
#     log1p(1x)     0.000 / 0.128  (+0.128)  0.100 / 0.114  (+0.014)
#     log1p(10x)    0.000 / 0.052  (+0.052)  0.037 / 0.043  (+0.006)
#     log1p(100x)   0.000 / 0.015  (+0.015)  0.014 / 0.012  (-0.002)
#
# At 100x the right position is not merely less distinct, it is *indistinguishable* from a
# wrong one, and a follower given those features has nothing to follow: it obeys the step
# penalties instead, which is how it came to walk the piece at the reference's tempo while
# claiming to be sure of itself.
SPECTRUM_COMPRESSION = False
COMPRESSION_GAMMA = 1.0


def _feature_frame(raw, feature: str, history: List[object], running: List[object]):
  """One comparison vector from one raw chroma frame, keeping the state it needs."""
  import numpy as np
  value = np.asarray(raw, dtype='float32')
  if feature == 'plain':
    pass
  elif feature == 'sqrt':
    value = np.sqrt(np.maximum(value, 0.0))
  elif feature in ('centered', 'running', 'delta'):
    value = np.sqrt(np.maximum(value, 0.0))
    norm = float(np.linalg.norm(value))
    if norm > 1e-9:
      value = value / norm
    value = value - value.mean()
    if feature == 'running':
      running.append(value)
      if len(running) > RUNNING_FRAMES:
        running.pop(0)
      average = np.mean(running, axis=0)
      value = value - average
    elif feature == 'delta':
      previous = history[-1] if history else np.zeros_like(value)
      value = value - previous
  history.append(value)
  if len(history) > 2:
    history.pop(0)
  norm = float(np.linalg.norm(value))
  return (value / norm).astype('float32') if norm > 1e-9 else np.zeros(12, dtype='float32')


def feature_sequence(raw_chroma, feature: str = DEFAULT_FEATURE):
  """The same transform applied to a whole reference, in one pass."""
  import numpy as np
  if feature == 'plain':
    return raw_chroma.astype('float32')
  history: List[object] = []
  running: List[object] = []
  out = np.zeros_like(raw_chroma, dtype='float32')
  for index in range(raw_chroma.shape[1]):
    out[:, index] = _feature_frame(raw_chroma[:, index], feature, history, running)
  return out


def raw_chroma(window, rate: int = SAMPLE_RATE, n_fft: int = N_FFT,
               compress: Optional[bool] = None):
  """Twelve pitch classes from one window of samples, before any transform."""
  import numpy as np
  spectrum = np.abs(np.fft.rfft(window))
  if SPECTRUM_COMPRESSION if compress is None else compress:
    spectrum = np.log1p(COMPRESSION_GAMMA * spectrum)
  folded = filterbank(rate, n_fft) @ spectrum
  norm = float(np.linalg.norm(folded))
  if norm <= 1e-9:
    return np.zeros(12, dtype='float32')
  return (folded / norm).astype('float32')


def chroma_frame(window, rate: int = SAMPLE_RATE, n_fft: int = N_FFT,
                 feature: str = DEFAULT_FEATURE, history=None, running=None,
                 compress: Optional[bool] = None):
  """One comparison vector from one window of samples.

  Deliberately causal and cheap -- one FFT, no smoothing over frames that have not
  happened -- because it runs once per hop on a live signal.
  """
  return _feature_frame(raw_chroma(window, rate, n_fft, compress), feature,
                        history if history is not None else [],
                        running if running is not None else [])


def raw_sequence(samples, rate: int = SAMPLE_RATE, hop: int = HOP, n_fft: int = N_FFT,
                 compress: Optional[bool] = None):
  """The reference's raw chroma: frame k covers samples `[k*hop, k*hop + n_fft)`.

  Exactly what the live side produces once it has that many samples -- no centering, so
  the two cannot end up a frame apart -- and in one batch: a strided copy, one FFT, one
  12 x (n_fft/2+1) matmul.
  """
  import numpy as np
  if len(samples) < n_fft:
    return np.zeros((12, 0), dtype='float32')
  count = 1 + (len(samples) - n_fft) // hop
  frames = np.lib.stride_tricks.as_strided(
      np.ascontiguousarray(samples, dtype='float32'),
      shape=(count, n_fft),
      strides=(samples.strides[0] * hop, samples.strides[0]))
  spectra = np.abs(np.fft.rfft(frames * _window(n_fft), axis=1)).T     # (bins, count)
  if SPECTRUM_COMPRESSION if compress is None else compress:
    spectra = np.log1p(COMPRESSION_GAMMA * spectra)
  folded = filterbank(rate, n_fft) @ spectra
  norms = np.linalg.norm(folded, axis=0)
  norms[norms <= 1e-9] = 1.0
  return (folded / norms).astype('float32')


def chroma_sequence(samples, rate: int = SAMPLE_RATE, hop: int = HOP,
                    n_fft: int = N_FFT, feature: str = DEFAULT_FEATURE,
                    compress: Optional[bool] = None):
  """The reference's comparison vectors: batched frames, then one pass of the transform,
  so a reference and a live signal cannot be transformed differently."""
  return feature_sequence(raw_sequence(samples, rate, hop, n_fft, compress), feature)


# ---------------------------------------------------------------------------
# the follower
# ---------------------------------------------------------------------------

class OnlineFollower:
  """Follows a performance against one reference track, frame by frame.

  Usage:
    follower = OnlineFollower(reference_chroma, grid)
    for block in audio_blocks:           # 20-50 ms at a time, from a mic or a file
        position = follower.feed(block)
        if position is not None:
            show(position.bar, position.beat)
  """

  def __init__(self, reference_chroma, grid: Dict[str, object],
               hop: int = HOP, rate: int = SAMPLE_RATE, n_fft: int = N_FFT,
               max_advance: int = MAX_ADVANCE, step_penalty: float = STEP_PENALTY,
               backtrack: int = BACKTRACK,
               rate_adaptive: bool = RATE_ADAPTIVE, smooth_frames: int = SMOOTH_FRAMES,
               feature: str = DEFAULT_FEATURE, compress: Optional[bool] = None,
               rate_penalty: float = RATE_PENALTY):
    self.reference = reference_chroma
    self.hop = hop
    self.rate = rate
    self.n_fft = n_fft
    self.max_advance = int(max_advance)
    self.backtrack = int(backtrack)
    self.step_penalty = float(step_penalty)
    self.rate_adaptive = bool(rate_adaptive)
    self.rate_penalty = float(rate_penalty)
    self.smooth_frames = max(1, int(smooth_frames))
    if feature not in FEATURES:
      raise FollowError('unknown feature %r' % (feature,))
    self.feature = feature
    self.compress = compress
    self.window = _window(n_fft)
    self.reference_frames = int(reference_chroma.shape[1])

    self.seconds_per_bar = float(grid.get('seconds_per_bar') or 0.0)
    self.first_bar = int(grid.get('first_bar') or 1)
    self.beats_per_bar = max(1, int(grid.get('beats') or 4))
    if self.seconds_per_bar <= 0:
      raise FollowError('the reference has no bar grid to follow')

    self.reset()

  def reset(self, reference_s: float = 0.0) -> None:
    """Start following from a point in the reference, with nothing else assumed.

    Seeding matters in practice: somebody practising bars 40-43 does not start at bar 1,
    and the band only extends forwards, so the position has to be told where to look.
    """
    import numpy as np
    frame = max(0, min(self.reference_frames - 1,
                       int(round(float(reference_s) * self.rate / self.hop))))
    self._buffer = np.zeros(0, dtype='float32')
    self._costs = np.full(self.reference_frames, np.inf, dtype='float64')
    self._costs[frame] = 0.0
    self._position = frame
    self._frames = 0
    self._quality = 0.0
    self._rate = 1.0
    self._since_advance = 0
    self._recent: List[object] = []
    self._history: List[object] = []
    self._running: List[object] = []

  # ------------------------------------------------------------------ feeding

  def feed(self, samples):
    """Add audio; returns the position after each completed hop (or None)."""
    import numpy as np
    if len(samples) == 0:
      return None
    self._buffer = np.concatenate([self._buffer, np.asarray(samples, dtype='float32')])
    position = None
    while len(self._buffer) >= self.n_fft:
      frame = self._next_frame()
      self._buffer = self._buffer[self.hop:]
      position = self._step(frame)
    return position

  def _next_frame(self):
    """One comparison vector, with a short causal average over the last few frames."""
    import numpy as np
    frame = chroma_frame(self._buffer[:self.n_fft] * self.window, self.rate, self.n_fft,
                         feature=self.feature, history=self._history,
                         running=self._running, compress=self.compress)
    if self.smooth_frames > 1:
      self._recent.append(frame)
      if len(self._recent) > self.smooth_frames:
        self._recent.pop(0)
      averaged = np.mean(self._recent, axis=0)
      norm = float(np.linalg.norm(averaged))
      frame = (averaged / norm if norm > 1e-9 else averaged).astype('float32')
    return frame

  def _step(self, frame: 'object') -> FollowPosition:
    """One row of the online DTW: where could we be now?

    The band runs from `backtrack` frames behind the current position to `max_advance`
    ahead of it, and every position in it is scored from every position in the previous
    band. Both directions matter: forward, because the music may be going faster than the
    reference; backward, because a correction has to be *possible* -- a position that can
    only move forwards can never make up an overshoot, it can only stop making it worse.
    """
    import numpy as np
    self._frames += 1
    lowest = max(0, self._position - self.backtrack)
    highest = min(self.reference_frames - 1, self._position + self.max_advance)
    band = np.arange(lowest, highest + 1)
    if len(band) == 0:
      return self.position()

    # 1 - cosine similarity for L2-normalised vectors: 0 identical, 1 unrelated
    similarity = self.reference[:, band].T @ frame
    local = 1.0 - np.clip(similarity, -1.0, 1.0)

    # arrive at each position from the cheapest place in the previous band, paying for a
    # step that does not look like the music's rate
    previous = self._costs[band]
    rates = self._rate if self.rate_adaptive else 1.0
    best = np.full(len(band), np.inf, dtype='float64')
    best_step = np.zeros(len(band), dtype='int64')
    for step in range(-self.backtrack, self.max_advance + 1):
      index = np.arange(len(band)) - step               # source position for this step
      inside = (index >= 0) & (index < len(band))
      if not inside.any():
        continue
      penalty = self.step_penalty * abs(step - 1)
      if step <= 0:
        penalty += STALL_PRESSURE * self._since_advance
      if self.rate_adaptive:
        penalty += self.rate_penalty * abs(step - rates)
      # the frame's own cost belongs to the position we are arriving at
      candidate = np.full(len(band), np.inf, dtype='float64')
      candidate[inside] = previous[index[inside]] + local[inside] + penalty
      better = candidate < best
      best[better] = candidate[better]
      best_step[better] = step

    chosen_offset = int(np.argmin(best))
    chosen = int(band[chosen_offset])
    step_taken = int(best_step[chosen_offset])
    self._costs = np.full(self.reference_frames, np.inf, dtype='float64')
    self._costs[band] = best
    self._since_advance = 0 if chosen > self._position else self._since_advance + 1
    self._position = chosen
    self._rate = max(MIN_RATE, min(MAX_RATE, (1.0 - RATE_MEMORY) * self._rate
                                             + RATE_MEMORY * step_taken))
    self._quality = float(max(0.0, 1.0 - local[chosen_offset]))
    return self.position()

  # ------------------------------------------------------------------ reading

  def position(self) -> FollowPosition:
    seconds = self._position * self.hop / float(self.rate)
    index = int(seconds / self.seconds_per_bar)
    bar = self.first_bar + index
    within = seconds - index * self.seconds_per_bar
    beat = 1 + int(within / self.seconds_per_bar * self.beats_per_bar)
    beat = max(1, min(self.beats_per_bar, beat))
    return FollowPosition(
        reference_s=seconds, bar=bar, beat=beat, beats_per_bar=self.beats_per_bar,
        bar_start_s=index * self.seconds_per_bar,
        bar_end_s=(index + 1) * self.seconds_per_bar,
        quality=self._quality, frames=self._frames)

  @property
  def reference_position(self) -> int:
    return self._position


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------

def _track_stamp(path) -> str:
  import hashlib
  stat = pathlib.Path(path).stat()
  return hashlib.sha1(('%d:%f' % (stat.st_size, stat.st_mtime)).encode('utf-8')).hexdigest()[:8]


def reference_for(cfg: AnalyzerConfig, piece, track: str = 'both',
                  feature: str = DEFAULT_FEATURE, compress: Optional[bool] = None):
  """(chroma, grid, track path) for a piece: the score, as audio to follow.

  The chroma is remembered on disk next to the track it came from, because it costs about
  a second to compute and the following itself needs it instantly.
  """
  import numpy as np
  import soundfile as sf

  from . import synth

  if piece is None or piece.musicxml is None:
    raise FollowError('this piece has no score to follow')
  path = pathlib.Path(synth.build_track(cfg, piece, track))
  grid = synth.grid(cfg, piece)
  if not grid:
    raise FollowError('this piece has no bar grid: build its practice track first')

  cache_dir = cfg.cache_dir / 'follow'
  cache_dir.mkdir(parents=True, exist_ok=True)
  compress = SPECTRUM_COMPRESSION if compress is None else compress
  cache = cache_dir / ('%s-%s-%s-%s-%s.npz' % (piece.slug, track, feature,
                                               int(bool(compress)), _track_stamp(path)))
  if cache.is_file():
    try:
      stored = np.load(str(cache))
      return stored['chroma'], grid, path
    except Exception:
      pass

  data, rate = sf.read(str(path), dtype='float32', always_2d=False)
  if getattr(data, 'ndim', 1) > 1:
    data = data.mean(axis=1)
  if rate != SAMPLE_RATE:
    from .align import _resample
    data = _resample(data, rate, SAMPLE_RATE)
  chroma = chroma_sequence(data, SAMPLE_RATE, feature=feature, compress=compress)
  try:
    np.savez_compressed(str(cache), chroma=chroma)
  except OSError:
    pass                    # a cache that cannot be written is not a failure
  return chroma, grid, path


# ---------------------------------------------------------------------------
# sources: the same path for a file (tests, and trying it without a mic) and a mic
# ---------------------------------------------------------------------------

def follow_file(follower: OnlineFollower, path, block: int = 1024,
                real_time: bool = False, duration: Optional[float] = None,
                progress=None) -> List[FollowPosition]:
  """Feed a WAV through the follower as if it were arriving live.

  `real_time=True` paces the blocks to wall-clock, which is what makes a file usable as a
  stand-in for a microphone (in the app, and in a live smoke test).
  """
  import time

  import soundfile as sf

  data, rate = sf.read(str(path), dtype='float32', always_2d=False)
  if getattr(data, 'ndim', 1) > 1:
    data = data.mean(axis=1)
  if rate != follower.rate:
    from .align import _resample
    data = _resample(data, rate, follower.rate)
  if duration is not None:
    data = data[:int(duration * follower.rate)]

  positions: List[FollowPosition] = []
  started = time.perf_counter()
  for start in range(0, len(data), block):
    chunk = data[start:start + block]
    position = follower.feed(chunk)
    if position is not None:
      positions.append(position)
    if real_time:
      target = (start + len(chunk)) / float(follower.rate)
      behind = target - (time.perf_counter() - started)
      if behind > 0:
        time.sleep(behind)
    if progress is not None and start % (block * 40) == 0:
      progress(start / float(len(data)), 'listening')
  return positions
