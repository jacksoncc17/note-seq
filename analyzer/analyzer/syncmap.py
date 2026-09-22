"""Reading and writing the measure -> time map on disk.

One JSON file per piece, next to nothing else, so the map can be inspected,
diffed, hand-edited or deleted without touching the app. It carries its own
provenance: which recording it was fitted to, which score, and how it was made.
That matters because a map is only as good as the recording it came from -- if
the audio is replaced, the old map must be recognised as stale rather than
silently mis-pointing at the wrong bars.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import schema
from .config import AnalyzerConfig
from .schema import AudioSource, MeasureTime, SyncResult


SYNC_VERSION = 1


@dataclass
class StoredSync:
  """A `SyncResult` plus the provenance needed to trust or distrust it."""

  slug: str
  result: SyncResult
  audio: Dict[str, Any] = field(default_factory=dict)
  source_musicxml: str = ''
  source_size: int = 0
  source_mtime: float = 0.0
  played: List[int] = field(default_factory=list)
  updated_at: str = ''
  version: int = SYNC_VERSION

  @property
  def method(self) -> str:
    return self.result.method

  @property
  def is_trusted(self) -> bool:
    return self.result.is_trusted

  def to_dict(self) -> Dict[str, Any]:
    out = self.result.to_dict()
    out.update({
        'version': SYNC_VERSION,
        'slug': self.slug,
        'audio': self.audio,
        'source_musicxml': self.source_musicxml,
        'source_size': self.source_size,
        'source_mtime': self.source_mtime,
        'played': self.played,
        'updated_at': self.updated_at,
    })
    return out

  @classmethod
  def from_dict(cls, data: Dict[str, Any]) -> 'StoredSync':
    return cls(
        slug=data.get('slug', ''),
        result=SyncResult.from_dict(data),
        audio=data.get('audio') or {},
        source_musicxml=data.get('source_musicxml', ''),
        source_size=int(data.get('source_size') or 0),
        source_mtime=float(data.get('source_mtime') or 0.0),
        played=list(data.get('played') or []),
        updated_at=data.get('updated_at', ''),
        version=int(data.get('version') or SYNC_VERSION))


def audio_fingerprint(path) -> Dict[str, Any]:
  """Size, mtime and a content hash for a recording.

  The hash is what makes staleness detection honest: re-downloading a different
  performance of the same piece produces a same-named file, and a map fitted to
  the old one would be silently wrong.
  """
  path = pathlib.Path(path)
  digest = hashlib.sha256()
  try:
    with path.open('rb') as handle:
      for chunk in iter(lambda: handle.read(1024 * 1024), b''):
        digest.update(chunk)
    stat = path.stat()
  except OSError:
    return {}
  return {'path': str(path), 'size': stat.st_size, 'sha256': digest.hexdigest()}


def save(cfg: AnalyzerConfig, slug: str, result: SyncResult, *,
         audio: Optional[AudioSource] = None,
         source_musicxml=None,
         played: Optional[List[int]] = None) -> pathlib.Path:
  """Write the map atomically and return where it went.

  Atomic because a half-written map is worse than none: the app would load it,
  find no measures, and look like it had lost the user's corrections.
  """
  cfg.ensure_dirs()
  source = pathlib.Path(source_musicxml) if source_musicxml else None
  audio_info: Dict[str, Any] = {}
  if audio is not None:
    audio_info = audio.to_dict()
    audio_info.update(audio_fingerprint(audio.path))
  stored = StoredSync(
      slug=slug, result=result, audio=audio_info,
      source_musicxml=str(source) if source else '',
      source_size=source.stat().st_size if source and source.is_file() else 0,
      source_mtime=source.stat().st_mtime if source and source.is_file() else 0.0,
      played=list(played or []),
      updated_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))

  path = cfg.sync_path(slug)
  tmp = path.with_suffix('.json.tmp')
  tmp.write_text(json.dumps(stored.to_dict(), indent=2, sort_keys=False), encoding='utf-8')
  os.replace(tmp, path)
  return path


def load(cfg: AnalyzerConfig, slug: str) -> Optional[StoredSync]:
  """Load the map for `slug`, or None when there is none we can read."""
  path = cfg.sync_path(slug)
  if not path.is_file():
    return None
  try:
    data = json.loads(path.read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return None
  if not isinstance(data, dict) or not data.get('measures'):
    return None
  try:
    return StoredSync.from_dict(data)
  except (TypeError, ValueError):
    return None


def staleness(cfg: AnalyzerConfig, stored: StoredSync,
              audio_path=None, musicxml_path=None) -> Optional[str]:
  """Explain why a stored map may no longer fit, or None when it still does.

  Args:
    cfg: paths.
    stored: the map that was loaded.
    audio_path: the recording in use now; defaults to the one in the store.
    musicxml_path: the score in use now; defaults to the one in the store.

  Returns:
    A short user-facing sentence, e.g. 'the recording has changed since this
    map was made', or None.
  """
  audio_now = pathlib.Path(audio_path or stored.audio.get('path') or '')
  if audio_now and audio_now.is_file():
    old_size = stored.audio.get('size')
    if old_size and audio_now.stat().st_size != old_size:
      return ('the recording has changed since this map was made '
              '(different file size)')
    old_hash = stored.audio.get('sha256')
    if old_hash:
      now = audio_fingerprint(audio_now).get('sha256')
      if now and now != old_hash:
        return 'the recording has changed since this map was made'

  score_now = pathlib.Path(musicxml_path or stored.source_musicxml or '')
  if score_now and score_now.is_file() and stored.source_mtime:
    if abs(score_now.stat().st_mtime - stored.source_mtime) > 1.0:
      return 'the score file was modified after this map was made'
  return None


def delete(cfg: AnalyzerConfig, slug: str) -> bool:
  """Remove the stored map. Returns True when a file was removed."""
  path = cfg.sync_path(slug)
  try:
    path.unlink()
    return True
  except FileNotFoundError:
    return False
  except OSError:
    return False


def summary_line(stored: StoredSync) -> str:
  """One line describing a map, for a label or a log."""
  result = stored.result
  parts = ['%s: %d measures' % (result.method, len(result.measures)),
           '%.2f s/bar' % result.seconds_per_measure]
  if result.quality.get('median_abs_change_vs_linear_s') is not None:
    parts.append('moved %.2f s from the average-tempo guess'
                 % result.quality['median_abs_change_vs_linear_s'])
  return ', '.join(parts)


def uniform_map(played: List[int], duration_s: float, lead_in_s: float = 0.0,
                tail_s: float = 0.0) -> SyncResult:
  """A map that just spreads the recording evenly over the measures.

  This is the one map the app can always produce, with no analysis at all, so it
  is defined here rather than in the aligner: it keeps "give me something to drag"
  working even if alignment is unavailable or fails. It is a *guess* and says so
  in its warnings -- measured against a real performance it is one to two seconds
  wrong per bar, which is fine for roughly this region and useless for exactly
  this bar.

  Args:
    played: printed measure numbers in performance order.
    duration_s: length of the recording.
    lead_in_s: seconds before the music starts, kept outside the division.
    tail_s: seconds after the music ends, likewise.

  Returns:
    A `SyncResult` with method 'linear'.

  Raises:
    ValueError: when there are no measures or the span is not positive.
  """
  count = len(played)
  if count == 0:
    raise ValueError('no measures to map')
  span = float(duration_s) - float(lead_in_s) - float(tail_s)
  if span <= 0:
    raise ValueError('the recording is shorter than the lead-in and tail')

  step = span / count
  measures = []
  for index, number in enumerate(played):
    start = lead_in_s + index * step
    measures.append(MeasureTime(measure=int(number), play_index=index,
                                start_s=start, end_s=start + step))
  return SyncResult(
      method=SyncResult.METHOD_LINEAR, measures=measures,
      seconds_per_measure=step, audio_duration_s=float(duration_s),
      offset_s=float(lead_in_s),
      warnings=['this map spreads the recording evenly over the bars; it is an '
                'average-tempo guess, not a measurement of the performance'])


def check_plausibility(result: SyncResult) -> List[str]:
  """Report map shapes that cannot describe a real performance.

  A measure boundary that does not advance, or a bar that lasts a tenth of the
  median, means the fit went wrong locally even if its overall trend is right.
  Checking here rather than inside the aligner means *every* map is validated --
  including ones loaded from disk or hand-edited -- and the app can say so
  instead of drawing markers a pianist would immediately distrust.

  Args:
    result: the map to inspect.

  Returns:
    A list of short, user-facing sentences; empty when the map looks sane.
  """
  problems: List[str] = []
  measures = sorted(result.measures, key=lambda m: m.play_index)
  if not measures:
    return ['the map is empty']

  durations = [m.duration_s for m in measures]
  positive = sorted(d for d in durations if d > 0)
  if positive:
    median = positive[len(positive) // 2]
    if median > 0:
      tiny = [m.measure for m in measures if 0 < m.duration_s < 0.3 * median]
      zero = [m.measure for m in measures if m.duration_s <= 0]
      if zero:
        problems.append('the map gives %d bar(s) no time at all (first: bar %s)'
                        % (len(zero), zero[0]))
      if tiny:
        problems.append('%d bar(s) are far shorter than the rest (first: bar %s)'
                        % (len(tiny), tiny[0]))

  for before, after in zip(measures, measures[1:]):
    if after.start_s < before.start_s - 1e-9:
      problems.append('the markers are out of order at bar %s' % after.measure)
      break
  for m in measures:
    if m.end_s < m.start_s - 1e-9:
      problems.append('bar %s ends before it starts' % m.measure)
      break

  if len(durations) > 2:
    jumps = [abs(b - a) for a, b in zip(durations, durations[1:])]
    if jumps and max(jumps) > 2.0:
      problems.append('the bar length swings by %.1f s in one step, which usually '
                      'means the fit slipped' % max(jumps))
  return problems
