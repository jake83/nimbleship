"""The assistant orchestrator (ADR 0016): the shared tool-use loop over the
read-only domain tools. Given a conversation, it lets the model call tools, feeds
back the structured results, and returns the grounded answer. The model seam
(LlmClient) is injected, so the loop is exercised end to end with a scripted fake
and never calls the real API in a test."""

from collections.abc import Sequence

from sqlalchemy.orm import Session

from nimbleship.assistant.llm import LlmClient
from nimbleship.assistant.loop import Message, run_tool_use_loop
from nimbleship.assistant.prompts import SYSTEM_PROMPT
from nimbleship.assistant.tools import TOOL_SCHEMAS, run_tool

# Bounds spend: a single-order diagnosis needs only a few tool reads.
MAX_TURNS = 8


def answer(session: Session, conversation: Sequence[Message], *, llm: LlmClient) -> str:
    """Run the tool-use loop for one conversation and return the model's grounded
    answer. `conversation` is the running message list (user turns and prior
    assistant turns); the order number lives in the operator's question, which the
    model passes to the tools."""
    return run_tool_use_loop(
        conversation,
        system=SYSTEM_PROMPT,
        tools=TOOL_SCHEMAS,
        run_tool=lambda name, tool_input: run_tool(session, name, tool_input),
        llm=llm,
        max_turns=MAX_TURNS,
        exhausted="I could not complete the diagnosis within the step budget.",
    )
