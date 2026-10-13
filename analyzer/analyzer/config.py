"""Where the analyzer keeps things, and how to override that.

Every path is derived from the checkout this file lives in, so the app works
from any working directory, and every one is overridable by an environment
variable so tests can point at a temporary tree.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field


ROOT = pathlib.Path(__file__).resolve().parents[2]      # the note-seq checkout
PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
ANALYZER_DIR = PACKAGE_DIR.parent                       # analyzer/


def _path(env: str, default: pathlib.Path) -> pathlib.Path:
  raw = os.environ.get(env)
  return pathlib.Path(raw).expanduser() if raw else default


def _flag(env: str, default: bool = True) -> bool:
  """A boolean from the environment: anything but a clear no means yes.

  Read when a config is built rather than when this module is imported, so a test
  (or a shell that exports the variable late) gets what it asked for.
  """
  raw = os.environ.get(env)
  if raw is None:
    return default
  return raw.strip().lower() not in ('0', 'no', 'off', 'false')


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

  # Which storage adapter `store.open_repository` should build. Changing this to
  # 'sqlite' (or anything else registered there) is the whole of a database move.
  backend: str = os.environ.get('ANALYZER_BACKEND', 'filesystem')

  # Listening to the piano and revealing the page from what is heard -- the online
  # follower. It is an addition, not a foundation: with `ANALYZER_FOLLOW=0` the Sync
  # tab simply has no such controls, nothing imports a microphone, and every other
  # way of revealing the page (the timer, the map, the dials) is untouched. Set the
  # variable to anything that reads as no: 0, no, off, false.
  follow_enabled: bool = field(default_factory=lambda: _flag('ANALYZER_FOLLOW', True))

  # Audio is cached rather than streamed: yt-dlp is slow, and both alignment and
  # playback want random access to decoded samples.
  target_sample_rate: int = 22050
  target_channels: int = 1

  # The speech server that reads a card's question aloud. The same self-hosted
  # OpenAI-compatible endpoint the tutor uses; a card is still built when it is down.
  tts_base_url: str = os.environ.get('ANALYZER_TTS_BASE_URL', 'http://192.168.2.174:8880/v1')
  tts_voice: str = os.environ.get('ANALYZER_TTS_VOICE', 'af_heart')

  def ensure_dirs(self) -> None:
    for d in (self.audio_dir, self.tracks_dir, self.sync_dir, self.render_dir,
              self.library_dir, self.decks_dir):
      d.mkdir(parents=True, exist_ok=True)

  @property
  def audio_dir(self) -> pathlib.Path:
    """Cached recordings, one WAV plus one JSON of metadata per piece."""
    return self.cache_dir / 'audio'

  @property
  def tracks_dir(self) -> pathlib.Path:
    """Practice tracks: the score rendered whole, or one hand at a time."""
    return self.cache_dir / 'tracks'

  @property
  def sync_dir(self) -> pathlib.Path:
    """Measure -> time maps, one JSON per piece."""
    return self.output_dir / 'sync'

  @property
  def render_dir(self) -> pathlib.Path:
    """Score renders and other derived pictures."""
    return self.output_dir / 'render'

  @property
  def decks_dir(self) -> pathlib.Path:
    """Anki decks: the specs, and the .apkg packages built from them."""
    return self.output_dir / 'decks'

  @property
  def library_dir(self) -> pathlib.Path:
    """The catalogue: curated metadata per piece (title, composer, tags, hidden).

    Separate from the question banks on purpose -- the banks hold facts derived
    from the score, this holds what a person chose. A database would keep the
    second as columns.
    """
    return self.output_dir / 'library'

  def audio_path(self, slug: str) -> pathlib.Path:
    return self.audio_dir / ('%s.wav' % slug)

  def audio_meta_path(self, slug: str) -> pathlib.Path:
    return self.audio_dir / ('%s.json' % slug)

  def sync_path(self, slug: str) -> pathlib.Path:
    return self.sync_dir / ('%s.json' % slug)


CFG = AnalyzerConfig()
