"""The carrier builder edge (ADR 0018): a conversation and the working definition in,
the builder's reply and the edited working copy out. The LLM client is a dependency so
it fails closed (503 when no key is configured) and a test can inject a scripted fake.
The builder never publishes - it hands the working copy back for the operator to commit
as a draft through the definition rails."""

from typing import Annotated, Literal

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError

from nimbleship.assistant import LlmClient, build_client
from nimbleship.carrier_builder import build
from nimbleship.config import get_settings
from nimbleship.domain.carrier_definition import CarrierDefinition

router = APIRouter(prefix="/carrier-builder", tags=["carrier-builder"])


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
def builder_messages(request: BuilderRequest, llm: LlmDep) -> BuilderReply:
    if llm is None:
        raise HTTPException(503, "the carrier builder is not configured")
    if not request.messages:
        raise HTTPException(422, "the conversation is empty")
    conversation: list[dict[str, object]] = [
        {"role": message.role, "content": message.content}
        for message in request.messages
    ]
    try:
        result = build(conversation, request.definition, llm=llm)
    except anthropic.APIError as error:
        raise HTTPException(502, "the carrier builder is unavailable") from error
    return BuilderReply(reply=result.reply, definition=result.definition)


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
