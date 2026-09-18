"""A small energy + zero-crossing VAD.

Not state of the art (a real deployment would use Silero VAD or webrtcvad), but
it is dependency-light, deterministic, and enough to exercise the turn-taking
logic in tests. Written against numpy rather than the removed-in-3.13 `audioop`
module so it keeps working on current Pythons.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .base import AudioFrame


def _samples(pcm: bytes) -> np.ndarray:
  if not pcm:
    return np.zeros(0, dtype=np.float32)
  usable = len(pcm) - (len(pcm) % 2)
  return np.frombuffer(pcm[:usable], dtype='<i2').astype(np.float32) / 32768.0


def rms(pcm: bytes) -> float:
  data = _samples(pcm)
  return float(np.sqrt(np.mean(data ** 2))) if data.size else 0.0


def zero_crossing_rate(pcm: bytes) -> float:
  data = _samples(pcm)
  if data.size < 2:
    return 0.0
  return float(np.mean(np.signbit(data[1:]) != np.signbit(data[:-1])))


def dbfs_of(pcm: bytes) -> float:
  level = rms(pcm)
  return 20 * math.log10(level) if level > 0 else -120.0


def energy_stats(pcm: bytes) -> dict:
  """Level summary plus a blunt verdict, for calibrating a microphone."""
  data = _samples(pcm)
  if data.size == 0:
    return {'rms': 0.0, 'peak': 0.0, 'clip_ratio': 0.0, 'verdict': 'no audio'}
  rms_level = float(np.sqrt(np.mean(data ** 2)))
  peak = float(np.abs(data).max())
  clip = float(np.mean(np.abs(data) > 0.99))
  if rms_level < 0.005:
    verdict = 'too quiet for reliable ASR - raise the input gain'
  elif clip > 0.01:
    verdict = 'clipping - lower the input gain'
  elif rms_level > 0.3:
    verdict = 'very hot but usable'
  else:
    verdict = 'healthy level'
  return {'rms': rms_level, 'peak': peak, 'clip_ratio': clip,
          'verdict': verdict}


class EnergyVad:
  def __init__(self, energy_threshold: float = 0.012,
               hangover_ms: int = 300, frame_ms: int = 20,
               zcr_threshold: float = 0.35):
    self.energy_threshold = energy_threshold
    self.hangover_frames = max(1, hangover_ms // max(frame_ms, 1))
    self.zcr_threshold = zcr_threshold
    self._speaking = False
    self._quiet_frames = 0
    self.frames_seen = 0

  def _is_speech(self, frame: AudioFrame) -> bool:
    if not frame.pcm:
      return False
    if rms(frame.pcm) < self.energy_threshold:
      return False
    # Voiced speech is lowish in zero crossings; broadband noise is not.
    return zero_crossing_rate(frame.pcm) <= self.zcr_threshold

  @property
  def speaking(self) -> bool:
    """Whether the VAD currently considers the signal to be speech."""
    return self._speaking

  def push(self, frame: AudioFrame) -> Optional[str]:
    self.frames_seen += 1
    speech = self._is_speech(frame)
    if speech:
      self._quiet_frames = 0
      if not self._speaking:
        self._speaking = True
        return 'speech_start'
      return None
    if self._speaking:
      self._quiet_frames += 1
      if self._quiet_frames >= self.hangover_frames:
        self._speaking = False
        self._quiet_frames = 0
        return 'speech_end'
    return None

  # The pitch channel is a different signal with different thresholds; keeping
  # the calculation explicit stops the two paths from being conflated later.
  @staticmethod
  def dbfs(pcm: bytes) -> float:
    return dbfs_of(pcm)
