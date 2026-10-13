"""Audio that is arriving, rather than audio already on disk.

`follow` is a stream follower: it wants mono float32 blocks of about 20-50 ms, as
they happen. This module is where those blocks come from, and there are two
sources, because the honest way to build a live feature on a machine that has no
piano in it is to have a second source that behaves exactly like the first:

* `MicSource` -- the microphone, through the sound server. It shells out to
  `parecord` (PulseAudio/PipeWire), falling back to `arecord` (bare ALSA) and
  `pw-record`; one of the three is present on any Linux desktop, and none of them
  needs a Python audio library, a build step, or a device enumeration of our own.
  The sound server does the resampling, so a 44.1 or 48 kHz microphone still
  arrives at 22050 Hz.
* `FileSource` -- a wav file, fed in at the pace it would have been played. It
  goes through the identical code path: same block size, same rate, same
  timestamps. That is the whole point -- the feature can be exercised, and tested,
  with no microphone and no pianist, and what is measured is the real thing.

Both hand back `None` when there is nothing yet rather than blocking, so a caller
with its own clock (the GUI's timer) stays responsive. `MicSource` does its
blocking read on a worker thread and queues the result; `FileSource` compares the
wall clock against the time each block is due.

Nothing here decides anything about music. If the sound server is missing, or the
device is busy, `start` raises `CaptureError` with what the tool actually said --
a live feature that silently hears nothing is worse than one that refuses.
"""

from __future__ import annotations

import pathlib
import queue
import shutil
import subprocess
import threading
import time
from typing import List, Optional, Sequence

import numpy as np

from . import follow

# The follower's own rate and hop: a block per hop means one position per frame,
# and no buffering of our own between here and there.
RATE = follow.SAMPLE_RATE
BLOCK = follow.HOP

# How far behind the microphone we are willing to be. The sound server's default
# is measured in hundreds of milliseconds; the follower wants to know where the
# player is *now*.
LATENCY_MS = 40

# The tools, in the order we prefer them. The first is PipeWire/PulseAudio, the
# second works on a bare ALSA machine, the third is PipeWire's own recorder.
TOOLS = ('parecord', 'arecord', 'pw-record')


class CaptureError(RuntimeError):
  """The device could not be opened, or stopped delivering."""


# --------------------------------------------------------------------- devices

def parse_sources(text: str) -> List[str]:
  """The capture devices in `pactl list short sources` output, in its order.

  Monitors are kept and marked: `alsa_output...monitor` is what the machine is
  *playing*, which is how a recording can be followed without a piano -- put the
  recording on and point the follower at the monitor instead of the microphone.
  """
  names = []
  for line in text.splitlines():
    parts = line.split()
    if len(parts) < 2:                    # index, name, driver, spec, state
      continue
    name = parts[1]
    if name and name != 'Source':
      names.append(name)
  return names


def microphones(tool: Optional[str] = None) -> List[str]:
  """Capture devices the sound server knows about; empty when it cannot say."""
  if tool is None and shutil.which('pactl') is None:
    return []
  try:
    out = subprocess.run(['pactl', 'list', 'short', 'sources'], capture_output=True,
                         text=True, timeout=5)
  except (OSError, subprocess.SubprocessError):
    return []
  if out.returncode != 0:
    return []
  return parse_sources(out.stdout)


def default_microphone() -> Optional[str]:
  """The sound server's default capture device, or None if it has no opinion."""
  if shutil.which('pactl') is None:
    return None
  try:
    out = subprocess.run(['pactl', 'get-default-source'], capture_output=True,
                         text=True, timeout=5)
  except (OSError, subprocess.SubprocessError):
    return None
  name = out.stdout.strip()
  return name or None


def device_label(name: str) -> str:
  """A device name, said the way a person would read it."""
  if name.endswith('.monitor'):
    short = name[:-len('.monitor')]
    return '%s  (what is playing)' % _short_device(short)
  return _short_device(name)


def _short_device(name: str) -> str:
  """`alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback` -> something short.

  Device names are long and mostly boilerplate. The tail is what tells two of them
  apart, so the tail is what is kept.
  """
  parts = [p for p in name.split('.') if p]
  if len(parts) >= 2:
    tail = parts[-1]
    if tail in ('analog-stereo', 'mono-fallback', 'analog-mono', 'default'):
      tail = parts[-2] if len(parts) > 2 else tail
    return tail.replace('_', ' ')
  return name


def available() -> bool:
  """Is there a capture tool to shell out to at all?"""
  return any(shutil.which(t) for t in TOOLS)


# --------------------------------------------------------------------- sources

