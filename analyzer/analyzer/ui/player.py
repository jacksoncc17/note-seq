"""Playing an exact range of a recording.

"Play measures 40 to 83" has to start on measure 40's downbeat and stop at the
end of 83, so this plays raw PCM out of a buffer rather than asking a media
player to seek: seeking to a millisecond is not reliably sample-accurate, and
overshooting the end by a fraction of a bar is exactly the mistake the user is
trying to hear. The whole recording is decoded once into memory (a six-minute
mono 22 kHz track is about 16 MB) and slices of it are handed to QAudioOutput.
"""

from __future__ import annotations

import pathlib
from typing import Optional

import numpy as np
from PyQt5.QtCore import QBuffer, QByteArray, QElapsedTimer, QObject, QTimer, pyqtSignal
from PyQt5.QtMultimedia import QAudio, QAudioFormat, QAudioOutput


class PlayerError(RuntimeError):
  """Raised when there is no usable audio device or the file cannot be read."""


class RangePlayer(QObject):
  """Sample-accurate playback of `[start, end)` inside one recording.

  Signals:
    positionChanged: seconds from the start of the *recording* while playing.
    finished: the requested range ended (or was stopped).
    errorOccurred: a user-facing message; playback did not start.
  """

  positionChanged = pyqtSignal(float)
  finished = pyqtSignal()
  errorOccurred = pyqtSignal(str)

  def __init__(self, parent=None):
    super().__init__(parent)
    self._pcm: Optional[np.ndarray] = None
    self._sample_rate = 0
    self._path: Optional[pathlib.Path] = None
    self._output: Optional[QAudioOutput] = None
    self._buffer: Optional[QBuffer] = None
    self._range: tuple = (0.0, 0.0)
    self._clock = QElapsedTimer()
    self._timer = QTimer(self)
    self._timer.setInterval(40)
    self._timer.timeout.connect(self._tick)

  # ------------------------------------------------------------------ loading

  def load(self, path) -> bool:
    """Decode a WAV into memory. Returns False (and emits) when it cannot."""
    path = pathlib.Path(path)
    try:
      import soundfile as sf
      data, rate = sf.read(str(path), dtype='float32', always_2d=False)
    except Exception as e:                       # soundfile raises several types
      self.errorOccurred.emit('could not read %s: %s' % (path.name, e))
      return False
    if data.ndim > 1:
      data = data.mean(axis=1)
    self._pcm = np.ascontiguousarray(data, dtype='float32')
    self._sample_rate = int(rate)
    self._path = path
    return True

  @property
  def path(self) -> Optional[pathlib.Path]:
    return self._path

  @property
  def duration(self) -> float:
    if self._pcm is None or self._sample_rate <= 0:
      return 0.0
    return float(len(self._pcm)) / float(self._sample_rate)

  @property
  def samples(self):
    return self._pcm

  @property
  def sample_rate(self) -> int:
    return self._sample_rate

  # ------------------------------------------------------------------ playing

  @property
  def is_playing(self) -> bool:
    # State and error live in the QAudio namespace, not on QAudioOutput: asking
    # QAudioOutput for ActiveState raises AttributeError, and an exception in a
    # Qt slot aborts the process rather than showing a message.
    if self._output is None:
      return False
    try:
      return self._output.state() == QAudio.ActiveState
    except RuntimeError:                       # the C++ object is already gone
      return False

  def play_range(self, start_s: float, end_s: float) -> bool:
    """Play `[start_s, end_s)` of the loaded recording.

    Any failure is reported through `errorOccurred` instead of raising, because an
    exception escaping a Qt slot terminates the application -- an audio-device
    quirk must never take the window down with it.
    """
    try:
      return self._play_range(start_s, end_s)
    except Exception as e:                     # deliberately broad: see docstring
      self.errorOccurred.emit('could not start playback: %s' % e)
      try:
        self.stop()
      except Exception:
        pass
      return False

  def _play_range(self, start_s: float, end_s: float) -> bool:
    if self._pcm is None or self._sample_rate <= 0:
      self.errorOccurred.emit('no recording loaded')
      return False
    self.stop()

    start_s = max(0.0, float(start_s))
    end_s = min(self.duration, float(end_s))
    if end_s - start_s < 0.05:
      self.errorOccurred.emit('that range is too short to play (%.2f s)' % (end_s - start_s))
      return False

    first = int(start_s * self._sample_rate)
    last = int(end_s * self._sample_rate)
    chunk = self._pcm[first:last]

    fmt = QAudioFormat()
    fmt.setSampleRate(self._sample_rate)
    fmt.setChannelCount(1)
    fmt.setSampleSize(16)
    fmt.setCodec('audio/pcm')
    fmt.setByteOrder(QAudioFormat.LittleEndian)
    fmt.setSampleType(QAudioFormat.SignedInt)

    try:
      self._output = QAudioOutput(fmt, self)
    except Exception as e:
      self.errorOccurred.emit('no audio output available: %s' % e)
      self._output = None
      return False
    if self._output.error() != QAudio.NoError:
      self.errorOccurred.emit('audio device problem: %s' % self._output.error())
      self._output = None
      return False

    pcm16 = np.clip(chunk, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype('<i2', copy=False)
    payload = QByteArray(pcm16.tobytes())
    self._buffer = QBuffer(self)              # kept: Qt does not own the data
    self._buffer.setData(payload)
    self._buffer.open(QBuffer.ReadOnly)

    self._range = (start_s, end_s)
    self._output.start(self._buffer)
    self._clock.start()
    self._timer.start()
    return True

  def stop(self) -> None:
    was_playing = self._output is not None
    self._timer.stop()
    if self._output is not None:
      try:
        self._output.stop()
      except RuntimeError:
        pass
      self._output.deleteLater()
      self._output = None
    if self._buffer is not None:
      try:
        self._buffer.close()
      except RuntimeError:
        pass
      self._buffer = None
    if was_playing:
      self.finished.emit()

  def _tick(self) -> None:
    if self._output is None:
      return
    try:
      state = self._output.state()
    except RuntimeError:                       # deleted underneath us
      self.stop()
      return
    elapsed = self._clock.elapsed() / 1000.0
    start_s, end_s = self._range
    position = start_s + elapsed
    self.positionChanged.emit(min(position, end_s))
    if position >= end_s or state == QAudio.IdleState:
      self.stop()
