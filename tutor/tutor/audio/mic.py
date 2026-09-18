"""Microphone capture, deliberately dependency-free.

This host has no /dev/snd (it is a container), but PipeWire/PulseAudio is
reachable, so capture goes through `parecord` in raw mode. That avoids adding
PortAudio bindings to the venv and works on any Linux box with PulseAudio or
PipeWire running. `arecord -D pulse` and `sounddevice` are supported fallbacks.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from typing import AsyncIterator, Dict, List, Optional

from .base import AudioFrame

DEFAULT_RATE = 16000
DEFAULT_FRAME_MS = 20


def list_input_sources() -> List[Dict[str, object]]:
  """Real capture sources (monitor/loopback entries are not microphones).

  `pactl list short sources` emits FIVE whitespace-separated fields --
  index, name, driver, "s16le 1ch 16000Hz", state -- with a tab between them.
  Requiring six fields silently returned nothing, which is how a live test ended
  up reporting "using: None" while still capturing from the default device.
  """
  if shutil.which('pactl') is None:
    return []
  try:
    out = subprocess.run(['pactl', 'list', 'short', 'sources'],
                         capture_output=True, text=True, timeout=5).stdout
  except (OSError, subprocess.SubprocessError):
    return []
  sources: List[Dict[str, object]] = []
  for line in out.splitlines():
    parts = line.split('\t') if '\t' in line else line.split()
    if len(parts) < 5:
      continue
    name = parts[1]
    if name.endswith('.monitor'):
      continue
    # Flatten before matching: with tab-separated output the sample spec arrives
    # as one token ("s16le 1ch 16000Hz"), with space-separated output as three,
    # so scanning whole tokens either way misses one of the two layouts.
    channels = rate = 0
    for token in [t for part in parts[2:] for t in part.split()]:
      if token.endswith('ch') and token[:-2].isdigit():
        channels = int(token[:-2])
      elif token.endswith('Hz') and token[:-2].isdigit():
        rate = int(token[:-2])
    sources.append({
        'index': int(parts[0]) if parts[0].isdigit() else -1,
        'name': name,
        'driver': parts[2],
        'channels': channels or 1,
        'rate': rate,
        'state': parts[4],
    })
  return sources


def pick_source(device: Optional[str] = None) -> Optional[str]:
  """Substring match against the source names; prefers a USB mic by default."""
  sources = list_input_sources()
  if not sources:
    return device
  if device:
    for src in sources:
      if str(device).lower() in str(src['name']).lower():
        return str(src['name'])
    return device
  for src in sources:
    if 'usb' in str(src['name']).lower() or 'mono-fallback' in str(src['name']):
      return str(src['name'])
  return str(sources[0]['name'])


def capture_command(device: Optional[str], rate: int, channels: int = 1,
                    backend: str = 'auto') -> List[str]:
  """The shell command that will stream raw PCM to stdout."""
  use_parecord = backend in ('auto', 'parecord') and shutil.which('parecord')
  if use_parecord:
    cmd = ['parecord', '--format=s16le', '--rate=%d' % rate,
           '--channels=%d' % channels, '--raw', '--latency-msec=100']
    if device:
      cmd.insert(1, '--device=%s' % device)
    return cmd
  use_arecord = backend in ('auto', 'arecord') and shutil.which('arecord')
  if use_arecord:
    cmd = ['arecord', '-D', 'pulse', '-f', 'S16_LE', '-r', str(rate),
           '-c', str(channels), '-t', 'raw', '-']
    return cmd
  raise RuntimeError(
      'No audio capture backend found. Install pulseaudio-utils (parecord) or '
      'alsa-utils (arecord), or pip install sounddevice with PortAudio.')


class MicrophoneSource:
  """Yields fixed-size PCM frames from the microphone."""

  def __init__(self, device: Optional[str] = None, rate: int = DEFAULT_RATE,
               frame_ms: int = DEFAULT_FRAME_MS, backend: str = 'auto'):
    self.device = pick_source(device)
    self.rate = rate
    self.frame_ms = frame_ms
    self.backend = backend
    self.frame_bytes = int(rate * frame_ms / 1000) * 2   # 16-bit mono

  async def frames(self) -> AsyncIterator[AudioFrame]:
    cmd = capture_command(self.device, self.rate, backend=self.backend)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
      while True:
        chunk = await proc.stdout.readexactly(self.frame_bytes)
        yield AudioFrame(pcm=chunk, sample_rate=self.rate,
                         at=time.monotonic())
    except asyncio.IncompleteReadError:
      err = b''
      if proc.stderr is not None:
        try:
          err = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
        except asyncio.TimeoutError:
          err = b''
      raise RuntimeError('microphone stream ended: %s'
                         % (err.decode('utf-8', 'replace').strip() or 'no data'))
    finally:
      if proc.returncode is None:
        proc.terminate()
        try:
          await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
          proc.kill()


def record_raw(device: Optional[str], seconds: float,
               rate: int = DEFAULT_RATE) -> bytes:
  """Blocking one-shot recording, for calibration and tests."""
  cmd = capture_command(pick_source(device), rate)
  proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
  try:
    want = int(seconds * rate) * 2
    chunks = []
    got = 0
    while got < want:
      chunk = proc.stdout.read(min(8192, want - got))
      if not chunk:
        break
      chunks.append(chunk)
      got += len(chunk)
    return b''.join(chunks)
  finally:
    proc.terminate()
    try:
      proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
      proc.kill()