class AudioSource:
  """A stream of mono float32 blocks at `rate`, from somewhere.

  `read` never blocks: it returns the next block, or None if there is not one yet.
  That is what lets the GUI keep its own timer and its own painting while a
  microphone is open.
  """

  def __init__(self, rate: int = RATE, block: int = BLOCK):
    self.rate = int(rate)
    self.block = int(block)
    # Why the source stopped, if it stopped on its own: a device that is unplugged
    # mid-piece is worth saying out loud rather than leaving the page standing still.
    self.error: str = ''

  def start(self) -> 'AudioSource':
    return self

  def read(self, timeout: float = 0.0) -> Optional[np.ndarray]:
    raise NotImplementedError

  def stop(self) -> None:
    pass

  def describe(self) -> str:
    return 'audio'

  @property
  def block_seconds(self) -> float:
    return self.block / float(self.rate)

  def __enter__(self) -> 'AudioSource':
    return self.start()

  def __exit__(self, *exc) -> None:
    self.stop()


def _command(tool: str, rate: int, device: Optional[str],
             latency_ms: int = LATENCY_MS) -> List[str]:
  """The raw 16-bit mono capture command for `tool`, writing to stdout."""
  if tool == 'parecord':
    cmd = ['parecord', '--raw', '--format=s16le', '--rate=%d' % rate,
           '--channels=1', '--latency-msec=%d' % latency_ms]
    if device:
      cmd.append('--device=%s' % device)
    return cmd
  if tool == 'arecord':
    cmd = ['arecord', '--quiet', '-t', 'raw', '-f', 'S16_LE', '-r', str(rate), '-c', '1']
    if device:
      cmd += ['-D', device]
    return cmd + ['-']
  if tool == 'pw-record':
    cmd = ['pw-record', '--rate', str(rate), '--channels', '1', '--format', 's16']
    if device:
      cmd += ['--target', device]
    return cmd + ['-']
  raise CaptureError('no way to record: none of %s is installed' % (', '.join(TOOLS),))


class MicSource(AudioSource):
  """The microphone, as blocks.

  The recording tool is started at `start()` and its stdout read on a daemon
  thread into a small queue, which is deliberately small: if the machine cannot
  keep up with the follower the right thing to drop is old audio, not to fall
  further and further behind. `start` waits long enough to notice a tool that
  refuses to open the device, so a bad device name is an error at the moment it
  is chosen rather than a page that never moves.
  """

  def __init__(self, device: Optional[str] = None, rate: int = RATE, block: int = BLOCK,
               tool: Optional[str] = None, command: Optional[Sequence[str]] = None,
               latency_ms: int = LATENCY_MS, queue_blocks: int = 8):
    super().__init__(rate, block)
    self.device = device
    self.latency_ms = latency_ms
    self.tool = tool or next((t for t in TOOLS if shutil.which(t)), '')
    self._command = list(command) if command else None
    self._proc: Optional[subprocess.Popen] = None
    self._thread: Optional[threading.Thread] = None
    self._stop = threading.Event()
    self._blocks: 'queue.Queue' = queue.Queue(maxsize=max(1, int(queue_blocks)))
    self.stderr_text: str = ''
    self.blocks_read = 0

  @property
  def raw_bytes(self) -> int:
    """Two bytes per sample: the tools speak 16-bit and we convert on arrival."""
    return self.block * 2

  def start(self) -> 'MicSource':
    if self._proc is not None:
      return self
    cmd = self._command or _command(self.tool, self.rate, self.device, self.latency_ms)
    try:
      self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=0)
    except OSError as e:
      raise CaptureError('could not start %s: %s' % (cmd[0], e))
    time.sleep(0.15)                      # long enough for a refusal to show up
    code = self._proc.poll()
    if code is not None:
      self.stderr_text = self._read_stderr()
      self._proc = None
      raise CaptureError('%s stopped at once (%s)%s'
                         % (cmd[0], code,
                            ': ' + self.stderr_text.strip() if self.stderr_text else ''))
    self._stop.clear()
    self._thread = threading.Thread(target=self._pump, name='analyzer-mic', daemon=True)
    self._thread.start()
    return self

  def _read_stderr(self) -> str:
    if self._proc is None or self._proc.stderr is None:
      return ''
    try:
      return self._proc.stderr.read().decode('utf-8', 'replace')
    except (OSError, ValueError):
      return ''

  def _pump(self) -> None:
    """Accumulate exactly one block at a time; an empty read is the end of the stream.

    The pipe is unbuffered, so a read returns whatever happens to be there -- half a
    block, or a pipe's worth of three blocks. Treating that as the end of the stream
    (which is what this did first) made every live listen stop after a moment.
    """
    proc = self._proc
    if proc is None or proc.stdout is None:
      return
    want = self.raw_bytes
    pending = bytearray()
    while not self._stop.is_set():
      try:
        chunk = proc.stdout.read(want - len(pending))
      except (OSError, ValueError):
        break
      if not chunk:
        break
      pending += chunk
      if len(pending) < want:
        continue
      samples = np.frombuffer(bytes(pending), dtype='<i2').astype('float32') / 32768.0
      pending.clear()
      self.blocks_read += 1
      try:
        self._blocks.put_nowait(samples)
      except queue.Full:
        try:                              # stay live: the oldest block goes
          self._blocks.get_nowait()
        except queue.Empty:
          pass
        try:
          self._blocks.put_nowait(samples)
        except queue.Full:
          pass
    if not self._stop.is_set():
      self.error = 'the device stopped sending audio'

  def read(self, timeout: float = 0.0) -> Optional[np.ndarray]:
    try:
      if timeout <= 0:
        return self._blocks.get_nowait()
      return self._blocks.get(timeout=timeout)
    except queue.Empty:
      return None

  def stop(self) -> None:
    self._stop.set()
    proc, self._proc = self._proc, None
    if proc is not None:
      try:
        proc.terminate()
        try:
          proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
          proc.kill()
      except OSError:
        pass
      for stream in (proc.stdout, proc.stderr):
        try:
          if stream is not None:
            stream.close()
        except OSError:
          pass
    thread, self._thread = self._thread, None
    if thread is not None and thread.is_alive():
      thread.join(timeout=1.0)
    while True:                           # a stale block must not outlive the source
      try:
        self._blocks.get_nowait()
      except queue.Empty:
        break

  def describe(self) -> str:
    if self.device:
      return 'the microphone (%s)' % device_label(self.device)
    return 'the microphone (%s)' % (default_microphone() or 'the default device')


