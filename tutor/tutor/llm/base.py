"""The LLM client interface.

Messages are plain dicts in OpenAI chat shape so both the scripted stub and a
self-hosted server are interchangeable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Protocol


@dataclass
class ToolCall:
  id: str
  name: str
  arguments: Dict[str, object] = field(default_factory=dict)


@dataclass
class LlmReply:
  text: str = ''
  tool_calls: List[ToolCall] = field(default_factory=list)


class LlmClient(Protocol):
  async def complete(self, messages: List[dict],
                     tools: List[dict]) -> LlmReply:
    """One model turn: either text, or tool calls to run, or both."""
    ...
