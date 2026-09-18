"""Voice-first conversational music tutor (skeleton).

Layers:
  tutor.events     - the conductor's input alphabet
  tutor.modes      - mode state machine and floor control
  tutor.audio      - VAD / ASR / TTS / instrument seams (stub + self-hosted adapters)
  tutor.core       - deterministic ground truth: facts, gate, tools
  tutor.llm        - LLM clients (scripted stub + self-hosted OpenAI-compatible)
  tutor.grounding  - post-generation claim verification
  tutor.conductor  - orchestrates the whole loop
  tutor.cli        - runnable entry point
"""

__all__ = ['config', 'events', 'modes', 'grounding', 'session', 'conductor']
