"""Turn a stream of microphone frames into utterances.

Kept separate from capture so the segmentation policy is unit-testable with
synthetic audio: a pre-roll so the first consonant is not clipped, a hangover so
a breath mid-phrase does not end the turn, and a cap so a stuck stream cannot
buffer without bound.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .base import AudioFrame
from .vad_energy import EnergyVad, rms


@dataclass
class Utterance:
  pcm: bytes
  duration_s: float
  peak: float
  frame_count: int


class UtteranceSegmenter:
  """Feeds frames through a VAD and emits complete utterances."""

  def __init__(self, energy_threshold: float = 0.012,
               hangover_ms: int = 300, frame_ms: int = 20,
               pre_roll_ms: int = 300, min_utterance_ms: int = 250,
               max_utterance_s: float = 20.0):
    self.frame_ms = frame_ms
    self.vad = EnergyVad(energy_threshold=energy_threshold,
                         hangover_ms=hangover_ms, frame_ms=frame_ms)
    self.pre_roll_frames = max(0, pre_roll_ms // frame_ms)
    self.min_frames = max(1, min_utterance_ms // frame_ms)
    self.max_frames = int(max_utterance_s * 1000 / frame_ms)
    self._pre: List[bytes] = []
    self._buf: List[bytes] = []
    self._peak = 0.0
    self._active = False

  @property
  def active(self) -> bool:
    return self._active

  def push(self, frame: AudioFrame) -> List[Tuple[str, Optional[Utterance]]]:
    """Returns events: ('speech_start', None), ('speech_end', None) and
    ('utterance', Utterance). A rejected burst (too short) emits no utterance
    event at all, so callers never have to filter `None` payloads out."""
    events: List[Tuple[str, Optional[Utterance]]] = []
    level = rms(frame.pcm)
    self._peak = max(self._peak, level)
    marker = self.vad.push(frame)

    if marker == 'speech_start':
      self._active = True
      self._buf = list(self._pre)      # pre-roll: keep the attack of the word
      self._pre = []
      self._peak = level
      events.append(('speech_start', None))

    if self._active:
      # Cut BEFORE appending, so a monologue becomes consecutive utterances
      # instead of one unbounded buffer (and capture continues seamlessly).
      if len(self._buf) >= self.max_frames:
        utterance = self._finish()
        if utterance is not None:
          events.append(('utterance', utterance))
        self._active = True
        self._peak = level
      self._buf.append(frame.pcm)
    else:
      self._pre.append(frame.pcm)
      if len(self._pre) > self.pre_roll_frames:
        self._pre.pop(0)

    if marker == 'speech_end' and self._active:
      events.append(('speech_end', None))
      utterance = self._finish()
      if utterance is not None:
        events.append(('utterance', utterance))
    return events

  def _finish(self) -> Optional[Utterance]:
    pcm = b''.join(self._buf)
    frames = len(self._buf)
    self._active = False
    self._buf = []
    peak, self._peak = self._peak, 0.0
    # Too short to be speech: usually a click, a chair, or a page turn.
    if frames < self.min_frames:
      return None
    return Utterance(pcm=pcm, duration_s=frames * self.frame_ms / 1000.0,
                     peak=peak, frame_count=frames)

  def reset(self) -> None:
    """Drop all state, e.g. after the tutor finished speaking.

    Without acoustic echo cancellation, anything captured while the tutor talks
    is its own voice. Resetting gives the student a clean start rather than a
    buffer that begins mid-word of the tutor's sentence.
    """
    self.vad = EnergyVad(energy_threshold=self.vad.energy_threshold,
                         hangover_ms=self.vad.hangover_frames * self.frame_ms,
                         frame_ms=self.frame_ms,
                         zcr_threshold=self.vad.zcr_threshold)
    self._pre = []
    self._buf = []
    self._peak = 0.0
    self._active = False

  def flush(self) -> Optional[Utterance]:
    """Emit whatever is buffered (end of stream / Ctrl-C)."""
    if self._active and self._buf:
      return self._finish()
    return None
