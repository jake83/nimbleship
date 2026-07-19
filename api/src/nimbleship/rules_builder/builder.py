"""The rules builder orchestrator (ADR 0017): the tool-use loop over the working-copy
edit tools. Given a conversation and the current working copy, it lets the model edit
the copy and dry-run it, then returns its reply and the resulting working copy. The
working copy is not saved here - it rides the request each turn (like the assistant's
conversation), and the operator commits it as a draft through the rulebook rails."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from nimbleship.assistant.llm import LlmClient
from nimbleship.assistant.loop import Message, run_tool_use_loop
from nimbleship.domain.allocation import ServiceDeclaration
from nimbleship.rules_builder.prompts import BUILDER_SYSTEM_PROMPT, EXHAUSTED_REPLY
from nimbleship.rules_builder.tools import (
    TOOL_SCHEMAS,
    WorkingCopy,
    run_builder_tool,
    working_copy_error,
)


class InvalidWorkingCopy(ValueError):
    """A client-supplied working copy that already violates a cross-service
    invariant. Raised before any edit so the builder never operates on - or hands
    back - a copy where an edit would behave wrongly (e.g. a remove hitting two
    same-coded services)."""


# A single builder turn may add several services and dry-run between them; this bounds
# a runaway loop while leaving room for a real multi-edit request.
MAX_TURNS = 16


@dataclass(frozen=True)
class BuildResult:
    """The builder's turn: its reply, and the working copy after its edits - which the
    surface shows and sends back on the next turn."""

    reply: str
    services: list[ServiceDeclaration]


def build(
    session: Session,
    conversation: Sequence[Message],
    services: Sequence[ServiceDeclaration],
    *,
    llm: LlmClient,
) -> BuildResult:
    """Run one builder turn against `services` (the working copy) and return the reply
    plus the edited copy. The copy is mutated in memory only; nothing is persisted.

    Rejects a client-supplied copy that already breaks a cross-service invariant: it
    rides the request each turn, so the server validates what it is handed rather
    than trusting the caller preserved a valid copy."""
    seed = list(services)
    clash = working_copy_error(seed)
    if clash is not None:
        raise InvalidWorkingCopy(clash)
    state = WorkingCopy(services=seed)
    reply = run_tool_use_loop(
        conversation,
        system=BUILDER_SYSTEM_PROMPT,
        tools=TOOL_SCHEMAS,
        run_tool=lambda name, tool_input: run_builder_tool(
            session, state, name, tool_input
        ),
        llm=llm,
        max_turns=MAX_TURNS,
        exhausted=EXHAUSTED_REPLY,
    )
    return BuildResult(reply=reply, services=state.services)
