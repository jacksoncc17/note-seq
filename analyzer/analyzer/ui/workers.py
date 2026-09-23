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


class WorkerCancelled(BaseException):
  """Raised inside a worker to stop it when the window is closing.

  Derives from `BaseException` on purpose: the progress helpers in `scoreview`
  and `align` swallow `Exception` so a broken callback cannot abort a render, and
  a cancellation must not be swallowed with them.
  """


class CancelableWorker(QThread):
  """A worker that can be asked to stop, so closing the window never leaves a
  running thread for Qt to destroy -- which aborts the process rather than
  reporting anything."""

  def __init__(self, parent=None):
    super().__init__(parent)
    self._cancelled = False

  def cancel(self) -> None:
    """Ask the worker to stop at its next progress report."""
    self._cancelled = True

  @property
  def cancelled(self) -> bool:
    return self._cancelled

  def reporter(self, callback=None):
    """Wrap a progress callback so it also honours cancellation."""
    def report(percent: float, message: str) -> None:
      if self._cancelled:
        raise WorkerCancelled()
      if callback is not None:
        callback(percent, message)
    return report


class DownloadWorker(CancelableWorker):
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
    self._cancelled = False
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
          progress=self.reporter(lambda percent, message:
                                 self.progressed.emit(percent, message)))
    except WorkerCancelled:
      return
    except Exception as e:                      # DownloadError, and anything else
      self.failed.emit(str(e))
      return
    self.fetched.emit(source)


class AlignWorker(CancelableWorker):
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
    self._cancelled = False
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
          progress=self.reporter(lambda percent, message:
                                 self.progressed.emit(percent, message)))
    except WorkerCancelled:
      return
    except Exception as e:
      self.failed.emit(str(e))
      return
    self.aligned.emit(result)


class RenderWorker(CancelableWorker):
  """Engrave the score into page images (Verovio + cairosvg).

  Engraving a 200-bar piano piece into nine pages takes on the order of fifteen
  seconds the first time and is then cached, so it runs off the UI thread like the
  other slow work.

  Signals:
    progressed: (percent 0..100, message)
    rendered: (ScoreRender)
    failed: (message)
  """

  progressed = pyqtSignal(float, str)
  rendered = pyqtSignal(object)
  failed = pyqtSignal(str)

  def __init__(self, cfg: AnalyzerConfig, musicxml, force: bool = False, parent=None):
    super().__init__(parent)
    self._cancelled = False
    self.cfg = cfg
    self.musicxml = str(musicxml)
    self.force = force

  def run(self) -> None:                        # pragma: no cover - needs Qt loop
    try:
      from .. import scoreview
    except ImportError as e:
      self.failed.emit('the score view is unavailable: %s' % e)
      return
    try:
      render = scoreview.render_score(
          self.cfg, self.musicxml, force=self.force,
          progress=self.reporter(lambda percent, message:
                                 self.progressed.emit(percent, message)))
    except WorkerCancelled:
      return
    except Exception as e:                      # ScoreRenderError, and anything else
      self.failed.emit(str(e))
      return
    self.rendered.emit(render)
