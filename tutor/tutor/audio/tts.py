"""TTS adapters: an offline stub and a self-hosted OpenAI-compatible client.

`speak` is cancellable at sentence granularity: the conductor cancels it when
the student barges in or starts playing, and the text that was never spoken must
be dropped from the turn record as well (see tutor.grounding / conductor).
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from typing import Callable, Optional

SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')


def sentences(text: str) -> list:
  return [s.strip() for s in SENTENCE_SPLIT.split(text.strip()) if s.strip()]


class StubTts:
  """Prints the tutor's speech, sentence by sentence, without audio.

  `delay_s` simulates synthesis latency so cancellation and barge-in can be
  exercised in tests and in the scripted demo.
  """

  def __init__(self, delay_s: float = 0.0, sink: Optional[Callable[[str], None]] = None):
    self.delay_s = delay_s
    self.sink = sink or (lambda s: print('  [tutor] %s' % s))
    self.spoken: list = []

  async def speak(self, text: str, cancel: asyncio.Event) -> bool:
    for sentence in sentences(text):
      if cancel.is_set():
        return False
      if self.delay_s:
        try:
          await asyncio.wait_for(cancel.wait(), timeout=self.delay_s)
          return False          # cancelled during synthesis
        except asyncio.TimeoutError:
          pass
      else:
        await asyncio.sleep(0)
      self.spoken.append(sentence)
      self.sink(sentence)
    return True

  def drop_unspoken(self, text: str) -> str:
    """The part of `text` that never reached the speaker."""
    done = len(self.spoken)
    return ' '.join(sentences(text)[done:])


class OpenAiCompatTts:
  """Self-hosted TTS (Kokoro-FastAPI, openedai-speech, Piper servers, ...)."""

  def __init__(self, base_url: str, model: str = 'kokoro', voice: str = 'af_heart',
               api_key: str = 'not-needed', out_dir: str = 'tutor/output',
               timeout_s: float = 60.0, play: bool = False,
               lang_code: str = '', verbose: bool = True):
    self.base_url = base_url.rstrip('/')
    self.model = model
    self.voice = voice
    self.api_key = api_key
    self.out_dir = out_dir
    self.timeout_s = timeout_s
    self.play = play
    self.verbose = verbose          # the doctor runs without the chatter
    # Language code for non-English voices ('z' = Mandarin). Passed through when
    # set; measured on a live server, zf_xiaoxiao produced the same Chinese with
    # and without it, so treat this as schema-correct rather than load-bearing.
    self.lang_code = lang_code
    self.counter = 0

  def _post(self, text: str, index: int) -> str:
    import json
    import os
    # stream=False matters: Kokoro-FastAPI defaults to a streaming response,
    # which arrives chunked with a placeholder frame count in the WAV header.
    payload = {'model': self.model, 'voice': self.voice,
               'input': text, 'response_format': 'wav',
               'stream': False}
    if self.lang_code:
      payload['lang_code'] = self.lang_code
    req = urllib.request.Request(
        self.base_url + '/audio/speech',
        data=json.dumps(payload).encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer %s' % self.api_key})
    with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
      audio = resp.read()
    os.makedirs(self.out_dir, exist_ok=True)
    path = os.path.join(self.out_dir, 'tts_%04d.wav' % index)
    with open(path, 'wb') as f:
      f.write(audio)
    return path

  async def speak(self, text: str, cancel: asyncio.Event) -> bool:
    for sentence in sentences(text):
      if cancel.is_set():
        return False
      self.counter += 1
      try:
        path = await asyncio.to_thread(self._post, sentence, self.counter)
      except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            'TTS server unreachable at %s (%s). Start it, or use TUTOR_TTS=stub.'
            % (self.base_url, exc)) from exc
      if self.verbose:
        print('  [tutor] %s   (%s)' % (sentence, path))
      if self.play:
        await self._play(path, cancel)
    return True

  async def _play(self, path: str, cancel: asyncio.Event) -> None:
    """Playback via paplay/aplay when present, else sounddevice.

    PulseAudio's own players avoid a PortAudio dependency, and `paplay` is
    already available wherever `parecord` is -- which is how the microphone path
    works on this host too.
    """
    import asyncio as _asyncio
    import shutil

    player = shutil.which('paplay') or shutil.which('aplay')
    if player:
      proc = await _asyncio.create_subprocess_exec(
          player, path, stdout=_asyncio.subprocess.DEVNULL,
          stderr=_asyncio.subprocess.DEVNULL)
      while proc.returncode is None:
        if cancel.is_set():
          proc.terminate()
          break
        await _asyncio.sleep(0.05)
      try:
        await _asyncio.wait_for(proc.wait(), timeout=2.0)
      except _asyncio.TimeoutError:
        proc.kill()
      return
    try:
      import sounddevice  # type: ignore
      import soundfile  # type: ignore
    except Exception:
      return
    data, rate = soundfile.read(path)

    def _run():
      sounddevice.play(data, rate)
      sounddevice.wait()

    await asyncio.to_thread(_run)
