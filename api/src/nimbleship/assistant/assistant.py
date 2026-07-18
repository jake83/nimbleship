"""The assistant orchestrator (ADR 0016): a Claude tool-use loop over the read-only
domain tools. Given a conversation, it lets the model call tools, feeds back the
structured results, and returns the grounded answer. The model seam (LlmClient) is
injected, so the loop is exercised end to end with a scripted fake and never calls
the real API in a test."""

import json
from collections.abc import Sequence

from sqlalchemy.orm import Session

from nimbleship.assistant.llm import LlmClient, LlmReply
from nimbleship.assistant.prompts import SYSTEM_PROMPT
from nimbleship.assistant.tools import TOOL_SCHEMAS, run_tool

# Bounds spend: a single-order diagnosis needs only a few tool reads.
MAX_TURNS = 8

Message = dict[str, object]


def answer(session: Session, conversation: Sequence[Message], *, llm: LlmClient) -> str:
    """Run the tool-use loop for one conversation and return the model's grounded
    answer. `conversation` is the running message list (user turns and prior
    assistant turns); the order number lives in the operator's question, which the
    model passes to the tools."""
    messages: list[Message] = list(conversation)
    for _ in range(MAX_TURNS):
        reply = llm.reply(system=SYSTEM_PROMPT, messages=messages, tools=TOOL_SCHEMAS)
        if reply.stop_reason != "tool_use" or not reply.tool_uses:
            # A tool_use stop with no calls can't advance the loop (and an empty
            # assistant message is a 400 at the real API), so treat it as terminal.
            return reply.text
        messages.append({"role": "assistant", "content": _assistant_content(reply)})
        messages.append({"role": "user", "content": _tool_results(session, reply)})
    return "I could not complete the diagnosis within the step budget."


def _assistant_content(reply: LlmReply) -> list[dict[str, object]]:
    # The assistant turn must be echoed back verbatim for the model to continue,
    # any leading text before the tool calls included.
    content: list[dict[str, object]] = []
    if reply.text:
        content.append({"type": "text", "text": reply.text})
    for use in reply.tool_uses:
        content.append(
            {"type": "tool_use", "id": use.id, "name": use.name, "input": use.input}
        )
    return content


def _tool_results(session: Session, reply: LlmReply) -> list[dict[str, object]]:
    return [
        {
            "type": "tool_result",
            "tool_use_id": use.id,
            "content": json.dumps(run_tool(session, use.name, use.input)),
        }
        for use in reply.tool_uses
    ]
