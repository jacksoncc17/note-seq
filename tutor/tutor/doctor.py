"""Environment self-check: `python -m tutor.cli --doctor`.

Answers "why is nothing working" in one command instead of five: it verifies the
interpreter, the dependencies, the question bank, both servers (with a real
request, not just a ping), the microphone, and audio playback -- then prints the
commands to run.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from typing import List, Tuple

OK = 'ok'
WARN = 'warn'
FAIL = 'fail'

Result = Tuple[str, str, str, str]      # status, name, detail, hint


def _check_python() -> Result:
  in_venv = sys.prefix != sys.base_prefix
  detail = 'Python %s at %s' % (sys.version.split()[0], sys.executable)
  if not in_venv:
    return (WARN, 'interpreter', detail,
            'not a virtualenv; use ../.venv/bin/python or ./run.sh')
  return (OK, 'interpreter', detail, '')


def _check_dependencies() -> List[Result]:
  out = []
  for module in ('note_seq', 'numpy'):
    try:
      importlib.import_module(module)
      out.append((OK, 'dependency: %s' % module, 'importable', ''))
    except Exception as exc:  # noqa: BLE001
      out.append((FAIL, 'dependency: %s' % module, str(exc),
                  'install with .venv/bin/pip install -e .'))
  return out


def _check_fingerprint(cfg) -> Result:
  from .pieces import available, resolve
  try:
    path, slug, _why = resolve(cfg)
  except SystemExit as exc:
    return (FAIL, 'question bank', str(exc).splitlines()[0],
            '.venv/bin/python memorization/analyze.py <score.musicxml>')
  cfg.fingerprint, cfg.piece = path, slug
  try:
    import json
    with open(cfg.fingerprint) as fh:
      data = json.load(fh)
    facts = data.get('global', {})
    others = len(available()) - 1
    extra = '' if others <= 0 else ' (%d other piece(s): --list-pieces)' % others
    return (OK, 'question bank', '%s - %d bars, %d questions%s'
            % (slug, facts.get('num_measures', 0),
               len(data.get('questions', [])), extra), '')
  except Exception as exc:  # noqa: BLE001
    return (FAIL, 'question bank', 'unreadable: %s' % exc, 're-run analyze.py')


async def _check_asr(cfg) -> List[Result]:
  from .audio.asr import detect_asr_api, make_asr
  out: List[Result] = []
  if cfg.asr == 'stub':
    out.append((WARN, 'ASR', 'set to stub: no server will be used',
                    'export TUTOR_ASR=openai (run.sh does this for voice/mic)'))
    return out
  api = detect_asr_api(cfg.asr_base_url, timeout_s=5.0)
  out.append((OK, 'ASR endpoint', '%s -> %s API'
              % (cfg.asr_base_url, api), ''))
  try:
    asr = make_asr(cfg)
    # A real request: one second of silence must come back as an empty
    # transcript. Reachability alone does not prove the endpoint works.
    quiet = b'\x00' * (cfg.sample_rate * 2)
    transcript = await asr.transcribe(quiet, cfg.sample_rate)
    out.append((OK, 'ASR request', 'answered in %s; empty clip -> %r'
                % (type(asr).__name__, transcript.text[:30]), ''))
  except Exception as exc:  # noqa: BLE001
    out.append((FAIL, 'ASR request', str(exc)[:150],
                'check the server address and that it is running'))
  return out


async def _check_tts(cfg) -> List[Result]:
  from .audio.tts import OpenAiCompatTts
  out: List[Result] = []
  if cfg.tts == 'stub':
    out.append((WARN, 'TTS', 'set to stub: replies are printed, not spoken',
                    'export TUTOR_TTS=openai (run.sh does this for voice)'))
    return out
  tts = OpenAiCompatTts(cfg.tts_base_url, cfg.tts_model, cfg.tts_voice,
                        out_dir=cfg.render_dir, lang_code=cfg.tts_lang_code,
                        verbose=False)
  # Track the sentence counter, not a directory diff: the counter restarts each
  # process, so a new reply simply overwrites tts_0001.wav and a before/after
  # glob shows nothing new.
  before = tts.counter
  try:
    completed = await tts.speak('check', asyncio.Event())
  except Exception as exc:  # noqa: BLE001
    out.append((FAIL, 'TTS request', str(exc)[:150],
                'check the server address and that it is running'))
    return out
  produced = [os.path.join(cfg.render_dir, 'tts_%04d.wav' % i)
              for i in range(before + 1, tts.counter + 1)]
  produced = [p for p in produced if os.path.exists(p)]
  if not completed or not produced:
    out.append((FAIL, 'TTS request', 'no audio produced', 'check the server logs'))
    return out
  path = produced[-1]
  try:
    from .audio.wav_io import read_wav
    data, rate = read_wav(path)
    out.append((OK, 'TTS request', '%s / %s -> %.2fs at %dHz'
                % (cfg.tts_model, cfg.tts_voice, data.size / rate if rate else 0,
                   rate), ''))
  except Exception as exc:  # noqa: BLE001
    out.append((WARN, 'TTS request', 'audio written but unreadable: %s' % exc, ''))
  return out


def _check_microphone() -> List[Result]:
  from .audio.mic import capture_command, list_input_sources, record_raw
  from .audio.vad_energy import energy_stats
  out: List[Result] = []
  sources = list_input_sources()
  if not sources:
    out.append((FAIL, 'microphone', 'no capture sources found',
                'install pulseaudio-utils, or check the audio server'))
    return out
  out.append((OK, 'microphone', '%d source(s): %s'
              % (len(sources), ', '.join(s['name'].split('.')[-1] for s in sources)), ''))
  try:
    capture_command(None, 16000)
  except Exception as exc:  # noqa: BLE001
    out.append((FAIL, 'capture tool', str(exc), 'install parecord or arecord'))
    return out
  try:
    pcm = record_raw(None, 1.0)
    stats = energy_stats(pcm)
    if not pcm:
      out.append((FAIL, 'capture test', 'no audio arrived', 'check the device'))
    else:
      out.append((OK, 'capture test', '1.0s, rms %.4f (%s)'
                  % (stats['rms'], stats['verdict']), ''))
  except Exception as exc:  # noqa: BLE001
    out.append((FAIL, 'capture test', str(exc)[:120], 'check the device'))
  return out


def _check_playback() -> Result:
  import shutil
  player = shutil.which('paplay') or shutil.which('aplay')
  if not player:
    return (WARN, 'playback', 'no paplay/aplay: replies will be silent',
            'install pulseaudio-utils or alsa-utils')
  return (OK, 'playback', player, '')


async def run_doctor(cfg) -> int:
  from .config import TutorConfig
  from .core.facts import FactStore

  print('== tutor self-check ==')
  print('  fingerprint: %s' % cfg.fingerprint)
  print('  ASR        : %s (%s)' % (cfg.asr, cfg.asr_base_url))
  print('  TTS        : %s (%s, voice %s)' % (cfg.tts, cfg.tts_base_url,
                                              cfg.tts_voice))
  print('  LLM        : %s%s\n' % (cfg.llm, ''
        if cfg.llm == 'stub' else ' (%s)' % cfg.llm_base_url))

  results: List[Result] = [_check_python()]
  results += _check_dependencies()
  results.append(_check_fingerprint(cfg))
  if not os.path.exists(cfg.fingerprint):
    results += [(WARN, 'ASR request', 'skipped (no question bank)', ''),
                (WARN, 'TTS request', 'skipped (no question bank)', '')]
  else:
    try:
      FactStore(cfg.fingerprint)          # fail loudly, not mid-lesson
      results.append((OK, 'fact model', 'loads and gates questions', ''))
    except Exception as exc:  # noqa: BLE001
      results.append((FAIL, 'fact model', str(exc)[:120], 're-run analyze.py'))
    results += await _check_asr(cfg)
    results += await _check_tts(cfg)
  results += _check_microphone()
  results.append(_check_playback())

  print('  %-16s %-24s %s' % ('STATUS', 'CHECK', 'DETAIL'))
  for status, name, detail, hint in results:
    mark = {'ok': 'ok  ', 'warn': 'WARN', 'fail': 'FAIL'}[status]
    print('  %-16s %-24s %s' % (mark, name, detail))
    if hint and status != OK:
      print('  %-16s %-24s -> %s' % ('', '', hint))

  failures = sum(1 for r in results if r[0] == FAIL)
  warnings = sum(1 for r in results if r[0] == WARN)
  print('\n  %d checks: %d failed, %d warnings'
        % (len(results), failures, warnings))
  if failures == 0:
    print('\n  ready. try:')
    print('    ./run.sh quiz          drill questions in the terminal (no servers)')
    print('    ./run.sh voice         voice conversation (needs ASR + TTS)')
    print('    ./run.sh mic           microphone + VAD test')
    print('    ./run.sh lessons       scripted demo of the whole loop')
  else:
    print('\n  fix the FAIL rows above first; WARN rows are optional.')
  return 1 if failures else 0