class FileSource(AudioSource):
  """A wav file, delivered in blocks at the pace it would have been played.

  `real_time=False` hands the whole file over as fast as it is asked for, which is
  what the tests use: the timing being tested is the follower's, and waiting in
  real time would only make the test slow. `real_time=True` (the default) is the
  microphone's behaviour, so a piece can be followed with no piano in the room.
  """

  def __init__(self, path, rate: int = RATE, block: int = BLOCK, real_time: bool = True,
               loop: bool = False):
    super().__init__(rate, block)
    self.path = pathlib.Path(path)
    self.real_time = bool(real_time)
    self.loop = bool(loop)
    self._samples = np.zeros(0, dtype='float32')
    self._at = 0
    self._started = 0.0
    self._delivered = 0

  def start(self) -> 'FileSource':
    import soundfile as sf
    if not self.path.is_file():
      raise CaptureError('no such file: %s' % self.path)
    try:
      data, rate = sf.read(str(self.path), dtype='float32', always_2d=False)
    except Exception as e:
      raise CaptureError('could not read %s: %s' % (self.path.name, e))
    if data.ndim > 1:
      data = data.mean(axis=1)
    if rate != self.rate:
      import librosa
      data = librosa.resample(np.asarray(data, dtype='float32'), orig_sr=rate,
                              target_sr=self.rate)
    self._samples = np.asarray(data, dtype='float32')
    self._at = 0
    self._delivered = 0
    self._started = time.monotonic()
    return self

  @property
  def duration(self) -> float:
    return len(self._samples) / float(self.rate)

  @property
  def finished(self) -> bool:
    return self._at >= len(self._samples)

  def read(self, timeout: float = 0.0) -> Optional[np.ndarray]:
    if self._at >= len(self._samples):
      if not self.loop or not len(self._samples):
        return None
      self._at = 0
      self._started = time.monotonic()
    if self.real_time:
      # Due times are counted from the start rather than from the previous block, so
      # a slow tick cannot make the replay drift away from the clock it is imitating.
      due = self._started + (self._delivered + 1) * self.block_seconds
      if time.monotonic() < due:
        return None
    block = self._samples[self._at:self._at + self.block]
    self._at += self.block
    self._delivered += 1
    return block

  def stop(self) -> None:
    self._at = len(self._samples)

  def describe(self) -> str:
    return 'the file %s' % self.path.name


class RecordSource(MicSource):
  """The microphone, plus the file it is being written to.

  A rehearsal is worth keeping: this writes every block it hands over to a wav as
  well, so a session that went well can be replayed through `FileSource` -- the
  same audio, the same code path -- without a microphone.
  """

  def __init__(self, path, **kwargs):
    super().__init__(**kwargs)
    self.path = pathlib.Path(path)
    self._out = None
    self._written = 0

  def start(self) -> 'RecordSource':
    super().start()
    import soundfile as sf
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self._out = sf.SoundFile(str(self.path), mode='w', samplerate=self.rate,
                             channels=1, subtype='PCM_16')
    self._written = 0
    return self

  def read(self, timeout: float = 0.0) -> Optional[np.ndarray]:
    block = super().read(timeout)
    if block is not None and self._out is not None:
      try:
        self._out.write(block)
        self._written += len(block)
      except Exception:
        pass
    return block

  def stop(self) -> None:
    super().stop()
    out, self._out = self._out, None
    if out is not None:
      try:
        out.close()
      except Exception:
        pass

  def describe(self) -> str:
    return '%s, kept as %s' % (MicSource.describe(self), self.path.name)
