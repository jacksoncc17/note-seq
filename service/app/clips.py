"""Cutting a piece of audio, and turning a range of bars into seconds.

Both of these live in the analyzer, and the desktop app shares them -- but this service
is also deployed on its own, serving finished pieces to a browser, and that half has no
business pulling in Verovio, librosa, FluidSynth and the rest. Measured: the serving
environment is **109 MB over 20 packages** (fastapi, uvicorn, python-multipart,
soundfile, numpy and their dependencies), where the analyzer's own stack is another
gigabyte on top. So the two functions the page needs at request time live here, and the
tests check them against the analyzer's versions so the two cannot drift apart silently.
"""

from __future__ import annotations

import io
from typing import Any, Dict, Optional, Tuple


def slice_file(path, start_s: float,
               end_s: float) -> Tuple[Optional[bytes], Optional[str]]:
  """`[start_s, end_s)` of an audio file as a standalone WAV.

  The same behaviour as `analyzer.server.slice_file`, which is what the desktop app and
  the phone client use: the range is clamped to the file, the source's sample format is
  kept when it is one of the plain PCM ones, and an empty range is an error rather than
  an empty file.

  Returns:
    (wav bytes, None) or (None, a message to show the caller).
  """
  import soundfile as sf

  path = str(path)
  try:
    info = sf.info(path)
  except Exception as e:
    return None, 'could not read the audio: %s' % e

  duration = float(info.frames) / float(info.samplerate)
  start_s = max(0.0, min(float(start_s), duration))
  end_s = max(0.0, min(float(end_s), duration))
  if end_s <= start_s:
    return None, 'the requested range is empty (%.2f s to %.2f s)' % (start_s, end_s)

  first = int(round(start_s * info.samplerate))
  last = int(round(end_s * info.samplerate))
  try:
    data, rate = sf.read(path, dtype='float32', start=first, stop=last, always_2d=False)
  except Exception as e:
    return None, 'could not read that part of the audio: %s' % e
  if len(data) == 0:
    return None, 'that range contains no audio'

  buffer = io.BytesIO()
  subtype = info.subtype if info.subtype in ('PCM_16', 'PCM_24', 'FLOAT') else 'PCM_16'
  sf.write(buffer, data, rate, format='WAV', subtype=subtype)
  return buffer.getvalue(), None


def range_for_bars(map_dict: Dict[str, Any], first: int, last: int
                   ) -> Optional[Tuple[float, float]]:
  """`(start_s, end_s)` covering bars `first`..`last` inclusive, from the map.

  The map's own rule, not a guess about tempo: the passage starts where the earlier of
  the two bars starts and ends where the later one ends, so playing it sounds the whole
  passage. A printed bar that a repeat plays twice maps to its first pass, which is what
  the desktop app and this service's own page both do when you click a bar twice over.

  Returns None when neither end of the range is in the map.
  """
  measures = (map_dict or {}).get('measures') or []
  if not measures:
    return None

  def find(bar: int) -> Optional[Dict[str, Any]]:
    for row in measures:
      try:
        number = int(row.get('measure'))
      except (TypeError, ValueError):
        continue
      if number == int(bar):
        return row
    return None

  lo, hi = find(first), find(last)
  if lo is None and hi is None:
    return None
  lo = lo if lo is not None else hi
  hi = hi if hi is not None else lo
  start = min(float(lo['start_s']), float(hi['start_s']))
  end = max(float(lo['end_s']), float(hi['end_s']))
  return (start, end)
