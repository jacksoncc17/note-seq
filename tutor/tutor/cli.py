"""Runnable entry point.

  # end-to-end demo, no model and no audio hardware required
  python -m tutor.cli --scripted

  # interactive text session (type your side of the conversation)
  python -m tutor.cli --text

  # point it at a self-hosted model
  TUTOR_LLM=openai TUTOR_LLM_BASE_URL=http://127.0.0.1:8000/v1 \
    TUTOR_LLM_MODEL=Qwen2.5-14B-Instruct python -m tutor.cli --text

  # show the grounding verifier catching a fabricated fact
  python -m tutor.cli --demo-grounding
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import List, Optional

from .audio import (EnergyVad, MidiInstrument, OpenAiCompatAsr,
                    OpenAiCompatTts, StubInstrument, StubTts)
from .audio.asr import StubAsr
from .conductor import Conductor
from .config import TutorConfig
from .core.facts import FactStore
from .core.tools import ToolBox
from .events import (MusicEnd, MusicStart, Shutdown, SpeechEnd, SpeechStart)
from .grounding import Evidence, GroundingVerifier
from .llm.stub import ScriptedTutor
from .modes import Mode
from .session import Session

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def note_to_midi(name: str) -> Optional[int]:
  name = name.strip()
  if not name:
    return None
  letter = name[0].upper()
  idx = 1
  accidental = 0
  if len(name) > 1 and name[1] in '#b':
    accidental = 1 if name[1] == '#' else -1
    idx = 2
  try:
    octave = int(name[idx:])
  except ValueError:
    return None
  base = NOTE_NAMES.index(letter) if letter in NOTE_NAMES else None
  if base is None:
    return None
  return (octave + 1) * 12 + base + accidental


def pitches_of_bars(facts: FactStore, bars: List[int], drop: int = 0) -> List[int]:
  """The score's pitches for a bar range, optionally with notes removed."""
  out: List[int] = []
  for b in bars:
    m = facts.bar(b)
    if not m:
      continue
    for name in m.get('pitches', []):
      midi = note_to_midi(name)
      if midi is not None:
        out.append(midi)
  if drop:
    out = out[:-drop] if drop < len(out) else []
  return out


def build(cfg: TutorConfig, mode: str, auto_advance: bool = True):
  # Resolve which piece this session is about, here rather than in main(), so
  # commands that need no question bank (the ASR/TTS probes, the mic tests) keep
  # working on a machine with no bank at all.
  from .pieces import resolve
  cfg.fingerprint, cfg.piece, why = resolve(cfg)
  print('piece: %s  (%s)' % (cfg.piece, why))
  facts = FactStore(cfg.fingerprint)
  session = Session(log_path=cfg.log_path, piece=facts.raw.get('source_file', ''))
  tools = ToolBox(facts, session, tier_max=cfg.tier_max,
                  render_dir=cfg.render_dir)
  evidence = Evidence(facts)
  verifier = GroundingVerifier(evidence, facts, policy=cfg.grounding_policy)

  if cfg.llm == 'openai':
    from .llm.openai_compat import OpenAiCompatLlm
    llm = OpenAiCompatLlm(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key,
                          cfg.llm_timeout_s)
  else:
    llm = ScriptedTutor()

  if cfg.tts == 'openai':
    tts = OpenAiCompatTts(cfg.tts_base_url, cfg.tts_model, cfg.tts_voice,
                          out_dir=cfg.render_dir, play=cfg.tts_play,
                          lang_code=cfg.tts_lang_code)
  else:
    tts = StubTts()

  conductor = Conductor(cfg, facts, tools, session, llm, tts, verifier,
                        mode=mode, auto_advance=auto_advance)
  return conductor, facts, session, tools, verifier


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_demo_grounding(cfg: TutorConfig) -> int:
  facts = FactStore(cfg.fingerprint)
  ev = Evidence(facts)
  verifier = GroundingVerifier(ev, facts, policy=cfg.grounding_policy)
  print('== grounding verifier demo ==')
  print('provenance of the question bank:')
  print(json.dumps(facts.provenance_report(), indent=2))
  print('\nNo evidence yet in this fresh session, so any factual claim is ungrounded:')
  for text in ('The A major section starts at bar 25.',
               'The first A major stretch begins at bar 53.'):
    report = verifier.check(text)
    print('  "%s" -> %s' % (text, 'grounded' if report.ok else
                            'UNGROUNDED %s' % [(c.kind, c.value) for c in report.ungrounded]))
  print('\nAfter calling get_piece_map, the true bar numbers become citable:')
  result = ToolBox(facts, _NullSession(), tier_max=cfg.tier_max).get_piece_map()
  ev.observe(result)
  for text in ('The A major section starts at bar 25.',
               'The first A major stretch begins at bar 53.'):
    report = verifier.check(text)
    verdict = 'grounded' if report.ok else 'UNGROUNDED %s' % [
        (c.kind, c.value) for c in report.ungrounded]
    print('  "%s" -> %s' % (text, verdict))
    verifier.policy = 'block'
    print('     policy=block -> %r' % verifier.repair(text, report))
    verifier.policy = 'hedge'
    print('     policy=hedge -> %r' % verifier.repair(text, report))
    verifier.policy = cfg.grounding_policy
  return 0


