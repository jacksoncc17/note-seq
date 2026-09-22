"""The conductor: wires audio in, the mode machine, the LLM tool loop,
grounding verification, and speech out.

Latency-shaped: the tool loop is the only blocking LLM work, and speech starts
only after verification. Music analysis never blocks the voice path -- the
comparison is deterministic and local.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

from .config import TutorConfig
from .core.facts import FactStore
from .core.registry import TOOL_SPECS
from .core.tools import ToolBox
from .events import (BargeIn, Event, MusicEnd, MusicStart, Shutdown, SpeechEnd,
                     SpeechStart, Tick)
from .grounding import Evidence, GroundingVerifier
from .llm.base import LlmClient
from .llm.prompts import build_messages
from .modes import (ConductorState, Decision, Floor, Mode, on_event,
                    wants_question_after_answer)
from .session import Session


class Conductor:
  def __init__(self, cfg: TutorConfig, facts: FactStore, tools: ToolBox,
               session: Session, llm: LlmClient, tts, verifier: GroundingVerifier,
               mode: str = 'converse', auto_advance: bool = True):
    self.cfg = cfg
    self.facts = facts
    self.tools = tools
    self.session = session
    self.llm = llm
    self.tts = tts
    self.verifier = verifier
    self.evidence = verifier.evidence
    self.state = ConductorState(mode=Mode(mode))
    self.history: List[dict] = []
    self.queue: asyncio.Queue = asyncio.Queue()
    self._tts_cancel = asyncio.Event()
    self._tts_task: Optional[asyncio.Task] = None
    self.auto_advance = auto_advance
    self._auto_ask_on_mode_change = False
    self.greeted = False
    # Which bars the student is currently playing. The tutor sets this when it
    # asks for a passage; a production system would instead infer it by
    # aligning the played notes to the score.
    self.playback_bars: Optional[List[int]] = None
    self.transcript: List[dict] = []

  # -- lifecycle ----------------------------------------------------------

  async def run(self, tick_s: float = 0.1) -> None:
    ticker = asyncio.create_task(self._ticker(tick_s))
    try:
      while True:
        event = await self.queue.get()
        await self.handle(event)
        if isinstance(event, Shutdown):
          break
    finally:
      ticker.cancel()
      self._cancel_tts()
      if self._tts_task:
        try:
          await self._tts_task
        except (asyncio.CancelledError, Exception):
          pass

  async def _ticker(self, tick_s: float) -> None:
    while True:
      await asyncio.sleep(tick_s)
      await self.queue.put(Tick())

  def post(self, event: Event) -> None:
    self.queue.put_nowait(event)

  # -- event handling -----------------------------------------------------

  async def handle(self, event: Event) -> None:
    state, decisions, context = on_event(self.state, event, self.cfg)
    self.state = state
    self.state.pending_question = self.tools.pending is not None
    for decision in decisions:
      await self._act(decision, context)

  async def _act(self, decision: Decision, context: Dict) -> None:
    if decision is Decision.CANCEL_TTS:
      self._cancel_tts()
      return
    if decision is Decision.STAY_SILENT:
      return
    if decision is Decision.RESPOND_TEXT:
      text = (context.get('text') or '').strip()
      if self.state.mode is Mode.DRILL and self.tools.pending is not None:
        await self.respond('answer_text', text)
      else:
        await self.respond('converse_text', text)
      return
    if decision is Decision.COMMENT_PLAYING:
      await self._comment_on_playing(context)
      return
    if decision is Decision.ASK:
      await self.respond('ask')

  async def _comment_on_playing(self, context: Dict) -> None:
    played = list(context.get('played_pitches') or self.state.played_pitches or [])
    bars = self.playback_bars or self._unit_bars()
    result = self.tools.compare_played(bars, played)
    self.evidence.observe(result)
    await self.respond('playing_feedback', result.summary,
                       extra_state={'last_evidence': result.summary,
                                    'compared_bars': bars})

  def _unit_bars(self) -> List[int]:
    unit = self.tools.unit or (self.facts.sections[0] if self.facts.sections else None)
    if not unit:
      return [1]
    start = int(unit.get('start_measure', 1))
    end = int(unit.get('end_measure', start))
    return list(range(start, min(end, start + 7) + 1))

  # -- the response pipeline ---------------------------------------------

  async def respond(self, trigger: str, user_text: str = '',
                    extra_state: Optional[Dict] = None,
                    depth: int = 0) -> Optional[str]:
    started = time.monotonic()
    self.tts_error: Optional[str] = None
    if user_text:
      self.history.append({'role': 'user', 'content': user_text})
    state = self._state_block(trigger, extra_state)
    messages = build_messages(state, self.history)
    text, tool_log = await self._tool_loop(messages)

    if not text:
      # The model only made tool calls: speak the last grounded result.
      text = tool_log[-1]['summary'] if tool_log else 'I do not have an answer for that.'

    report = self.verifier.check(text)
    repaired = self.verifier.repair(text, report)
    if repaired is None and self.cfg.grounding_retry > 0 and report.ungrounded:
      messages.append({'role': 'assistant', 'content': text})
      messages.append({'role': 'user', 'content':
                       'Do not state that. You may only use these facts: %s'
                       % ' | '.join(self.evidence.cite())})
      retry_text, retry_log = await self._tool_loop(messages)
      if retry_text:
        report = self.verifier.check(retry_text)
        repaired = self.verifier.repair(retry_text, report)
        text = retry_text
        tool_log.extend(retry_log)
    if repaired is None:
      repaired = ('I would rather not guess. Ask me about a bar number or say '
                  'drill me.')
      report = self.verifier.check(repaired)

    speak_started = time.monotonic()
    think_ms = (speak_started - started) * 1000
    spoken = await self._speak(repaired)
    playback_ms = (time.monotonic() - speak_started) * 1000
    latency_ms = (time.monotonic() - started) * 1000
    self.history.append({'role': 'assistant', 'content': repaired})
    self.session.log_turn(trigger=trigger, user_text=user_text,
                          tool_calls=tool_log, reply=repaired,
                          grounding=report.as_dict(), spoken_fully=spoken,
                          latency_ms=latency_ms, think_ms=think_ms,
                          playback_ms=playback_ms)
    self.transcript.append({'trigger': trigger, 'user': user_text,
                            'reply': repaired,
                            # Full records, not just names: the summaries are the
                            # evidence the reply was allowed to cite, and a
                            # client showing "what the tutor looked up" wants
                            # them.
                            'tools': [{'name': t['name'], 'ok': t.get('ok'),
                                       'summary': (t.get('summary') or '')[:200]}
                                      for t in tool_log],
                            'grounded': report.ok, 'spoken_fully': spoken,
                            'ungrounded': [(c.kind, c.value)
                                           for c in report.ungrounded],
                            'known_bars': len(report.known_bars),
                            'think_ms': round(think_ms, 1),
                            'playback_ms': round(playback_ms, 1),
                            'latency_ms': round(latency_ms, 1)})

    # Drill keeps going by itself: answer -> next question. Switching into
    # drill also asks immediately, so "drill me" is answered with a question
    # rather than a bare acknowledgement. Depth-capped so a model that keeps
    # requesting a mode change cannot spin forever.
    if depth >= 2:
      return repaired
    if self.auto_advance and self._auto_ask_on_mode_change:
      self._auto_ask_on_mode_change = False
      await self.respond('ask', depth=depth + 1)
    elif (trigger in ('answer_text', 'playing_feedback') and self.auto_advance
          and wants_question_after_answer(self.state)):
      await self.respond('ask', depth=depth + 1)
    return repaired

  async def _tool_loop(self, messages: List[dict]):
    tool_log: List[dict] = []
    text = ''
    for _ in range(max(1, self.cfg.llm_max_tool_rounds)):
      reply = await self.llm.complete(messages, TOOL_SPECS)
      if not reply.tool_calls:
        text = (reply.text or '').strip()
        break
      messages.append({
          'role': 'assistant', 'content': reply.text or '',
          'tool_calls': [{'id': c.id, 'type': 'function',
                          'function': {'name': c.name,
                                       'arguments': _json(c.arguments)}}
                         for c in reply.tool_calls]})
      for call in reply.tool_calls:
        result = self.tools.dispatch(call.name, call.arguments)
        self.evidence.observe(result)
        if call.name == 'set_mode' and result.ok:
          self._apply_mode(str((result.data or {}).get('mode', '')))
        self.state.pending_question = self.tools.pending is not None
        tool_log.append({'name': call.name, 'args': call.arguments,
                         'ok': result.ok, 'summary': result.summary})
        messages.append({'role': 'tool', 'tool_call_id': call.id,
                         'name': call.name, 'content': result.as_text()})
      text = (reply.text or '').strip() or text
    return text, tool_log

  def _apply_mode(self, name: str) -> None:
    try:
      mode = Mode(name.strip().lower())
    except ValueError:
      self.session.log({'event': 'bad_mode', 'mode': name})
      return
    if mode is not self.state.mode:
      self.session.log({'event': 'mode_change', 'from': self.state.mode.value,
                        'to': mode.value})
      # Only a real switch into drill asks straight away; re-arming on every
      # set_mode(drill) would loop.
      if mode is Mode.DRILL and self.tools.pending is None:
        self._auto_ask_on_mode_change = True
    self.state.mode = mode

  def _state_block(self, trigger: str, extra: Optional[Dict] = None) -> dict:
    pending = self.tools.pending
    unit = self.tools.unit
    global_facts = self.facts.global_facts
    block = {
        'trigger': trigger,
        'mode': self.state.mode.value,
        'floor': self.state.floor.value,
        'tier_max': self.cfg.tier_max,
        'last_bar': global_facts.get('num_measures'),
        'home_key': global_facts.get('key_inferred'),
        'pending_question': pending.public() if pending else None,
        'unit': ({'key': unit.get('key'), 'start_measure': unit.get('start_measure'),
                  'end_measure': unit.get('end_measure')} if unit else None),
        'weak_spots': self.session.weak_spot_prompts(3),
        'recent_evidence': self.evidence.cite(3),
        'greeted': self.greeted,
        'played_pitches': list(self.state.played_pitches),
    }
    if extra:
      block.update(extra)
    return block

  # -- speech -------------------------------------------------------------

  async def _speak(self, text: str) -> bool:
    self._tts_cancel = asyncio.Event()
    self.state.speaking = True
    self.state.floor = Floor.TUTOR
    completed = False
    try:
      self._tts_task = asyncio.create_task(self.tts.speak(text, self._tts_cancel))
      completed = await self._tts_task
    except asyncio.CancelledError:
      completed = False
    except Exception as exc:  # noqa: BLE001 - a voice outage must not end the lesson
      # The reply text still reaches the client, which can show it. Losing the
      # whole turn because synthesis failed would be worse than being silent.
      self.tts_error = str(exc)[:200]
      self.session.log({'event': 'tts_error', 'error': self.tts_error})
      print('  [tts unavailable: %s]' % self.tts_error)
      completed = False
    finally:
      self.state.speaking = False
      self.state.floor = Floor.NONE
      self._tts_task = None
    return bool(completed)

  def _cancel_tts(self) -> None:
    if self._tts_task and not self._tts_task.done():
      self._tts_cancel.set()

  # -- convenience --------------------------------------------------------

  async def greet(self) -> None:
    if self.greeted:
      return
    await self.respond('greeting')
    # Marked only after it succeeds: a transient failure (a TTS box that is
    # briefly away) used to consume the greeting permanently.
    self.greeted = True


def _json(value) -> str:
  import json
  return json.dumps(value, ensure_ascii=False)
