"""Export a piece in the format CyoloScoreFollowing reads, so it can be measured.

`cyolo_score_following` (Henkel & Widmer, Frontiers 2021, MIT) is a real-time score
follower: a small YOLO-style detector, conditioned on the audio by an LSTM, that
predicts note / bar / system bounding boxes **on the score page image**. It would be the
live version of the Sync playing tab -- the page following the audio rather than the
audio following a pre-computed map.

It reads one `.npz` per piece plus the performance beside it as a WAV. Nothing here
depends on their code: this only writes the file, so it can be tested without torch.
Feeding it to their pretrained model needs their conda env and is a separate step (see
the README).

The conventions were read off their loader, `data_utils.load_piece`, and they are not
symmetric -- both facts matter when writing a file for it:

* **sheets** -- `(pages, H, W)`, uint8, 255 is paper, *unpadded*: the loader pads the
  width to a square before the model sees it, and each page is scaled to 416x416.
* **coords** -- `note_x` in the *unpadded* frame, because the loader adds the pad
  itself. Ours are noteheads, from the same engraving the reveal uses.
* **bars / systems** -- in their own files these already include the pad, so their
  loader adds it a second time and their boxes end up outside the image (measured: a
  system reaching x=1348 on a 1181-wide page). We deliberately reproduce that, so our
  piece is fed to the model exactly as their own pieces are; the note class -- the one
  that actually positions the page -- is consistent either way.

Two timings can be exported, and they answer different questions:

* `timing='synthetic'` -- the practice track (the score rendered by FluidSynth at one
  tempo) with onsets from its own exact bar grid. Clean piano and a ground truth that is
  exact, which isolates the *engraving* difference from everything else.
* `timing='recording'` -- the real performance, with onsets interpolated inside each bar
  from the measure map. This is the realistic case, and it also inherits the map's own
  error in the bars where the fit was clamped.
"""

from __future__ import annotations

import json
import pathlib
from typing import Dict, List, Optional, Sequence, Tuple

from .config import AnalyzerConfig


class CyoloError(RuntimeError):
  """Raised when a piece cannot be exported, with a message meant for a person."""


# `coords`, `bars` and `systems` are object arrays of **plain Python dicts**, because that
# is what their loader iterates: `for key in entry: entry[key]` works on a dict and not on
# a structured record (iterating a record yields values, not field names).
#
# The catch: numpy writes an object array with pickle, and numpy 2 renamed `numpy.core` to
# `numpy._core` -- so a file written by this numpy cannot be opened by the numpy 1.x in
# their python 3.7 environment ("No module named 'numpy._core'"). `tools/cyolo_eval.py`
# bridges that with a module alias in the runner, so their code stays untouched. Keep the
# values in these dicts plain Python (float/int/str), never numpy scalars.


def _page_array(path, height: int):
  """One page as grayscale uint8, paper white, padded down to `height`."""
  import numpy as np
  from PIL import Image

  with Image.open(path) as drawn:
    image = drawn.convert('RGBA')
    background = Image.new('RGBA', image.size, (255, 255, 255, 255))
    flat = Image.alpha_composite(background, image).convert('L')
  grey = np.asarray(flat, dtype=np.uint8)
  if grey.shape[0] < height:
    pad = np.full((height - grey.shape[0], grey.shape[1]), 255, dtype=np.uint8)
    grey = np.vstack([grey, pad])
  return grey[:height]


def _bar_timing(cfg: AnalyzerConfig, piece, timing: str):
  """(seconds at the start of a bar, seconds per bar) for the chosen timing."""
  if timing == 'synthetic':
    from . import synth
    grid = synth.grid(cfg, piece)
    if not grid or not grid.get('seconds_per_bar'):
      raise CyoloError('no bar grid for the practice track: build one first')
    per_bar = float(grid['seconds_per_bar'])
    first = int(grid.get('first_bar') or 1)

    def start(bar: int) -> float:
      return (bar - first) * per_bar

    def span(bar: int) -> float:
      return per_bar

    return start, span

  if timing != 'recording':
    raise CyoloError('timing must be "synthetic" or "recording", not %r' % (timing,))
  if piece.sync is None or not piece.sync.result.measures:
    raise CyoloError('no measure map: align this piece, or export the synthetic timing')
  spans: Dict[int, Tuple[float, float]] = {}
  for measure in piece.sync.result.measures:
    spans.setdefault(measure.measure, (measure.start_s, measure.end_s))

  def start(bar: int) -> float:
    found = spans.get(bar)
    if found is None:
      raise CyoloError('bar %d is not in the map' % bar)
    return float(found[0])

  def span(bar: int) -> float:
    found = spans.get(bar)
    if found is None:
      raise CyoloError('bar %d is not in the map' % bar)
    return float(found[1] - found[0])

  return start, span


