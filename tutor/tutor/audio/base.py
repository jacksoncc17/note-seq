"""Protocols for the four audio seams."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Protocol


@dataclass
class AudioFrame:
  pcm: bytes
  sample_rate: int
  at: float = 0.0


@dataclass
class Transcript:
  text: str
  confidence: float = 1.0
  is_final: bool = True
  # Servers that report it (whisper-asr-webservice does) let the tutor tell
  # "the student said nothing" from "the microphone heard the piano".
  no_speech_prob: float = 0.0
  language: str = ''


class Vad(Protocol):
  """Frame-level speech activity. Returns an event name or None."""

  def push(self, frame: AudioFrame) -> Optional[str]:
    """'speech_start' | 'speech_end' | None"""
    ...


class Asr(Protocol):
  """Utterance-level speech recognition over the speech channel only."""

  async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
    ...


class Transcriber(Protocol):
  """Streaming variant: partial hypotheses while the student is talking.

  Worth having in production because the tutor can begin composing a reply on a
  stable partial, which buys 150-300 ms of the latency budget.
  """

  async def stream(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[Transcript]:
    ...


class Tts(Protocol):
  """Sentence-streaming speech synthesis that can be cancelled mid-utterance."""

  async def speak(self, text: str, cancel: asyncio.Event) -> bool:
    """Speak `text`; return False if cancelled before finishing."""
    ...


class InstrumentSource(Protocol):
  """The piano channel: MIDI input or note tracking over audio.

  Yields `MusicStart`, `NoteEvent` and `MusicEnd` events. Phrase boundaries
  (`MusicEnd`) are what make music-aware turn taking possible.
  """

  def events(self) -> AsyncIterator[object]:
    ...
