"""Instrument channel: MIDI input, with a scripted stub for demos and tests.

Phrase boundaries matter as much as the notes: `MusicEnd` is what tells the
conductor "the student finished a phrase", which is the trigger for coaching in
LISTEN_COACH mode and the answer submission in DRILL mode.

If your piano has MIDI out, use `MidiInstrument` and you never need polyphonic
audio transcription -- the weakest link in the whole system.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Iterable, List, Optional

from ..events import MusicEnd, MusicStart, NoteEvent


class StubInstrument:
  """Plays back scripted phrases as if the student were playing them."""

  def __init__(self, phrases: Optional[Iterable[Iterable[int]]] = None,
               gap_s: float = 0.4, note_s: float = 0.25):
    self.phrases = [list(p) for p in (phrases or [])]
    self.gap_s = gap_s
    self.note_s = note_s
    self.played: List[int] = []

  def add_phrase(self, pitches: Iterable[int]) -> None:
    self.phrases.append(list(pitches))

  async def events(self) -> AsyncIterator[object]:
    for phrase in self.phrases:
      await asyncio.sleep(0)
      yield MusicStart()
      for pitch in phrase:
        self.played.append(pitch)
        yield NoteEvent(pitch=pitch)
        await asyncio.sleep(0)
      await asyncio.sleep(self.gap_s)
      yield MusicEnd(pitch_events=[(p, self.note_s) for p in phrase],
                     duration_s=self.note_s * len(phrase))


class MidiInstrument:
  """MIDI-in via mido, grouped into phrases by an inactivity timer."""

  def __init__(self, port_match: str = '', silence_ms: int = 600,
               poll_s: float = 0.005):
    self.port_match = port_match
    self.silence_s = silence_ms / 1000.0
    self.poll_s = poll_s

  def _open(self):
    import mido
    names = mido.get_input_names()
    if not names:
      raise RuntimeError(
          'No MIDI input ports found. Plug in a keyboard, or use '
          'TUTOR_INSTRUMENT=stub.')
    chosen = next((n for n in names if self.port_match.lower() in n.lower()),
                  names[0])
    return mido.open_input(chosen), chosen

  async def events(self) -> AsyncIterator[object]:
    port, name = await asyncio.to_thread(self._open)
    print('  [instrument] listening on %s' % name)
    phrase: List[int] = []
    playing = False
    last_note_at = 0.0
    try:
      while True:
        for msg in port.iter_pending():
          if msg.type == 'note_on' and getattr(msg, 'velocity', 1) > 0:
            if not playing:
              playing = True
              yield MusicStart()
            phrase.append(msg.note)
            last_note_at = time.monotonic()
            yield NoteEvent(pitch=msg.note, velocity=msg.velocity)
        if playing and (time.monotonic() - last_note_at) > self.silence_s:
          yield MusicEnd(pitch_events=list(phrase),
                         duration_s=self.silence_s)
          phrase = []
          playing = False
        await asyncio.sleep(self.poll_s)
    finally:
      port.close()