class _NullSession:
  def record_attempt(self, *a, **k):
    pass

  def status(self):
    return {}

  def weak_spots(self, limit=5):
    return []

  def weak_spot_prompts(self, limit=5):
    return []


async def cmd_scripted(cfg: TutorConfig, mode: str) -> int:
  conductor, facts, session, tools, verifier = build(cfg, mode)
  await conductor.greet()

  script = [
      ('drill me on this piece', None),
      ('measure 53', None),                     # answer to a key-change question
      ('I think bar 40', None),
      ('how am I doing', None),
  ]
  for text, _ in script:
    conductor.post(SpeechEnd(text=text))
    await _drain(conductor)

  # The student plays the opening bars three ways: correctly, with a wrong note,
  # and with two pitch classes missing -- so the comparison tool has something
  # to catch (and something to correctly leave alone).
  unit_bars = conductor._unit_bars()
  conductor.playback_bars = unit_bars
  conductor.auto_advance = False
  exact = pitches_of_bars(facts, unit_bars)
  classes = sorted({p % 12 for p in exact})
  wrong_note = list(exact)
  if wrong_note:
    wrong_note[len(wrong_note) // 2] += 1        # a semitone off
  dropped = [p for p in exact if p % 12 not in set(classes[:2])] if len(classes) > 2 else exact
  for label, phrase in (('exact', exact), ('one wrong note', wrong_note),
                        ('two pitch classes dropped', dropped)):
    print('  [student] plays %s (%d notes)' % (label, len(phrase)))
    conductor.post(MusicStart())
    conductor.post(MusicEnd(pitch_events=[(p, 0.3) for p in phrase]))
    await _drain(conductor)
  print('  [note] the middle case passes on purpose: comparison is at')
  print('         pitch-class granularity over a bar range, so one altered note')
  print('         inside a dense passage can hide. Per-note comparison needs the')
  print('         alignment pass (see docs/ARCHITECTURE.md, "known limitations").')

  conductor.post(SpeechEnd(text='stop'))
  await _drain(conductor)
  conductor.post(Shutdown())
  await _drain(conductor)
  _report(conductor, facts, session, tools)
  return 0


async def _drain(conductor: Conductor) -> None:
  """Let the conductor finish reacting to whatever is queued."""
  for _ in range(6):
    if conductor.queue.empty():
      await asyncio.sleep(0)
    if conductor.queue.empty():
      break
    event = await conductor.queue.get()
    await conductor.handle(event)
    if isinstance(event, Shutdown):
      return


async def cmd_asr_probe(cfg: TutorConfig, audio_path: Optional[str]) -> int:
  """Talk to a real ASR server and report what actually happened."""
  import time as _time

  from .audio import (detect_asr_api, find_test_clip, make_asr,
                      wav_to_pcm16_mono)

  clip = find_test_clip(audio_path)
  if not clip:
    print('No audio clip to probe with. Pass one: --asr-probe my_clip.wav',
          file=sys.stderr)
    return 2

  pcm, src_rate, duration = wav_to_pcm16_mono(clip, cfg.sample_rate)
  print('== ASR probe ==')
  print('  server      : %s' % cfg.asr_base_url)
  detected = detect_asr_api(cfg.asr_base_url)
  print('  api detected: %s%s' % (detected,
        '' if cfg.asr_api in ('auto', detected) else
        '  (config says %s)' % cfg.asr_api))
  print('  clip        : %s' % clip)
  print('  audio       : %.2fs @ %dHz -> %d bytes PCM @ %dHz'
        % (duration, src_rate, len(pcm), cfg.sample_rate))
  print('  biasing     : %s' % (cfg.asr_hotwords[:60] + '...'))

  asr = make_asr(cfg)
  print('  client      : %s' % type(asr).__name__)
  started = _time.monotonic()
  try:
    transcript = await asr.transcribe(pcm, cfg.sample_rate)
  except Exception as exc:  # noqa: BLE001 - report, do not traceback
    elapsed = _time.monotonic() - started
    print('\n  FAILED after %.2fs: %s' % (elapsed, exc))
    return 1
  elapsed = _time.monotonic() - started

  print('\n  transcript  : %r' % transcript.text)
  print('  confidence  : %.3f  (derived from avg_logprob)' % transcript.confidence)
  print('  no-speech   : %.3f' % transcript.no_speech_prob)
  print('  language    : %s' % (transcript.language or '-'))
  print('  latency     : %.2fs  (%.2fx realtime)'
        % (elapsed, elapsed / duration if duration else 0))

  words = set(re.findall(r"[a-z0-9#']+", transcript.text.lower()))
  wanted = {'measure', 'bar', 'major', 'minor'}
  found = sorted(wanted & words)
  numbers = re.findall(r'\b\d{1,3}\b', transcript.text)
  print('\n  domain check: %s' % (', '.join(found) or 'no music vocabulary heard'))
  print('  bar numbers : %s' % (', '.join(numbers) or 'none spoken'))
  if not transcript.text.strip():
    print('  note        : empty transcript - check the clip or the mic level')
  return 0


async def cmd_tts_probe(cfg: TutorConfig, text: Optional[str],
                        voice: Optional[str], roundtrip: bool,
                        voices: bool) -> int:
  """Synthesise speech with a real TTS server and check it is intelligible.

  The round-trip is the useful part: synthesise -> transcribe -> compare. A
  tutor whose own voice cannot be understood (or whose "A major" comes back as
  "A minor") is worse than a silent one, and this makes that measurable.
  """
  import time as _time

  import urllib.request

  from .audio.tts import OpenAiCompatTts
  from .audio.wav_io import read_wav

  if voices:
    url = '%s/audio/voices' % cfg.tts_base_url.rstrip('/')
    try:
      with urllib.request.urlopen(url, timeout=10) as resp:
        payload = json.loads(resp.read().decode('utf-8', 'replace'))
    except Exception as exc:  # noqa: BLE001
      print('could not list voices at %s: %s' % (url, exc))
      return 1
    listed = payload.get('voices') or []
    print('== TTS voices (%d) ==' % len(listed))
    for entry in listed:
      name = entry.get('id') or entry.get('name')
      grade = entry.get('overall_grade') or ''
      print('  %-22s %s' % (name, grade))
    return 0

  phrase = text or ('Measure fifty three. The key changes to A major, then '
                    'returns to A minor at bar sixty nine.')
  voice = voice or cfg.tts_voice
  tts = OpenAiCompatTts(cfg.tts_base_url, cfg.tts_model, voice,
                        out_dir=cfg.render_dir)

  print('== TTS probe ==')
  print('  server : %s' % cfg.tts_base_url)
  print('  model  : %s   voice: %s' % (cfg.tts_model, voice))
  print('  text   : %s' % phrase)

  started = _time.monotonic()
  sentences_before = tts.counter
  try:
    completed = await tts.speak(phrase, asyncio.Event())
  except Exception as exc:  # noqa: BLE001 - report, do not traceback
    print('\n  FAILED: %s' % exc)
    return 1
  elapsed = _time.monotonic() - started
  if not completed:
    print('\n  synthesis was cancelled')
    return 1

  import os
  import numpy as np
  produced = [os.path.join(cfg.render_dir, 'tts_%04d.wav' % i)
              for i in range(sentences_before + 1, tts.counter + 1)]
  produced = [p for p in produced if os.path.exists(p)]
  if not produced:
    print('\n  no audio file was written')
    return 1
  chunks, rate = [], cfg.sample_rate
  for path in produced:
    audio, rate = read_wav(path)
    chunks.append(audio)
  audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
  duration = audio.size / rate if rate else 0.0
  peak = float(abs(audio).max()) if audio.size else 0.0
  print('\n  audio  : %d file(s), last %s' % (len(produced), produced[-1]))
  print('  format : %dHz, %.2fs, peak %.3f' % (rate, duration, peak))
  print('  latency: %.2fs (%.2fx realtime)'
        % (elapsed, elapsed / duration if duration else 0))
  if peak < 0.02:
    print('  WARNING: almost silent output')

  if roundtrip:
    from .audio import make_asr
    asr = make_asr(cfg)
    # Resample to the ASR rate the way the live path would.
    from .audio.wav_io import pcm16, resample
    pcm = pcm16(resample(audio, rate, cfg.sample_rate))
    t0 = _time.monotonic()
    transcript = await asr.transcribe(pcm, cfg.sample_rate)
    print('\n  round-trip through %s:' % type(asr).__name__)
    print('    sent : %s' % phrase)
    print('    heard: %s' % transcript.text)
    print('    conf %.3f, no-speech %.3f, %.2fs'
          % (transcript.confidence, transcript.no_speech_prob,
             _time.monotonic() - t0))
    # Language-agnostic fact check: numbers compare by VALUE ("fifty three" ->
    # "53" is a success), plus letter tokens and Chinese character coverage.
    from .roundtrip import compare_roundtrip
    facts = compare_roundtrip(phrase, transcript.text)
    print('    numbers kept        : %s'
          % (', '.join(str(n) for n in facts['kept_numbers']) or 'none sent'))
    if facts['lost_numbers']:
      print('    NUMBERS LOST        : %s  <-- check this'
            % ', '.join(str(n) for n in facts['lost_numbers']))
    if facts['kept_letters']:
      print('    note/key symbols    : %s kept' % ', '.join(facts['kept_letters']))
    if facts['lost_letters']:
      print('    SYMBOLS LOST        : %s  <-- the letter never reached the audio'
            % ', '.join(facts['lost_letters']))
    if facts['lost_words']:
      print('    WORDS LOST          : %s  <-- check this'
            % ', '.join(facts['lost_words']))
    if facts['cjk_ratio'] < 1.0:
      print('    Chinese characters  : %.0f%% present' % (100 * facts['cjk_ratio']))
    print('    verdict             : %s'
          % ('round-trip clean' if facts['ok'] else 'FACTS LOST in synthesis'))
  return 0


async def cmd_mic_replay(cfg: TutorConfig, path: str,
                         threshold: Optional[float] = None,
                         hangover_ms: Optional[int] = None) -> int:
  """Run the live VAD+ASR pipeline over a WAV file instead of a microphone.

  This is how you tune segmentation without asking anyone to keep speaking: the
  same frames, the same segmenter, the same server, but from a file that can be
  replayed as often as you like.
  """
  from .audio import make_asr
  from .audio.base import AudioFrame
  from .audio.segmenter import UtteranceSegmenter
  from .audio.vad_energy import energy_stats
  from .audio.wav_io import wav_to_pcm16_mono

  pcm, src_rate, duration = wav_to_pcm16_mono(path, cfg.sample_rate)
  frame_bytes = int(cfg.sample_rate * cfg.frame_ms / 1000) * 2
  threshold = cfg.vad_energy_threshold if threshold is None else threshold
  hangover_ms = cfg.vad_hangover_ms if hangover_ms is None else hangover_ms
  segmenter = UtteranceSegmenter(energy_threshold=threshold,
                                hangover_ms=hangover_ms,
                                frame_ms=cfg.frame_ms)
  asr = make_asr(cfg)

  print('== mic replay (VAD path, no microphone) ==')
  print('  file      : %s' % path)
  print('  audio     : %.2fs @ %dHz -> %dHz mono, %s'
        % (duration, src_rate, cfg.sample_rate, energy_stats(pcm)['verdict']))
  print('  vad       : threshold %.4f rms, hangover %dms'
        % (threshold, hangover_ms))
  print('  asr       : %s\n' % type(asr).__name__)

  events = 0
  speech_frames = 0
  transcriptions = []
  for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
    frame = AudioFrame(pcm=pcm[offset:offset + frame_bytes],
                       sample_rate=cfg.sample_rate,
                       at=offset / 2.0 / cfg.sample_rate)
    for kind, utterance in segmenter.push(frame):
      if kind == 'speech_start':
        events += 1
        print('  [%5.2fs] speech start' % (offset / 2.0 / cfg.sample_rate))
      elif kind == 'speech_end':
        print('  [%5.2fs] speech end' % (offset / 2.0 / cfg.sample_rate))
      elif utterance is not None:
        speech_frames += utterance.frame_count
        print('  [%5.2fs] utterance %.2fs peak %.3f'
              % (offset / 2.0 / cfg.sample_rate, utterance.duration_s,
                 utterance.peak))
        transcript = await asr.transcribe(utterance.pcm, cfg.sample_rate)
        transcriptions.append(transcript.text)
        print('             %r  (conf %.2f)' % (transcript.text,
                                                transcript.confidence))

  pending = segmenter.flush()
  if pending is not None:
    transcript = await asr.transcribe(pending.pcm, cfg.sample_rate)
    transcriptions.append(transcript.text)
    print('  [  end] utterance %.2fs (flushed)\n             %r'
          % (pending.duration_s, transcript.text))

  print('\n  %d utterance(s), %.2fs of speech detected in %.2fs of audio'
        % (len(transcriptions), speech_frames * cfg.frame_ms / 1000.0, duration))
  print('  joined   : %r' % ' '.join(t for t in transcriptions if t))
  if len(transcriptions) > 2:
    print('  note     : speech is being chopped up - raise --hangover-ms or '
          'lower --vad-threshold')
  return 0


async def cmd_mic_oneshot(cfg: TutorConfig, seconds: float,
                          device: Optional[str] = None,
                          delay: float = 0.0) -> int:
  """Record a fixed stretch and transcribe all of it -- no VAD involved.

  This separates two very different problems: a microphone that is too quiet or
  mis-selected, versus a VAD that is chopping speech into fragments. If the
  whole-chunk transcript is right and the live test is wrong, the VAD is at
  fault; if both are wrong, it is the input signal.
  """
  import time as _time

  from .audio import make_asr
  from .audio.mic import list_input_sources, pick_source, record_raw
  from .audio.vad_energy import dbfs_of, energy_stats

  print('== one-shot capture test (no VAD) ==')
  for src in list_input_sources():
    print('  source: %-58s %dch %dHz %s'
          % (str(src['name'])[:58], src['channels'], src['rate'], src['state']))
  chosen = pick_source(device)
  print('  using : %s' % chosen)
  print('  record: %.1f s @ %dHz mono' % (seconds, cfg.sample_rate))
  if delay > 0:
    print('\n  starting in %.0f seconds...' % delay, flush=True)
    for remaining in range(int(delay), 0, -1):
      print('    %d' % remaining, flush=True)
      await asyncio.sleep(1)
  print('\n  RECORDING NOW -- speak the whole phrase.\n', flush=True)

  started = _time.monotonic()
  pcm = await asyncio.to_thread(record_raw, chosen, seconds, cfg.sample_rate)
  elapsed = _time.monotonic() - started
  stats = energy_stats(pcm)

  # Always keep the audio: a saved clip can be replayed against different server
  # settings (`--asr-probe <file>`) without asking anyone to speak again.
  from .audio.wav_io import pcm16_to_wav_bytes
  import datetime
  os.makedirs(cfg.render_dir, exist_ok=True)
  stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
  saved = os.path.join(cfg.render_dir, 'mic_%s.wav' % stamp)
  with open(saved, 'wb') as fh:
    fh.write(pcm16_to_wav_bytes(pcm, cfg.sample_rate))

  print('  captured %.2fs (%d bytes): rms %.4f, peak %.4f, %s'
        % (elapsed, len(pcm), stats['rms'], stats['peak'], stats['verdict']))
  print('  level : %.1f dBFS rms, %.1f dBFS sample peak'
        % (dbfs_of(pcm), 20 * __import__('math').log10(max(stats['peak'], 1e-9))))
  print('  saved : %s' % saved)
  print('         replay with: --asr-probe %s' % saved)

  if stats['rms'] < 0.005:
    print('\n  WARNING: that is very quiet. Check the input gain, or pick '
          'another source with --mic-device.')

  asr = make_asr(cfg)
  print('\n  transcribing the whole %.1fs chunk with %s...'
        % (seconds, type(asr).__name__), flush=True)
  t0 = _time.monotonic()
  transcript = await asr.transcribe(pcm, cfg.sample_rate)
  print('  transcript : %r' % transcript.text)
  print('  confidence : %.3f   no-speech %.3f   %.2fs'
        % (transcript.confidence, transcript.no_speech_prob,
           _time.monotonic() - t0))
  return 0


async def cmd_mic_test(cfg: TutorConfig, seconds: float,
                       device: Optional[str] = None,
                       threshold: Optional[float] = None) -> int:
  """Live microphone -> VAD -> ASR. Prints every utterance as it lands."""
  import time as _time

  from .audio import make_asr
  from .audio.mic import MicrophoneSource, list_input_sources
  from .audio.segmenter import UtteranceSegmenter
  from .audio.vad_energy import dbfs_of

  sources = list_input_sources()
  print('== microphone test ==')
  if sources:
    for src in sources:
      print('  source: %-60s %dch %dHz %s'
            % (str(src['name'])[:60], src['channels'], src['rate'], src['state']))
  else:
    print('  (no PulseAudio sources listed; trying the default device)')

  source = MicrophoneSource(device=device, rate=cfg.sample_rate,
                            frame_ms=cfg.frame_ms)
  print('  using : %s' % source.device)
  print('  format: %dHz mono, %dms frames' % (source.rate, source.frame_ms))
  threshold = cfg.vad_energy_threshold if threshold is None else threshold
  segmenter = UtteranceSegmenter(energy_threshold=threshold,
                                hangover_ms=cfg.vad_hangover_ms,
                                frame_ms=cfg.frame_ms)
  print('  vad   : energy threshold %.4f rms (=-%.1f dBFS), hangover %dms'
        % (threshold, -20 * __import__('math').log10(threshold),
           cfg.vad_hangover_ms))
  asr = make_asr(cfg)
  print('  asr   : %s at %s' % (type(asr).__name__, cfg.asr_base_url))
  print('\n  Speak now. Ctrl-C to stop%s.\n'
        % ('' if not seconds else ' (auto-stop after %.0fs)' % seconds))

  started = _time.monotonic()
  last_level = 0.0
  utterances = 0
  peak_seen = 0.0
  try:
    async for frame in source.frames():
      now = _time.monotonic()
      events = segmenter.push(frame)
      for kind, utterance in events:
        if kind == 'speech_start':
          print('  [%6.1fs] speech start' % (now - started), flush=True)
        elif utterance is not None:
          utterances += 1
          peak_seen = max(peak_seen, utterance.peak)
          print('  [%6.1fs] utterance %.2fs, peak %.3f -> transcribing...'
                % (now - started, utterance.duration_s, utterance.peak),
                flush=True)
          t0 = _time.monotonic()
          try:
            transcript = await asr.transcribe(utterance.pcm, source.rate)
          except Exception as exc:  # noqa: BLE001 - keep the meter running
            print('             ASR FAILED: %s' % exc, flush=True)
            continue
          elapsed = _time.monotonic() - t0
          if transcript.text:
            print('             %r' % transcript.text, flush=True)
            print('             confidence %.3f, no-speech %.3f, %.2fs'
                  % (transcript.confidence, transcript.no_speech_prob, elapsed),
                  flush=True)
          else:
            print('             (no words recognised: confidence %.3f, '
                  'no-speech %.3f, %.2fs)'
                  % (transcript.confidence, transcript.no_speech_prob, elapsed),
                  flush=True)
        elif kind == 'speech_end':
          print('  [%6.1fs] speech end' % (now - started), flush=True)
      if not segmenter.active and (now - last_level) > 2.0:
        last_level = now
        print('  [%6.1fs] idle, level %.4f rms (%.1f dBFS)'
              % (now - started, _level_of(frame), dbfs_of(frame.pcm)), flush=True)
      if seconds and (now - started) >= seconds:
        break
  except KeyboardInterrupt:
    pass
  except Exception as exc:  # noqa: BLE001 - report, do not traceback
    print('\n  microphone error: %s' % exc)
    return 1
  print('\n  stopped after %.1fs: %d utterance(s), loudest peak %.3f'
        % (_time.monotonic() - started, utterances, peak_seen))
  if utterances == 0:
    print('  nothing was detected - check the source with --mic-device, or '
          'lower --vad-threshold')
  return 0


def _level_of(frame) -> float:
  from .audio.vad_energy import rms
  return rms(frame.pcm)


async def cmd_voice(cfg: TutorConfig, mode: str, device: Optional[str],
                    threshold: Optional[float], seconds: float) -> int:
  """Full loop: microphone -> VAD -> ASR -> conductor -> spoken reply.

  With no TTS server configured the reply is printed (StubTts), so this tests
  the whole turn pipeline except synthesis.
  """
  import time as _time

  from .audio import make_asr
  from .audio.asr import looks_like_hallucination
  from .audio.mic import MicrophoneSource
  from .audio.segmenter import UtteranceSegmenter

  conductor, facts, session, tools, verifier = build(cfg, mode)
  source = MicrophoneSource(device=device, rate=cfg.sample_rate,
                            frame_ms=cfg.frame_ms)
  segmenter = UtteranceSegmenter(
      energy_threshold=cfg.vad_energy_threshold if threshold is None else threshold,
      hangover_ms=cfg.vad_hangover_ms, frame_ms=cfg.frame_ms)
  asr = make_asr(cfg)

  print('== voice session ==')
  print('  mic  : %s' % source.device)
  print('  asr  : %s at %s' % (type(asr).__name__, cfg.asr_base_url))
  print('  tts  : %s (no TTS server: replies are printed)'
        % type(conductor.tts).__name__)
  print('  mode : %s   Ctrl-C to stop\n' % conductor.state.mode.value)

  runner = asyncio.create_task(conductor.run(tick_s=0.1))
  if conductor.state.mode is Mode.DRILL:
    await conductor.respond('ask')
  else:
    await conductor.greet()
  print('\n  listening on %s -- speak whenever you are ready'
        % source.device.split('.')[-1], flush=True)
  started = _time.monotonic()
  half_duplex_muted = False
  last_level = 0.0
  from .audio.vad_energy import dbfs_of, rms
  try:
    async for frame in source.frames():
      now = _time.monotonic()
      # Heartbeat: without it a silent room looks identical to a broken app,
      # and there was no way to tell from the screen that it was listening.
      if not segmenter.active and (now - last_level) > 5.0:
        last_level = now
        level = rms(frame.pcm)
        print('  [idle] %.0fs  mic %.1f dBFS%s'
              % (now - started, dbfs_of(frame.pcm),
                 '  (very quiet - check the mic)' if level < 0.003 else ''),
              flush=True)
      # Half-duplex: while the tutor speaks, do not listen. There is no AEC, so
      # otherwise its own voice is captured and barges in on itself.
      if conductor.state.speaking and not cfg.voice_full_duplex:
        if not half_duplex_muted:
          half_duplex_muted = True
          segmenter.reset()
          print('  (tutor speaking: not listening)', flush=True)
        continue
      if half_duplex_muted:
        half_duplex_muted = False
        segmenter.reset()
        print('  (your turn)', flush=True)
      for kind, utterance in segmenter.push(frame):
        if kind == 'speech_start':
          # Also acts as barge-in if the tutor is mid-reply.
          conductor.post(SpeechStart())
        elif utterance is not None:
          transcript = await asr.transcribe(utterance.pcm, source.rate)
          if looks_like_hallucination(transcript.text,
                                      transcript.no_speech_prob,
                                      transcript.confidence):
            print('  [ignored: not speech (%r, no-speech %.2f, conf %.2f)]'
                  % (transcript.text[:40], transcript.no_speech_prob,
                     transcript.confidence), flush=True)
          elif transcript.text.strip():
            print('  [heard] %s' % transcript.text, flush=True)
            conductor.post(SpeechEnd(text=transcript.text,
                                     confidence=transcript.confidence))
      if seconds and (_time.monotonic() - started) >= seconds:
        break
  except KeyboardInterrupt:
    print('\n  interrupted')
  finally:
    conductor.post(Shutdown())
    try:
      await asyncio.wait_for(runner, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
      runner.cancel()
    _report(conductor, facts, session, tools)
  return 0


def cmd_text(cfg: TutorConfig, mode: str) -> int:
  conductor, facts, session, tools, verifier = build(cfg, mode)
  loop = asyncio.new_event_loop()
  asyncio.set_event_loop(loop)
  print('Commands: :drill :listen :teach :map :bars A B :play A [B] :play-wrong A',
        ':status :quit')
  if conductor.state.mode is Mode.DRILL:
    # In drill mode the first thing out of the tutor's mouth should be a
    # question, not an invitation to ask for one.
    loop.run_until_complete(conductor.respond('ask'))
  else:
    loop.run_until_complete(conductor.greet())
  try:
    while True:
      try:
        line = input('you> ').strip()
      except (EOFError, KeyboardInterrupt):
        break
      if not line:
        continue
      if line.startswith(':'):
        if line in (':quit', ':exit'):
          break
        _handle_command(line, conductor, facts, tools, session, loop)
        continue
      conductor.post(SpeechEnd(text=line))
      loop.run_until_complete(_drain(conductor))
  finally:
    conductor.post(Shutdown())
    loop.run_until_complete(_drain(conductor))
    _report(conductor, facts, session, tools)
    loop.close()
  return 0


def _handle_command(line: str, conductor, facts, tools, session, loop) -> None:
  parts = line.split()
  cmd = parts[0]
  if cmd == ':drill':
    conductor.state.mode = Mode.DRILL
    print('(mode: drill)')
    conductor.post(SpeechEnd(text='drill me'))
    loop.run_until_complete(_drain(conductor))
    return
  if cmd == ':listen':
    conductor.state.mode = Mode.LISTEN_COACH
    print('(mode: listen and coach) — play, and I will comment at phrase ends')
    return
  if cmd == ':teach':
    conductor.state.mode = Mode.TEACH
    print('(mode: teach)')
    return
  if cmd == ':map':
    print(facts.spine())
    return
  if cmd == ':bars' and len(parts) >= 3:
    for b in range(int(parts[1]), int(parts[2]) + 1):
      print(' ', facts.bar_line(b))
    return
  if cmd == ':status':
    print(json.dumps(session.status(), indent=2))
    if session.weak_spot_prompts():
      print('weak spots:', '; '.join(session.weak_spot_prompts()))
    return
  if cmd in (':play', ':play-wrong') and len(parts) >= 2:
    bars = _bar_range(parts[1:])
    drop = 2 if cmd == ':play-wrong' else 0
    conductor.playback_bars = bars
    conductor.post(MusicStart())
    conductor.post(MusicEnd(pitch_events=[
        (p, 0.3) for p in pitches_of_bars(facts, bars, drop=drop)]))
    loop.run_until_complete(_drain(conductor))
    return
  print('unknown command')


def _bar_range(parts: List[str]) -> List[int]:
  if len(parts) >= 2:
    return list(range(int(parts[0]), int(parts[1]) + 1))
  return [int(parts[0])]


def _report(conductor, facts, session, tools) -> None:
  print('\n' + '=' * 70)
  print('SESSION')
  print('=' * 70)
  for turn in conductor.transcript:
    print(' trigger=%-16s user=%-34r tools=%s grounded=%s'
          % (turn['trigger'], turn['user'], ','.join(turn['tools']) or '-',
             turn['grounded']))
    print('   think %.0fms + speech %.0fms = %.0fms total'
          % (turn.get('think_ms', 0), turn.get('playback_ms', 0),
             turn['latency_ms']))
    if not turn['grounded']:
      print('   (blocked claims: %s; %d bars citable)'
            % (turn.get('ungrounded'), turn.get('known_bars', 0)))
    print('   tutor: %s' % turn['reply'])
  print('\n%s' % session.summary())
  if session.weak_spot_prompts():
    print('weak spots: %s' % '; '.join(session.weak_spot_prompts()))
  print('log: %s' % session.log_path)
  print('\nQUESTION BANK PROVENANCE')
  print(json.dumps(facts.provenance_report()['by_source'], indent=2))


def main(argv: Optional[List[str]] = None) -> int:
  ap = argparse.ArgumentParser(description='Voice-first conversational music tutor (skeleton)')
  ap.add_argument('--text', action='store_true', help='interactive text session')
  ap.add_argument('--scripted', action='store_true', help='run a scripted demo end to end')
  ap.add_argument('--demo-grounding', action='store_true',
                  help='show the grounding verifier catching a fabricated fact')
  ap.add_argument('--asr-probe', nargs='?', const='', default=None,
                  metavar='AUDIO',
                  help='transcribe an audio file with the configured ASR server '
                       'and report the result (default: a synthesised clip)')
  ap.add_argument('--piece', default=None,
                  help='which piece to drill (slug, e.g. "chopin-waltz-..." )')
  ap.add_argument('--list-pieces', action='store_true',
                  help='show every question bank on disk')
  ap.add_argument('--doctor', action='store_true',
                  help='check the environment, question bank, servers and mic')
  ap.add_argument('--asr-url', default=None,
                  help='ASR server base URL, e.g. http://192.168.2.174:9000')
  ap.add_argument('--asr-api', default=None,
                  help='override API shape: auto | openai | whisper_asr')
  ap.add_argument('--tts-probe', nargs='?', const='', default=None,
                  metavar='TEXT', help='synthesise TEXT with the configured TTS '
                                       'server')
  ap.add_argument('--tts-url', default=None,
                  help='TTS server base URL, e.g. http://192.168.2.174:8880/v1')
  ap.add_argument('--tts-voice', default=None, help='voice name, e.g. af_heart')
  ap.add_argument('--tts-voices', action='store_true', help='list TTS voices')
  ap.add_argument('--roundtrip', action='store_true',
                  help='with --tts-probe: transcribe the synthesised audio back')
  ap.add_argument('--mic-test', action='store_true',
                  help='live microphone -> VAD -> ASR test')
  ap.add_argument('--mic-oneshot', type=float, default=None, metavar='SECONDS',
                  help='record SECONDS and transcribe it whole (no VAD): '
                       'calibrates the mic and isolates VAD problems')
  ap.add_argument('--voice', action='store_true',
                  help='full voice loop: mic -> ASR -> tutor (replies printed)')
  ap.add_argument('--mic-device', default=None,
                  help='capture source name (substring match)')
  ap.add_argument('--mic-seconds', type=float, default=0.0,
                  help='stop after N seconds (0 = until Ctrl-C)')
  ap.add_argument('--mic-delay', type=float, default=0.0,
                  help='seconds to wait before recording (mic-oneshot)')
  ap.add_argument('--mic-replay', default=None, metavar='WAV',
                  help='replay a WAV through the live VAD+ASR pipeline')
  ap.add_argument('--hangover-ms', type=int, default=None,
                  help='VAD hangover in ms (mic-replay)')
  ap.add_argument('--vad-threshold', type=float, default=None,
                  help='VAD energy threshold in rms (default from config)')
  ap.add_argument('--mode', default='converse',
                  choices=['teach', 'drill', 'listen_coach', 'converse', 'review'])
  ap.add_argument('--fp', default=None, help='path to fingerprint.json')
  ap.add_argument('--tier-max', type=int, default=None)
  ap.add_argument('--log', default=None)
  args = ap.parse_args(argv)

  cfg = TutorConfig.from_env()
  if args.fp:
    cfg.fingerprint = args.fp
  if args.tier_max is not None:
    cfg.tier_max = args.tier_max
  if args.log:
    cfg.log_path = args.log
  # All configuration overrides are applied BEFORE any command is dispatched:
  # handling --asr-url after the TTS branch once meant a round-trip silently
  # used the stub recogniser instead of the real server.
  if args.asr_url:
    cfg.asr_base_url = args.asr_url
    cfg.asr = 'openai'            # 'openai' means "use the HTTP client"
  if args.asr_api:
    cfg.asr_api = args.asr_api
  if args.tts_url:
    cfg.tts_base_url = args.tts_url
    cfg.tts = 'openai'
  if args.tts_voice:
    cfg.tts_voice = args.tts_voice
  if args.mic_device:
    cfg.mic_device = args.mic_device
  if args.piece:
    cfg.piece = args.piece

  if args.list_pieces:
    from .pieces import listing
    print('question banks on disk (newest first):')
    for line in listing():
      print('  %s' % line)
    return 0
  if args.doctor:
    from .doctor import run_doctor
    return asyncio.run(run_doctor(cfg))
  if args.tts_voices:
    return asyncio.run(cmd_tts_probe(cfg, None, None, False, True))
  if args.tts_probe is not None:
    return asyncio.run(cmd_tts_probe(cfg, args.tts_probe or None,
                                     args.tts_voice, args.roundtrip, False))
  if args.asr_probe is not None:
    return asyncio.run(cmd_asr_probe(cfg, args.asr_probe or None))
  if args.mic_replay:
    return asyncio.run(cmd_mic_replay(cfg, args.mic_replay,
                                      args.vad_threshold, args.hangover_ms))
  if args.mic_oneshot is not None:
    return asyncio.run(cmd_mic_oneshot(cfg, args.mic_oneshot, args.mic_device,
                                       args.mic_delay))
  if args.mic_test:
    return asyncio.run(cmd_mic_test(cfg, args.mic_seconds,
                                    args.mic_device, args.vad_threshold))
  if args.voice:
    return asyncio.run(cmd_voice(cfg, args.mode, args.mic_device,
                                 args.vad_threshold, args.mic_seconds))
  if not os.path.exists(cfg.fingerprint):
    print('No fingerprint at %s. Build one first:\n'
          '  .venv/bin/python memorization/analyze.py <score.musicxml>'
          % cfg.fingerprint, file=sys.stderr)
    return 2

  if args.demo_grounding:
    return cmd_demo_grounding(cfg)
  if args.scripted:
    return asyncio.run(cmd_scripted(cfg, args.mode))
  if args.text:
    return cmd_text(cfg, args.mode)
  ap.print_help()
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
