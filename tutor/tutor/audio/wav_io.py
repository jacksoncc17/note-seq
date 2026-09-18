"""WAV helpers: get 16 kHz mono PCM out of whatever file you have.

A microphone pipeline hands the ASR raw frames, but for testing against a real
server we need to turn a WAV file into exactly that shape -- including the
resample, because a 22.05 kHz espeak clip sent as if it were 16 kHz would be
heard by Whisper as slow, detuned speech.
"""

from __future__ import annotations

import wave
from typing import Optional

import numpy as np


def read_wav(path: str) -> tuple:
  """Return (float32 mono samples in [-1, 1], sample_rate).

  Frame counts are derived from the bytes actually present, never from the
  header: server-generated WAVs routinely declare 2,147,483,647 frames (a
  streaming placeholder), which reads as ~89000 seconds of audio for a 6-second
  file. Verified against Kokoro-FastAPI output.
  """
  with wave.open(path, 'rb') as w:
    channels = w.getnchannels()
    width = w.getsampwidth()
    rate = w.getframerate()
    declared = w.getnframes()
    chunks = []
    while True:
      chunk = w.readframes(1 << 16)
      if not chunk:
        break
      chunks.append(chunk)
  frames = b''.join(chunks)
  actual = len(frames) // max(width * channels, 1)
  if declared > actual * 2:
    # Worth surfacing: silently accepting it is how a 6s file becomes 25 hours.
    import sys
    print('note: %s declares %d frames but holds %d; using the real count'
          % (path, declared, actual), file=sys.stderr)
  if width == 2:
    data = np.frombuffer(frames, dtype='<i2').astype(np.float32) / 32768.0
  elif width == 1:
    data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128) / 128.0
  elif width == 4:
    data = np.frombuffer(frames, dtype='<i4').astype(np.float32) / 2147483648.0
  else:
    raise ValueError('unsupported WAV sample width: %d bytes' % width)
  if channels > 1:
    data = data.reshape(-1, channels).mean(axis=1)
  return data, rate


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
  """Linear-interpolation resample -- fine for speech at these rates."""
  if src_rate == dst_rate or samples.size == 0:
    return samples
  duration = samples.size / float(src_rate)
  n_out = max(1, int(round(duration * dst_rate)))
  src_idx = np.linspace(0.0, samples.size - 1, num=n_out, dtype=np.float64)
  lo = np.floor(src_idx).astype(np.int64)
  hi = np.minimum(lo + 1, samples.size - 1)
  frac = (src_idx - lo).astype(np.float32)
  return (samples[lo] * (1.0 - frac) + samples[hi] * frac).astype(np.float32)


def pcm16(samples: np.ndarray) -> bytes:
  clipped = np.clip(samples, -1.0, 1.0)
  return (clipped * 32767.0).astype('<i2').tobytes()


def wav_to_pcm16_mono(path: str, target_rate: int = 16000) -> tuple:
  """(pcm bytes at target_rate, source_rate, source_duration_s)."""
  samples, rate = read_wav(path)
  duration = samples.size / float(rate) if rate else 0.0
  return pcm16(resample(samples, rate, target_rate)), rate, duration


def pcm16_to_wav_bytes(pcm: bytes, sample_rate: int,
                       channels: int = 1) -> bytes:
  import io
  buf = io.BytesIO()
  with wave.open(buf, 'wb') as w:
    w.setnchannels(channels)
    w.setsampwidth(2)
    w.setframerate(sample_rate)
    w.writeframes(pcm)
  return buf.getvalue()


def find_test_clip(hint: Optional[str] = None) -> Optional[str]:
  """A speech clip to probe a server with, if one has been synthesised."""
  import glob
  import os
  if hint and os.path.exists(hint):
    return hint
  # wav_io.py -> audio -> tutor -> tutor(pkg dir) -> repo root
  repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
      os.path.abspath(__file__)))))
  for pattern in ('tutor/output/asr/*.wav', 'tutor/output/*.wav',
                  'note_seq/testdata/example_mono.wav'):
    matches = sorted(glob.glob(os.path.join(repo, pattern)))
    if matches:
      return matches[0]
  return None
