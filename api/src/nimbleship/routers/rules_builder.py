"""The rules builder edge (ADR 0017): a conversation plus a working copy in, the
builder's reply and the edited working copy out. The LLM client is a dependency so it
fails closed (503 when no key is configured) and a test can inject a scripted fake.
The builder never publishes - it hands the working copy back for the operator to
commit as a draft through the rulebook rails."""

from typing import Annotated, Literal

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from nimbleship.assistant import LlmClient, build_client
from nimbleship.config import get_settings
from nimbleship.db import get_session
from nimbleship.domain.allocation import Rulebook, ServiceDeclaration
from nimbleship.domain.dry_run import DEFAULT_LIMIT, dry_run_rulebook
from nimbleship.domain.rulebook import active_rulebook
from nimbleship.routers.rulebook import DryRunResultOut
from nimbleship.rules_builder import InvalidWorkingCopy, build

router = APIRouter(prefix="/rulebook/builder", tags=["rulebook"])

SessionDep = Annotated[Session, Depends(get_session)]

# Caps the working copy that rides every turn; a real rulebook is dozens of
# services, far under this. Matches the order_numbers replay cap.
SERVICES_MAX = 500


def get_llm_client() -> LlmClient | None:
    settings = get_settings()
    return build_client(settings.anthropic_api_key, settings.anthropic_model)


LlmDep = Annotated[LlmClient | None, Depends(get_llm_client)]


class BuilderMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=10_000)


class BuilderRequest(BaseModel):
    messages: list[BuilderMessage] = Field(max_length=50)
    # The working copy so far. Omitted on the first turn: the builder seeds from the
    # live rulebook so edits start from what is shipping today.
    services: list[ServiceDeclaration] | None = Field(
        default=None, max_length=SERVICES_MAX
    )


class BuilderReply(BaseModel):
    reply: str
    services: list[ServiceDeclaration]


class BuilderDryRunRequest(BaseModel):
    services: list[ServiceDeclaration] = Field(max_length=SERVICES_MAX)
    order_numbers: list[str] | None = Field(default=None, max_length=500)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=500)


class BuilderDryRunOut(BaseModel):
    # No rulebook_version: the working copy is unsaved, so there is no version yet.
    total: int
    changed: int
    results: list[DryRunResultOut]


@router.get("/status")
def builder_status(llm: LlmDep) -> dict[str, bool]:
    """Whether the builder is configured, so a surface can disable its input instead
    of letting a submit dead-end at the 503."""
    return {"configured": llm is not None}


@router.post("/messages")
def builder_messages(
    request: BuilderRequest, session: SessionDep, llm: LlmDep
) -> BuilderReply:
    if llm is None:
        raise HTTPException(503, "the rules builder is not configured")
    if not request.messages:
        raise HTTPException(422, "the conversation is empty")
    services = (
        request.services
        if request.services is not None
        else active_rulebook(session).services
    )
    conversation: list[dict[str, object]] = [
        {"role": message.role, "content": message.content}
        for message in request.messages
    ]
    try:
        result = build(session, conversation, services, llm=llm)
    except InvalidWorkingCopy as error:
        # A client-supplied working copy that already breaks an invariant: a bad
        # request, not a builder outage - reject it before the model runs.
        raise HTTPException(422, str(error)) from error
    except anthropic.APIError as error:
        raise HTTPException(502, "the rules builder is unavailable") from error
    return BuilderReply(reply=result.reply, services=result.services)


@router.post("/dry-run")
def builder_dry_run(
    request: BuilderDryRunRequest, session: SessionDep
) -> BuilderDryRunOut:
    """Replay the working copy over historical orders and report what would reroute,
    so the operator previews impact before saving a draft (ADR 0017). Pure allocation,
    no model - so it needs no API key. An invalid working copy (duplicate code or
    tie-break, or empty) is a bad request, not a server error."""
    try:
        rulebook = Rulebook(version=0, services=request.services)
    except ValidationError as error:
        raise HTTPException(422, str(error)) from error
    report = dry_run_rulebook(session, rulebook, request.order_numbers, request.limit)
    return BuilderDryRunOut(
        total=report.total,
        changed=report.changed,
        results=[
            DryRunResultOut(
                order_number=result.order_number,
                current_service=result.current_service,
                draft_service=result.draft_service,
                changed=result.changed,
            )
            for result in report.results
        ],
    )
