"""Tutor configuration.

Everything tunable lives here so a real deployment differs from the demo only
by env vars: point `TUTOR_LLM_BASE_URL` at vLLM / llama.cpp / Ollama and set
`TUTOR_LLM=openai`, and the same conductor drives a real model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

# Anchor default paths to the repository root so the tutor runs from anywhere.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _under_root(*parts: str) -> str:
  return os.path.join(_REPO_ROOT, *parts)


def _env(name: str, default):
  raw = os.environ.get(name)
  if raw is None:
    return default
  if isinstance(default, bool):
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')
  if isinstance(default, int):
    return int(raw)
  if isinstance(default, float):
    return float(raw)
  return raw


@dataclass
class TutorConfig:
  # --- ground truth -------------------------------------------------------
  # Which piece to drill. Empty means "the most recent bank" (see pieces.py).
  piece: str = ''
  fingerprint: str = _under_root('memorization', 'output', 'fingerprint.json')
  # Highest question tier the drill may use: 0 structure, 1 harmony,
  # 2 surface, 3 performance. Performance needs real audio grading.
  tier_max: int = 1

  # --- components ---------------------------------------------------------
  llm: str = 'stub'                 # 'stub' | 'openai'
  llm_base_url: str = 'http://127.0.0.1:8000/v1'
  llm_model: str = 'local-model'
  llm_api_key: str = 'not-needed'
  llm_timeout_s: float = 60.0
  llm_max_tool_rounds: int = 4

  asr: str = 'stub'                 # 'stub' | 'openai'
  # Which HTTP shape the server speaks: 'auto' reads its /openapi.json, or set
  # it explicitly to 'openai' (/v1/audio/transcriptions) or 'whisper_asr'
  # (ahmetoner/whisper-asr-webservice's native POST /asr).
  asr_api: str = 'auto'
  asr_base_url: str = 'http://127.0.0.1:8080/v1'
  asr_model: str = 'whisper-1'
  asr_language: str = 'en'
  asr_timeout_s: float = 120.0
  asr_vad_filter: bool = False
  # The biasing prompt is the one ASR lever that reliably fixes musical
  # vocabulary: a live probe turned "measure fifty three" into "Measure 53".
  # Command words belong here too -- "drill me" was heard as "Giormi" twice
  # before they were added.
  asr_hotwords: str = ('measure, bar, key, major, minor, A minor, A major, '
                       'E minor, D minor, B major, E major, C major, '
                       'ritardando, sostenuto, coda, phrase, beat, hand, '
                       'left hand, right hand, drill me, quiz me, listen, '
                       'stop, teach me, next question, repeat that, '
                       'say again, how am I doing')

  tts: str = 'stub'                 # 'stub' | 'openai'
  tts_base_url: str = 'http://127.0.0.1:8880/v1'
  tts_model: str = 'kokoro'
  tts_voice: str = 'af_heart'
  # Language code for non-English voices ('z' = Mandarin). Measured on a live
  # Kokoro server: it is accepted and passed through, and on zf_xiaoxiao the
  # Chinese output was identical with and without it -- so this is correctness
  # by the schema rather than a knob that changed the result.
  tts_lang_code: str = ''
  tts_play: bool = True             # play replies through the speakers/headset

  instrument: str = 'stub'          # 'stub' | 'midi'
  midi_port: str = ''               # substring match, '' = first available

  # --- audio / turn taking ------------------------------------------------
  sample_rate: int = 16000
  frame_ms: int = 20
  mic_device: str = ''              # substring match; '' = auto (prefers USB mic)
  # Hangover is the single most important ASR accuracy knob here, measured
  # against a real 10s clip: 300ms split one spoken turn into 6 fragments and
  # corrupted the values that matter ("53" -> "f3", "bar 69" -> "bar 60"),
  # because Whisper needs sentence context to render numbers correctly.
  # 600ms gave 3 fragments, 900ms gave 1 utterance and a perfect transcript.
  # A tutor's unit of turn is a sentence, not a syllable gap.
  vad_energy_threshold: float = 0.012
  vad_hangover_ms: int = 900      # speech keeps the floor this long after energy drops
  music_endpoint_silence_ms: int = 600   # instrument silence => phrase boundary
  coach_delay_ms: int = 900         # LISTEN_COACH: pause after a phrase before commenting
  duck_tts_while_playing: bool = True    # never talk over the student
  # Half-duplex by default: with no echo cancellation, listening while the tutor
  # speaks mainly captures the tutor, and its own voice then barges in on
  # itself. Set True once AEC (or a good headset) is in place.
  voice_full_duplex: bool = False

  # --- grounding ----------------------------------------------------------
  grounding_policy: str = 'block'   # 'block' | 'hedge' | 'warn'
  grounding_retry: int = 1

  # --- state --------------------------------------------------------------
  log_path: str = _under_root('tutor', 'output', 'session.jsonl')
  render_dir: str = _under_root('tutor', 'output')

  @classmethod
  def from_env(cls) -> 'TutorConfig':
    cfg = cls()
    for f in fields(cls):
      if isinstance(f.default, (str, int, float, bool)):
        setattr(cfg, f.name, _env('TUTOR_' + f.name.upper(), f.default))
    return cfg
