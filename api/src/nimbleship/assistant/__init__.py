"""The AI assistant (ADR 0016): an in-process, read-only tool-use loop that answers
single-order diagnostics ("why did order X ship with Y / fail to print / miss its
manifest") from NimbleShip's own structured domain reads."""

from nimbleship.assistant.assistant import answer
from nimbleship.assistant.llm import (
    AnthropicClient,
    LlmClient,
    LlmReply,
    ToolUse,
    build_client,
)

__all__ = [
    "AnthropicClient",
    "LlmClient",
    "LlmReply",
    "ToolUse",
    "answer",
    "build_client",
]
