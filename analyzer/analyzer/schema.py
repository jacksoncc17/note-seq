"""Data types shared between the analyzer's modules.

Kept deliberately small and dependency-free (stdlib only) so every other module
-- and the tests -- can import it without pulling in Qt, numpy or yt-dlp.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _fields(cls) -> set:
  return {f.name for f in dataclasses.fields(cls)}


@dataclass(frozen=True)
class AudioSource:
  """A recording on disk, identified by the piece it belongs to.

  `source_id` is the provider's own id (a YouTube video id, say) so the same
  recording can be recognised again without re-downloading.
  """

  slug: str
  path: str
  url: str = ''
  source_id: str = ''
  title: str = ''
  duration_s: float = 0.0
  sample_rate: int = 0
  channels: int = 0
  fetched_at: str = ''

  def to_dict(self) -> Dict[str, Any]:
    return dataclasses.asdict(self)

  @classmethod
  def from_dict(cls, data: Dict[str, Any]) -> 'AudioSource':
    return cls(**{k: v for k, v in data.items() if k in _fields(cls)})


@dataclass
class MeasureTime:
  """Where one measure sits in the recording.

  `measure` is the number printed in the score. `play_index` is the position in
  the *performance*: with repeats the two differ, because e.g. measure 8 may be
  played at position 8 and again at position 40.
  """

  measure: int
  play_index: int
  start_s: float
  end_s: float
  confidence: Optional[float] = None

  @property
  def duration_s(self) -> float:
    return max(0.0, self.end_s - self.start_s)

  def to_dict(self) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        'measure': self.measure,
        'play_index': self.play_index,
        'start_s': round(self.start_s, 3),
        'end_s': round(self.end_s, 3),
    }
    if self.confidence is not None:
      out['confidence'] = round(self.confidence, 3)
    return out

  @classmethod
  def from_dict(cls, data: Dict[str, Any]) -> 'MeasureTime':
    return cls(**{k: v for k, v in data.items() if k in _fields(cls)})


@dataclass
class SyncResult:
  """A complete measure -> time mapping for one recording.

  `method` records how it was produced, because that is the whole point of the
  app: a `linear` map is a guess from the average tempo, a `dtw` map has been
  fitted to the actual performance, and a `manual` map is one the user has
  corrected by hand. Never present an unrefined map as if it were measured.
  """

  method: str
  measures: List[MeasureTime] = field(default_factory=list)
  seconds_per_measure: float = 0.0
  audio_duration_s: float = 0.0
  offset_s: float = 0.0
  quality: Dict[str, Any] = field(default_factory=dict)
  warnings: List[str] = field(default_factory=list)

  METHOD_LINEAR = 'linear'
  METHOD_DTW = 'dtw'
  METHOD_MANUAL = 'manual'

  def __len__(self) -> int:
    return len(self.measures)

  @property
  def is_trusted(self) -> bool:
    """True when a human has looked at this mapping."""
    return self.method in (self.METHOD_DTW, self.METHOD_MANUAL)

  def time_for_measure(self, measure: int, play_index: Optional[int] = None):
    """Return the MeasureTime for a printed measure number, or None.

    When a measure is played more than once, `play_index` picks the pass; with
    no play_index the first pass wins.
    """
    found = None
    for m in self.measures:
      if m.measure != measure:
        continue
      if play_index is None:
        return m
      if m.play_index == play_index:
        return m
      found = found or m
    return found if play_index is not None else None

  def range_for_measures(self, first: int, last: int,
                         play_index: Optional[int] = None):
    """Return (start_s, end_s) covering measures `first`..`last` inclusive.

    Returns None when neither end of the range is known. The range is taken to
    end where the last measure ends, so playing it sounds the whole passage.
    """
    lo = self.time_for_measure(first, play_index)
    hi = self.time_for_measure(last, play_index)
    if lo is None and hi is None:
      return None
    if lo is None:
      lo = hi
    if hi is None:
      hi = lo
    start = min(lo.start_s, hi.start_s)
    end = max(lo.end_s, hi.end_s)
    return (start, end)

  def to_dict(self) -> Dict[str, Any]:
    return {
        'version': 1,
        'method': self.method,
        'seconds_per_measure': round(self.seconds_per_measure, 4),
        'audio_duration_s': round(self.audio_duration_s, 3),
        'offset_s': round(self.offset_s, 3),
        'quality': self.quality,
        'warnings': list(self.warnings),
        'measures': [m.to_dict() for m in self.measures],
    }

  @classmethod
  def from_dict(cls, data: Dict[str, Any]) -> 'SyncResult':
    known = _fields(cls) - {'measures'}
    kwargs = {k: v for k, v in data.items() if k in known}
    kwargs['measures'] = [MeasureTime.from_dict(m) for m in data.get('measures', [])]
    return cls(**kwargs)
