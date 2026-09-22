"""Background work, so the window never freezes.

Downloading a recording takes seconds and aligning one can take tens of seconds;
both run on a `QThread` and report progress through signals. Nothing in here
touches widgets -- the window connects these signals to its own slots, which is
what keeps the GUI responsive and the workers testable on their own.
"""

from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from ..config import AnalyzerConfig
from ..schema import AudioSource, SyncResult


class DownloadWorker(QThread):
  """Fetch a recording once, into the local cache.

  Signals:
    progressed: (percent 0..100, message)
    fetched: (AudioSource) on success
    failed: (message) with something safe to show the user
  """

  progressed = pyqtSignal(float, str)
  fetched = pyqtSignal(object)
  failed = pyqtSignal(str)

  def __init__(self, cfg: AnalyzerConfig, slug: str, url: str,
               force: bool = False, parent=None):
    super().__init__(parent)
    self.cfg = cfg
    self.slug = slug
    self.url = url
    self.force = force

  def run(self) -> None:                        # pragma: no cover - needs Qt loop
    try:
      from .. import ytaudio
    except ImportError as e:
      self.failed.emit('the downloader is unavailable: %s' % e)
      return
    try:
      source = ytaudio.download_audio(
          self.cfg, self.slug, self.url, force=self.force,
          progress=lambda percent, message: self.progressed.emit(percent, message))
    except Exception as e:                      # DownloadError, and anything else
      self.failed.emit(str(e))
      return
    self.fetched.emit(source)


class AlignWorker(QThread):
  """Fit the measure map to the recording.

  Signals:
    progressed: (percent 0..100, message)
    aligned: (SyncResult)
    failed: (message)
  """

  progressed = pyqtSignal(float, str)
  aligned = pyqtSignal(object)
  failed = pyqtSignal(str)

  def __init__(self, cfg: AnalyzerConfig, played: List[int], wav_path,
               score_midi, baseline: Optional[SyncResult] = None, parent=None):
    super().__init__(parent)
    self.cfg = cfg
    self.played = list(played)
    self.wav_path = str(wav_path)
    self.score_midi = str(score_midi)
    self.baseline = baseline

  def run(self) -> None:                        # pragma: no cover - needs Qt loop
    try:
      from .. import align
    except ImportError as e:
      self.failed.emit('the alignment module is unavailable: %s' % e)
      return
    try:
      result = align.align_to_audio(
          self.played, self.wav_path, self.score_midi, cfg=self.cfg,
          baseline=self.baseline,
          progress=lambda percent, message: self.progressed.emit(percent, message))
    except Exception as e:
      self.failed.emit(str(e))
      return
    self.aligned.emit(result)
