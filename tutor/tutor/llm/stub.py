"""A scripted tutor that behaves like the real one, without a model.

It is not a mock: it makes the *same* tool calls a real model would (draw a
question, grade an answer, compare playing), so the conductor's tool loop,
grounding verifier and session log are all exercised end to end. It also serves
as an executable statement of the tutoring policy in tutor/llm/prompts.py.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from .base import LlmReply, ToolCall

STATE_MARKER = 'STATE (machine-readable, do not read aloud):'
_BAR_RE = re.compile(r'\b(?:bar|bars|m|measure|measures)\s*(\d{1,3})', re.I)
_PLAIN_NUM_RE = re.compile(r'\b(\d{1,3})\b')


def extract_state(messages: List[dict]) -> Dict[str, object]:
  for msg in messages:
    if msg.get('role') != 'system':
      continue
    content = msg.get('content') or ''
    if STATE_MARKER in content:
      tail = content.split(STATE_MARKER, 1)[1].strip()
      try:
        return json.loads(tail.splitlines()[0])
      except (ValueError, IndexError):
        return {}
  return {}


def _last_of(messages: List[dict], role: str) -> Optional[dict]:
  for msg in reversed(messages):
    if msg.get('role') == role:
      return msg
  return None


def _first_sentence(text: str) -> str:
  """Everything up to the first full stop -- for keeping speech brief."""
  stripped = (text or '').strip()
  for idx, char in enumerate(stripped):
    if char in '.!?':
      return stripped[:idx + 1]
  return stripped


class ScriptedTutor:
  def __init__(self) -> None:
    self._counter = 0

  def _call(self, name: str, args: dict) -> ToolCall:
    self._counter += 1
    return ToolCall(id='call_%d' % self._counter, name=name, arguments=args)

  async def complete(self, messages: List[dict], tools: List[dict]) -> LlmReply:
    state = extract_state(messages)
    last = messages[-1] if messages else {}
    if last.get('role') == 'tool':
      # The tool NAME matters: session_status also carries a `correct` field,
      # and keying off that alone made a progress question come back as
      # "Not quite. It is ." during a live session.
      return LlmReply(text=self._speak(last.get('content', ''),
                                       last.get('name', '')))
    user = _last_of(messages, 'user') or {}
    text = (user.get('content') or '').strip()
    return self._decide(state, text)

  # -- policy -------------------------------------------------------------

  def _decide(self, state: Dict[str, object], text: str) -> LlmReply:
    lowered = text.lower()
    trigger = str(state.get('trigger') or '')
    mode = str(state.get('mode') or 'converse')
    pending = state.get('pending_question')

    # Trigger-driven turns come FIRST. Looking at the previous utterance here
    # would make "drill me" match its own keyword again on the follow-up turn
    # and loop on mode switching instead of asking the question.
    if trigger == 'greeting':
      if state.get('recent_evidence'):
        return LlmReply(text='Ready when you are.')
      return LlmReply(tool_calls=[self._call('get_piece_map', {})])

    if trigger == 'playing_feedback':
      # The conductor already compared the playing deterministically; the
      # tutor's job is to say it plainly, not to re-derive it.
      return LlmReply(text=str(state.get('last_evidence') or 'I heard that.'))

    if trigger == 'ask':
      return LlmReply(tool_calls=[self._call(
          'next_question', {'tier': int(state.get('tier_max', 1))})])

    if pending is not None and text:
      return LlmReply(tool_calls=[self._call('grade_answer', {'answer': text})])

    # Utterance-driven turns: commands first, then questions about the score.
    if any(w in lowered for w in ('drill', 'quiz me', 'test me', 'questions')):
      return LlmReply(tool_calls=[self._call('set_mode', {'mode': 'drill'})])
    if 'listen' in lowered:
      return LlmReply(tool_calls=[self._call('set_mode', {'mode': 'listen_coach'})])
    if any(w in lowered for w in ('stop', 'enough', 'just talk', 'done',
                                  'that is all', "that's all")):
      return LlmReply(tool_calls=[self._call('set_mode', {'mode': 'converse'})])
    if lowered.strip(' .!?') in ('hello', 'hi', 'hey', 'hello there',
                                 'good morning', 'good evening'):
      return LlmReply(text='Hello. Ask me about a bar, a key area or a repeat, '
                           'or say drill me.')

    # An explicit question outranks the mode's default. Asking "what is the
    # structure?" while drilling should be answered, not treated as a request
    # for the next question -- a real tutor would answer it.
    if any(w in lowered for w in ('end of', 'ending', 'coda', 'last bar',
                                  'finish', 'final bar')):
      last = int(state.get('last_bar') or 1)
      return LlmReply(tool_calls=[self._call('get_bar', {'bar': last})])
    if any(w in lowered for w in ('repeat', 'comes back', 'return', 'form',
                                  'map', 'structure', 'key')):
      return LlmReply(tool_calls=[self._call('get_piece_map', {})])
    if any(w in lowered for w in ('how am i', 'progress', 'accuracy', 'doing')):
      return LlmReply(tool_calls=[self._call('session_status', {})])
    bar = _BAR_RE.search(text)
    if bar:
      return LlmReply(tool_calls=[self._call('get_bar', {'bar': int(bar.group(1))})])

    # Nothing specific was asked: in drill mode that means "next question".
    if mode == 'drill':
      return LlmReply(tool_calls=[self._call(
          'next_question', {'tier': int(state.get('tier_max', 1))})])

    num = _PLAIN_NUM_RE.search(text)
    if num:
      return LlmReply(tool_calls=[self._call('get_bar', {'bar': int(num.group(1))})])
    # Honest fallback: no tool, no invented facts.
    return LlmReply(text=('I can only tell you what is in the score. Ask me about a '
                          'bar number, a key area, or a repeat, or say drill me.'))

  # -- speech shaping -----------------------------------------------------

  def _speak(self, tool_content: str, tool_name: str = '') -> str:
    try:
      payload = json.loads(tool_content)
    except ValueError:
      return 'I lost that lookup.'
    summary = payload.get('summary', '')
    data = payload.get('data') or {}

    if tool_name == 'grade_answer' or (tool_name == '' and
                                       'question_id' in data):
      expected = data.get('expected', '')
      head = 'Yes, that is right.' if data.get('correct') else 'Not quite.'
      reason = summary.split('. ', 1)[1] if '. ' in summary else ''
      if not str(expected).strip():
        return ('%s I could not tell what you answered, so let us try another.'
                % head)
      return ('%s It is %s. %s' % (head, expected, reason)).strip()
    if tool_name == 'session_status':
      return summary or 'No attempts yet.'
    if tool_name == 'next_question' or 'prompt' in data:
      options = data.get('options') or []
      if len(options) >= 2:
        # Phrased as a question, like a person would: candidate answers are not
        # assertions, and the grounding verifier treats them accordingly.
        listed = ', '.join(options[:-1]) + ' or ' + options[-1]
        return '%s Is it %s?' % (data.get('prompt', ''), listed)
      return data.get('prompt', summary)
    if data.get('spine'):
      # Spoken replies must be SHORT. Reading the whole map aloud produced a
      # 38.7-second greeting in a live session, during which the student could
      # not get a word in (half-duplex drops anything they say meanwhile).
      return '%s Say drill me when you want questions.' % _first_sentence(
          data['spine'])
    if data.get('path'):
      return 'I saved a practice file for that passage.'
    if 'mode' in data and tool_name == 'set_mode':
      return 'Okay, %s.' % str(data['mode']).replace('_', ' ')
    if summary:
      return summary
    return 'Done.'
