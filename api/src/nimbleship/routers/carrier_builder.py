"""The carrier builder edge (ADR 0018): a conversation and the working definition in,
the builder's reply and the edited working copy out. The LLM client is a dependency so
it fails closed (503 when no key is configured) and a test can inject a scripted fake.
The builder never publishes - it hands the working copy back for the operator to commit
as a draft through the definition rails."""

from datetime import datetime
from typing import Annotated, Literal

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from nimbleship.assistant import LlmClient, build_client
from nimbleship.carrier_builder import build
from nimbleship.carrier_builder.handoff import (
    BlockerAlreadyResolved,
    UnknownBlocker,
    blockers_for,
    resolve_blocker,
)
from nimbleship.config import get_settings
from nimbleship.db import get_session
from nimbleship.domain.carrier_definition import CarrierDefinition
from nimbleship.models import HandoffBlocker

router = APIRouter(prefix="/carrier-builder", tags=["carrier-builder"])

SessionDep = Annotated[Session, Depends(get_session)]


def get_llm_client() -> LlmClient | None:
    settings = get_settings()
    return build_client(settings.anthropic_api_key, settings.anthropic_model)


LlmDep = Annotated[LlmClient | None, Depends(get_llm_client)]


class BuilderMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=10_000)


# A definition has a handful of top-level keys; this caps them as a light structural
# guard. It does not bound the nested payload size - overall request-body limiting is
# an app-level concern (there is no body-size middleware yet).
_DEFINITION_MAX_KEYS = 50


class BuilderRequest(BaseModel):
    messages: list[BuilderMessage] = Field(max_length=50)
    # The working definition so far, assembled key by key; empty on the first turn of a
    # new-carrier onboarding.
    definition: dict[str, object] = Field(
        default_factory=dict, max_length=_DEFINITION_MAX_KEYS
    )
    # The onboarding documentation (pasted text and text attachments), redacted of
    # known stored config values before it reaches the model. Bounded so a request
    # can't ask the model to ingest an unbounded blob.
    packet: str = Field(default="", max_length=200_000)


class BuilderReply(BaseModel):
    reply: str
    definition: dict[str, object]


class CheckRequest(BaseModel):
    definition: dict[str, object] = Field(
        default_factory=dict, max_length=_DEFINITION_MAX_KEYS
    )


class CheckOut(BaseModel):
    valid: bool
    # Human-readable validation problems; empty when valid.
    errors: list[str]


@router.get("/status")
def builder_status(llm: LlmDep) -> dict[str, bool]:
    """Whether the builder is configured, so a surface can disable its input instead of
    letting a submit dead-end at the 503."""
    return {"configured": llm is not None}


@router.post("/messages")
def builder_messages(
    request: BuilderRequest, session: SessionDep, llm: LlmDep
) -> BuilderReply:
    if llm is None:
        raise HTTPException(503, "the carrier builder is not configured")
    if not request.messages:
        raise HTTPException(422, "the conversation is empty")
    conversation: list[dict[str, object]] = [
        {"role": message.role, "content": message.content}
        for message in request.messages
    ]
    try:
        result = build(
            session, conversation, request.definition, request.packet, llm=llm
        )
    except anthropic.APIError as error:
        raise HTTPException(502, "the carrier builder is unavailable") from error
    return BuilderReply(reply=result.reply, definition=result.definition)


class BlockerOut(BaseModel):
    id: int
    carrier: str
    kind: str
    title: str
    detail: str
    plugin_name: str | None
    status: str
    resolution: str | None
    created_at: datetime
    resolved_at: datetime | None


def _blocker_out(blocker: HandoffBlocker) -> BlockerOut:
    return BlockerOut(
        id=blocker.id,
        carrier=blocker.carrier,
        kind=blocker.kind,
        title=blocker.title,
        detail=blocker.detail,
        plugin_name=blocker.plugin_name,
        status=blocker.status,
        resolution=blocker.resolution,
        created_at=blocker.created_at,
        resolved_at=blocker.resolved_at,
    )


class ResolveIn(BaseModel):
    # The engineer's answer: a decision, or "shipped in vX" for a plugin.
    resolution: str = Field(min_length=1, max_length=10_000)


@router.get("/blockers")
def list_blockers(carrier: str, session: SessionDep) -> list[BlockerOut]:
    """A carrier's Handoff blockers, open and resolved - the engineer's queue and the
    onboarding's audit trail (ADR 0018). No model involved; needs no API key."""
    return [_blocker_out(blocker) for blocker in blockers_for(session, carrier)]


@router.post("/blockers/{blocker_id}/resolve")
def resolve(blocker_id: int, payload: ResolveIn, session: SessionDep) -> BlockerOut:
    """Record the engineer's answer and close the blocker. The next builder turn for
    the carrier consumes it (the model reads resolutions via its list_blockers tool)."""
    try:
        blocker = resolve_blocker(session, blocker_id, payload.resolution)
    except UnknownBlocker as error:
        raise HTTPException(404, str(error)) from error
    except BlockerAlreadyResolved as error:
        raise HTTPException(409, str(error)) from error
    return _blocker_out(blocker)


@router.post("/check")
def builder_check(request: CheckRequest) -> CheckOut:
    """Validate the working definition as a whole CarrierDefinition and report what
    remains - the capability board's completeness signal. Pure validation, no model, so
    it needs no API key. An incomplete mid-build copy is a normal 200 with errors, not
    a 422: incompleteness is the expected state this endpoint exists to describe."""
    try:
        CarrierDefinition.model_validate(request.definition)
    except ValidationError as error:
        problems = [
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        ]
        return CheckOut(valid=False, errors=problems)
    return CheckOut(valid=True, errors=[])
