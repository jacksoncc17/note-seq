"""Audio seams: VAD, ASR, TTS and the instrument channel.

Each is a Protocol with a stub implementation that runs offline and a
self-hosted adapter that speaks an OpenAI-compatible HTTP API. The conductor
only ever sees the protocols, so swapping a stub for a real model is a config
change.

Design note: the *instrument* channel is deliberately separate from ASR. Speech
and piano have different failure modes, and merging them into one microphone
stream is the single biggest source of nonsense in a music tutor.
"""

from .base import Asr, AudioFrame, InstrumentSource, Transcriber, Tts, Vad
from .vad_energy import EnergyVad
from .asr import (OpenAiCompatAsr, StubAsr, WhisperAsrWebservice,
                  detect_asr_api, make_asr, parse_openai_asr, parse_whisper_asr)
from .tts import OpenAiCompatTts, StubTts
from .instrument import MidiInstrument, StubInstrument
from .wav_io import find_test_clip, pcm16, pcm16_to_wav_bytes, read_wav, resample
from .wav_io import wav_to_pcm16_mono

__all__ = [
    'Asr', 'AudioFrame', 'InstrumentSource', 'Transcriber', 'Tts', 'Vad',
    'EnergyVad', 'StubAsr', 'OpenAiCompatAsr', 'WhisperAsrWebservice',
    'detect_asr_api', 'make_asr', 'parse_openai_asr', 'parse_whisper_asr',
    'StubTts', 'OpenAiCompatTts',
    'StubInstrument', 'MidiInstrument',
    'read_wav', 'resample', 'pcm16', 'pcm16_to_wav_bytes', 'wav_to_pcm16_mono',
    'find_test_clip',
]
