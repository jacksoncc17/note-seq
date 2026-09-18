"""LLM seam: clients that may call tools, and the prompt that binds them."""

from .base import LlmClient, LlmReply, ToolCall
from .prompts import build_messages, state_block
from .stub import ScriptedTutor

__all__ = ['LlmClient', 'LlmReply', 'ToolCall', 'ScriptedTutor',
           'build_messages', 'state_block']
