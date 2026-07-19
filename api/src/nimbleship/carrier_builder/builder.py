"""The carrier builder orchestrator (ADR 0018): the tool-use loop over the
working-definition edit tools. Given a conversation and the working copy so far, it
lets the model edit the copy and returns its reply plus the resulting copy. The working
copy is not saved here - it rides each request (like the rules builder's), and the
operator commits it as a draft through the definition rails."""

from collections.abc import Sequence
from dataclasses import dataclass

from nimbleship.assistant.llm import LlmClient
from nimbleship.assistant.loop import Message, run_tool_use_loop
from nimbleship.carrier_builder.prompts import BUILDER_SYSTEM_PROMPT, EXHAUSTED_REPLY
from nimbleship.carrier_builder.tools import (
    TOOL_SCHEMAS,
    WorkingDefinition,
    run_carrier_builder_tool,
)

# A definition is built over many turns (several operations, each with steps); this
# bounds a runaway loop while leaving room for a real multi-edit turn.
MAX_TURNS = 24


@dataclass(frozen=True)
class BuildResult:
    """The builder's turn: its reply, and the working definition after its edits -
    which the surface shows and sends back on the next turn."""

    reply: str
    definition: dict[str, object]


def build(
    conversation: Sequence[Message],
    definition: dict[str, object],
    *,
    llm: LlmClient,
) -> BuildResult:
    """Run one builder turn against the working `definition` and return the reply plus
    the edited copy. The copy is mutated in memory only; nothing is persisted.

    Bulk document ingestion (the onboarding packet) is deferred to a follow-up: it must
    route credentials to Carrier Config and keep them out of the model (ADR 0018), so a
    raw doc dump can't reach the prompt until that separation exists."""
    state = WorkingDefinition(data=dict(definition))
    reply = run_tool_use_loop(
        conversation,
        system=BUILDER_SYSTEM_PROMPT,
        tools=TOOL_SCHEMAS,
        run_tool=lambda name, tool_input: run_carrier_builder_tool(
            state, name, tool_input
        ),
        llm=llm,
        max_turns=MAX_TURNS,
        exhausted=EXHAUSTED_REPLY,
    )
    return BuildResult(reply=reply, definition=state.data)