def export_piece(cfg: AnalyzerConfig, piece, out_dir, timing: str = 'recording',
                 name: Optional[str] = None,
                 progress=None) -> Dict[str, object]:
  """Write `<name>.npz` and `<name>.wav` for one piece.

  Returns:
    A small summary: the paths, and how many pages, bars, systems and notes went in.

  Raises:
    CyoloError: if the piece has no engraving, or (for the recording timing) no map.
  """
  import numpy as np

  from . import scoreview

  if piece is None or piece.musicxml is None:
    raise CyoloError('this piece has no score to engrave')
  render = scoreview.cached_render(cfg, piece.musicxml) or scoreview.render_score(
      cfg, piece.musicxml)
  if not render.pages:
    raise CyoloError('the score has no pages')

  out_dir = pathlib.Path(out_dir)
  out_dir.mkdir(parents=True, exist_ok=True)
  name = name or piece.slug

  def report(percent: float, message: str) -> None:
    if progress is not None:
      progress(percent, message)

  report(5.0, 'reading the pages')
  height = max(page.height for page in render.pages)
  width = max(page.width for page in render.pages)
  sheets = np.stack([_page_array(page.image, height) for page in render.pages])
  pad = (height - width) // 2          # what their loader will add to x

  bars: List[Dict[str, object]] = []
  systems: List[Dict[str, object]] = []
  coords: List[Dict[str, object]] = []
  start_of, span_of = _bar_timing(cfg, piece, timing)

  for page in render.pages:
    report(10.0 + 60.0 * (page.number - 1) / max(1, len(render.pages)),
           'measuring page %d of %d' % (page.number, len(render.pages)))
    reveal = scoreview.page_reveal(cfg, piece.musicxml, page.number, page.width)
    bands = scoreview.line_bands(reveal, page.image)

    # one entry per line of the page, as their system boxes are
    system_index: Dict[int, int] = {}
    for index, _band in enumerate(reveal.systems):
      marks = [m for m in reveal.marks if reveal.system_number(m.measure) == index + 1]
      if not marks:
        continue
      top, bottom = bands[index]
      left = min(m.x0 for m in marks)
      right = max(m.x1 for m in marks)
      system_index[index + 1] = len(systems)
      systems.append({'x': float(left + pad), 'y': float(top),
                      'w': float(right - left), 'h': float(bottom - top),
                      'page_nr': int(page.number - 1)})

    bar_index: Dict[int, int] = {}
    for box in page.measures:
      line = reveal.system_number(box.number)
      if line is None:
        continue
      bar_index[box.number] = len(bars)
      bars.append({'x': float(box.x0 + pad), 'y': float(box.y0),
                   'w': float(box.x1 - box.x0), 'h': float(box.y1 - box.y0),
                   'system_idx': int(system_index.get(line, 0)),
                   'page_nr': int(page.number - 1)})

    # noteheads, in the order the reveal found them, each with its own beat
    for mark in reveal.marks:
      if mark.kind != 'note':
        continue
      units = max(1, reveal.units.get(mark.measure, 1))
      fraction = mark.unit / float(units)
      onset = start_of(mark.measure) + fraction * span_of(mark.measure)
      coords.append({
          'onset': float(onset),
          'pitch': 0.0,                       # their loader never reads it
          'note_x': float((mark.x0 + mark.x1) / 2),      # unpadded: the loader pads
          'note_y': float((mark.y0 + mark.y1) / 2),
          'e_idx': int(len(coords)),
          'system_idx': int(system_index.get(reveal.system_number(mark.measure), 0)),
          'bar_idx': int(bar_index.get(mark.measure, 0)),
          'page_nr': int(page.number - 1),
      })

  report(75.0, 'copying the audio')
  audio = _audio_for(cfg, piece, timing)
  target_wav = out_dir / ('%s.wav' % name)
  target_wav.write_bytes(pathlib.Path(audio).read_bytes())

  report(85.0, 'writing the npz')
  npz_path = out_dir / ('%s.npz' % name)
  np.savez_compressed(
      npz_path,
      sheets=sheets,
      coords=np.array(coords, dtype=object),
      bars=np.array(bars, dtype=object),
      systems=np.array(systems, dtype=object),
      synthesized=np.array(timing == 'synthetic'),
  )
  report(100.0, 'exported %s' % npz_path.name)
  return {'npz': npz_path, 'wav': target_wav, 'pages': len(render.pages),
          'bars': len(bars), 'systems': len(systems), 'notes': len(coords),
          'height': height, 'width': width, 'pad': pad, 'timing': timing}


def _audio_for(cfg: AnalyzerConfig, piece, timing: str):
  """The performance to follow: the recording, or the score rendered at one tempo."""
  if timing == 'synthetic':
    from . import synth
    if not piece.has_score:
      raise CyoloError('no score to render for the synthetic timing')
    return synth.build_track(cfg, piece, 'both')
  if not piece.has_audio:
    raise CyoloError('no recording has been fetched for this piece')
  return piece.audio.path


def main(argv: Optional[Sequence[str]] = None) -> int:
  """`python -m analyzer.cyolo <slug> --out DIR [--timing synthetic|recording]`."""
  import argparse

  from . import library

  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument('slug', help='which piece to export')
  parser.add_argument('--out', default=None, help='where to write (default: cache/cyolo)')
  parser.add_argument('--timing', default='recording', choices=('recording', 'synthetic'))
  parser.add_argument('--name', default=None, help='file name inside --out')
  args = parser.parse_args(argv)

  cfg = AnalyzerConfig()
  piece = library.find(cfg, args.slug)
  if piece is None:
    print('no such piece: %s' % args.slug)
    return 2
  out = pathlib.Path(args.out) if args.out else cfg.cache_dir / 'cyolo'
  summary = export_piece(cfg, piece, out, timing=args.timing, name=args.name,
                         progress=lambda p, m: None)
  print(json.dumps({k: (str(v) if isinstance(v, pathlib.Path) else v)
                    for k, v in summary.items()}, indent=1))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
