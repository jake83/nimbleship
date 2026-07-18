"""The AI assistant edge (ADR 0016): a conversation in, a grounded answer out. The
LLM client is a dependency so it fails closed (503 when no key is configured) and
so a test can inject a scripted fake. Read-only - the assistant only queries."""

from typing import Annotated, Literal

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from nimbleship.assistant import LlmClient, answer, build_client
from nimbleship.config import get_settings
from nimbleship.db import get_session

router = APIRouter(prefix="/assistant", tags=["assistant"])

SessionDep = Annotated[Session, Depends(get_session)]


def get_llm_client() -> LlmClient | None:
    settings = get_settings()
    return build_client(settings.anthropic_api_key, settings.anthropic_model)


LlmDep = Annotated[LlmClient | None, Depends(get_llm_client)]


class AssistantMessage(BaseModel):
    # Only the two conversation roles - a stray role (e.g. "system") is a malformed
    # request the boundary rejects (422), not a vendor outage misreported as 502.
    role: Literal["user", "assistant"]
    content: str = Field(max_length=10_000)


class AssistantRequest(BaseModel):
    messages: list[AssistantMessage] = Field(max_length=50)


class AssistantReply(BaseModel):
    reply: str


@router.get("/status")
def assistant_status(llm: LlmDep) -> dict[str, bool]:
    """Whether the assistant is configured, so a surface can disable its input
    instead of letting a submit dead-end at the 503."""
    return {"configured": llm is not None}


@router.post("/messages")
def assistant_messages(
    request: AssistantRequest, session: SessionDep, llm: LlmDep
) -> AssistantReply:
    if llm is None:
        raise HTTPException(503, "the assistant is not configured")
    if not request.messages:
        raise HTTPException(422, "the conversation is empty")
    conversation: list[dict[str, object]] = [
        {"role": message.role, "content": message.content}
        for message in request.messages
    ]
    try:
        reply = answer(session, conversation, llm=llm)
    except anthropic.APIError as error:
        # A model/transport failure is the assistant being unavailable, not a bug in
        # the request - surface it as such rather than a 500.
        raise HTTPException(502, "the assistant is unavailable") from error
    return AssistantReply(reply=reply)
