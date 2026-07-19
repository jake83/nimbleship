"""The Claude tool-use loop shared by the tool-using features (ADR 0016, 0017): let
the model call tools, feed back the structured results, return its final text. The
model seam and the tool runner are injected, so the loop is task-agnostic and
exercised with a scripted fake, never the real API."""

import json
from collections.abc import Callable, Sequence

from nimbleship.assistant.llm import LlmClient, LlmReply

Message = dict[str, object]
# (tool name, tool input) -> a JSON-serialisable result handed back to the model.
type ToolRunner = Callable[[str, dict[str, object]], dict[str, object]]


def run_tool_use_loop(
    conversation: Sequence[Message],
    *,
    system: str,
    tools: Sequence[dict[str, object]],
    run_tool: ToolRunner,
    llm: LlmClient,
    max_turns: int,
    exhausted: str,
) -> str:
    """Run the loop for one conversation and return the model's final text. Each turn
    the model may call tools; the loop runs them and feeds the results back until the
    model stops calling tools, or the turn budget (max_turns) is spent (`exhausted`)."""
    messages: list[Message] = list(conversation)
    for _ in range(max_turns):
        reply = llm.reply(system=system, messages=messages, tools=tools)
        if reply.stop_reason != "tool_use" or not reply.tool_uses:
            # A tool_use stop with no calls can't advance the loop (and an empty
            # assistant message is a 400 at the real API), so treat it as terminal.
            return reply.text
        messages.append({"role": "assistant", "content": _assistant_content(reply)})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": json.dumps(run_tool(use.name, use.input)),
                    }
                    for use in reply.tool_uses
                ],
            }
        )
    return exhausted


def _assistant_content(reply: LlmReply) -> list[dict[str, object]]:
    # The assistant turn must be echoed back verbatim to continue, any leading text
    # before the tool calls included.
    content: list[dict[str, object]] = []
    if reply.text:
        content.append({"type": "text", "text": reply.text})
    for use in reply.tool_uses:
        content.append(
            {"type": "tool_use", "id": use.id, "name": use.name, "input": use.input}
        )
    return content
