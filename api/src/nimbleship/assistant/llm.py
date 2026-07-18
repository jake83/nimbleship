"""The model seam for the AI assistant (ADR 0016): a narrow protocol over one
Claude turn, so the orchestrator loop is testable with a scripted fake and never
calls the real API in a test. The Anthropic SDK is imported only by the production
client, so the module loads without a key."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict[str, object]


@dataclass(frozen=True)
class LlmReply:
    """One model turn: its text, any tool calls it wants run, and why it stopped.
    stop_reason == 'tool_use' means the loop must run the tools and continue."""

    stop_reason: str
    text: str
    tool_uses: tuple[ToolUse, ...]


class LlmClient(Protocol):
    def reply(
        self,
        *,
        system: str,
        messages: list[dict[str, object]],
        tools: Sequence[dict[str, object]],
    ) -> LlmReply: ...


class AnthropicClient:
    """The production client: one messages.create call per turn, mapped to LlmReply.
    Read-only - it never acts, it only asks the model what to read next."""

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def reply(
        self,
        *,
        system: str,
        messages: list[dict[str, object]],
        tools: Sequence[dict[str, object]],
    ) -> LlmReply:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=system,
            messages=messages,  # type: ignore[arg-type]
            tools=list(tools),  # type: ignore[arg-type]
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        tool_uses = tuple(
            ToolUse(id=block.id, name=block.name, input=dict(block.input))
            for block in response.content
            if block.type == "tool_use"
        )
        return LlmReply(
            stop_reason=response.stop_reason or "end_turn",
            text=text,
            tool_uses=tool_uses,
        )


def build_client(api_key: str | None, model: str) -> LlmClient | None:
    """The production client, or None when no key is configured (ADR 0016's
    fail-closed default) - the caller reports 'not configured' rather than erroring."""
    if not api_key:
        return None
    return AnthropicClient(api_key, model)
