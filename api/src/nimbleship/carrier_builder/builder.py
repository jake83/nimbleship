"""The carrier builder orchestrator (ADR 0018): the tool-use loop over the
working-definition edit tools. Given a conversation and the working copy so far, it
lets the model edit the copy and returns its reply plus the resulting copy. The working
copy is not saved here - it rides each request (like the rules builder's), and the
operator commits it as a draft through the definition rails."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from nimbleship.assistant.llm import LlmClient
from nimbleship.assistant.loop import Message, run_tool_use_loop
from nimbleship.carrier_builder.prompts import BUILDER_SYSTEM_PROMPT, EXHAUSTED_REPLY
from nimbleship.carrier_builder.redaction import redact_packet
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
    session: Session,
    conversation: Sequence[Message],
    definition: dict[str, object],
    packet: str = "",
    *,
    llm: LlmClient,
) -> BuildResult:
    """Run one builder turn against the working `definition`, grounded in `packet`
    (the onboarding documentation), and return the reply plus the edited copy. The
    copy is mutated in memory only; the one durable side effect a turn may have is
    raising a Handoff blocker via `session`, which must outlive the conversation for
    the engineer to resolve.

    The packet is redacted before it reaches the prompt: every known stored config
    value is replaced with its config.* path (ADR 0018 - secrets never reach the
    model). This is the single point where packet text enters the prompt, so every
    ingestion mode inherits the scrub."""
    state = WorkingDefinition(data=dict(definition))
    system = BUILDER_SYSTEM_PROMPT
    if packet.strip():
        redacted = redact_packet(session, packet)
        system += (
            "\n\nCarrier documentation provided by the operator (credentials the"
            " operator stored appear as their [use config.*] reference, never the"
            " value):\n" + redacted
        )
    reply = run_tool_use_loop(
        conversation,
        system=system,
        tools=TOOL_SCHEMAS,
        run_tool=lambda name, tool_input: run_carrier_builder_tool(
            session, state, name, tool_input
        ),
        llm=llm,
        max_turns=MAX_TURNS,
        exhausted=EXHAUSTED_REPLY,
    )
    return BuildResult(reply=reply, definition=state.data)
