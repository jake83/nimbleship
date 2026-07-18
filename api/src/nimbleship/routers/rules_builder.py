"""The rules builder edge (ADR 0017): a conversation plus a working copy in, the
builder's reply and the edited working copy out. The LLM client is a dependency so it
fails closed (503 when no key is configured) and a test can inject a scripted fake.
The builder never publishes - it hands the working copy back for the operator to
commit as a draft through the rulebook rails."""

from typing import Annotated, Literal

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from nimbleship.assistant import LlmClient, build_client
from nimbleship.config import get_settings
from nimbleship.db import get_session
from nimbleship.domain.allocation import ServiceDeclaration
from nimbleship.domain.rulebook import active_rulebook
from nimbleship.rules_builder import InvalidWorkingCopy, build

router = APIRouter(prefix="/rulebook/builder", tags=["rulebook"])

SessionDep = Annotated[Session, Depends(get_session)]


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
    services: list[ServiceDeclaration] | None = None


class BuilderReply(BaseModel):
    reply: str
    services: list[ServiceDeclaration]


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
