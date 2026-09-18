"""Client for a self-hosted OpenAI-compatible chat completions server.

Works with vLLM, llama.cpp's server, Ollama, LM Studio, TGI and friends -- all
of which expose /v1/chat/completions with tool calling. Keeping to that shape
means the tutor is model-agnostic and can be pointed at whichever model you
have the VRAM for.

Latency note: send a stable prefix (system prompt + tool schemas) so the server
can reuse its KV cache; a rolling conversation that keeps the prefix identical
is the difference between ~200 ms and ~2 s to first token.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import List, Optional

from .base import LlmReply, ToolCall


class OpenAiCompatLlm:
  def __init__(self, base_url: str, model: str, api_key: str = 'not-needed',
               timeout_s: float = 60.0, temperature: float = 0.6,
               max_tokens: int = 220):
    self.base_url = base_url.rstrip('/')
    self.model = model
    self.api_key = api_key
    self.timeout_s = timeout_s
    self.temperature = temperature
    self.max_tokens = max_tokens

  def _post(self, messages: List[dict], tools: List[dict]) -> dict:
    payload = {
        'model': self.model,
        'messages': messages,
        'temperature': self.temperature,
        'max_tokens': self.max_tokens,
    }
    if tools:
      payload['tools'] = tools
      payload['tool_choice'] = 'auto'
    req = urllib.request.Request(
        self.base_url + '/chat/completions',
        data=json.dumps(payload).encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer %s' % self.api_key})
    with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
      return json.loads(resp.read().decode('utf-8'))

  async def complete(self, messages: List[dict],
                     tools: Optional[List[dict]] = None) -> LlmReply:
    try:
      data = await asyncio.to_thread(self._post, messages, tools or [])
    except urllib.error.HTTPError as exc:
      detail = exc.read().decode('utf-8', 'replace')[:300]
      raise RuntimeError('LLM server returned %s: %s' % (exc.code, detail)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
      raise RuntimeError(
          'LLM server unreachable at %s (%s). Start it, or use TUTOR_LLM=stub.'
          % (self.base_url, exc)) from exc

    choice = (data.get('choices') or [{}])[0]
    message = choice.get('message') or {}
    calls = []
    for tc in message.get('tool_calls') or []:
      fn = tc.get('function') or {}
      raw_args = fn.get('arguments') or '{}'
      try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
      except ValueError:
        args = {}
      calls.append(ToolCall(id=tc.get('id') or 'call', name=fn.get('name') or '',
                            arguments=args))
    return LlmReply(text=(message.get('content') or '').strip(), tool_calls=calls)
