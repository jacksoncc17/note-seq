"""Where the analyzer keeps things, and how to override that.

Every path is derived from the checkout this file lives in, so the app works
from any working directory, and every one is overridable by an environment
variable so tests can point at a temporary tree.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass


ROOT = pathlib.Path(__file__).resolve().parents[2]      # the note-seq checkout
PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
ANALYZER_DIR = PACKAGE_DIR.parent                       # analyzer/


def _path(env: str, default: pathlib.Path) -> pathlib.Path:
  raw = os.environ.get(env)
  return pathlib.Path(raw).expanduser() if raw else default


@dataclass
class AnalyzerConfig:
  """Filesystem layout, with the question-bank layout of `memorization/`.

  `pieces/` is shared with the quiz and tutor tools on purpose: a piece that has
  been analysed once is available to all three.
  """

  root: pathlib.Path = ROOT
  cache_dir: pathlib.Path = _path('ANALYZER_CACHE', ANALYZER_DIR / 'cache')
  output_dir: pathlib.Path = _path('ANALYZER_OUTPUT', ANALYZER_DIR / 'output')
  bank_dir: pathlib.Path = _path('ANALYZER_BANKS', ROOT / 'memorization' / 'output' / 'pieces')
  legacy_bank: pathlib.Path = ROOT / 'memorization' / 'output' / 'fingerprint.json'
  sheet_music_dir: pathlib.Path = _path('ANALYZER_SHEET_DIR', pathlib.Path.home() / 'sheet_music')

  # Audio is cached rather than streamed: yt-dlp is slow, and both alignment and
  # playback want random access to decoded samples.
  target_sample_rate: int = 22050
  target_channels: int = 1

  def ensure_dirs(self) -> None:
    for d in (self.audio_dir, self.sync_dir, self.render_dir):
      d.mkdir(parents=True, exist_ok=True)

  @property
  def audio_dir(self) -> pathlib.Path:
    """Cached recordings, one WAV plus one JSON of metadata per piece."""
    return self.cache_dir / 'audio'

  @property
  def sync_dir(self) -> pathlib.Path:
    """Measure -> time maps, one JSON per piece."""
    return self.output_dir / 'sync'

  @property
  def render_dir(self) -> pathlib.Path:
    """Score renders and other derived pictures."""
    return self.output_dir / 'render'

  def audio_path(self, slug: str) -> pathlib.Path:
    return self.audio_dir / ('%s.wav' % slug)

  def audio_meta_path(self, slug: str) -> pathlib.Path:
    return self.audio_dir / ('%s.json' % slug)

  def sync_path(self, slug: str) -> pathlib.Path:
    return self.sync_dir / ('%s.json' % slug)


CFG = AnalyzerConfig()
