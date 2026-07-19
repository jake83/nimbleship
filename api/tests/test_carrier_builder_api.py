"""The carrier builder route (ADR 0018): fails closed without a key, runs the edit loop
over a scripted fake injected through the dependency, and returns the edited working
definition. The onboarding packet rides the request as grounding for the model."""

from collections.abc import Sequence

import anthropic
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nimbleship.assistant import LlmReply, ToolUse
from nimbleship.routers.carrier_builder import get_llm_client

Message = dict[str, object]

_OPERATION: dict[str, object] = {
    "steps": [
        {
            "name": "book",
            "transport": "http",
            "request": {
                "method": "POST",
                "url": "config.url",
                "content_type": "json",
                "mapping": [{"target": "order", "source": "shipment.order_number"}],
            },
        }
    ]
}


class _FakeLlm:
    def __init__(self, replies: list[LlmReply]) -> None:
        self._replies = replies

    def reply(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[dict[str, object]],
    ) -> LlmReply:
        return self._replies.pop(0)


def _use(app: FastAPI, replies: list[LlmReply]) -> None:
    app.dependency_overrides[get_llm_client] = lambda: _FakeLlm(replies)


def test_status_reports_not_configured_without_a_key(client: TestClient) -> None:
    assert client.get("/api/carrier-builder/status").json() == {"configured": False}


def test_messages_503_when_not_configured(client: TestClient) -> None:
    response = client.post(
        "/api/carrier-builder/messages",
        json={"messages": [{"role": "user", "content": "onboard acme"}]},
    )
    assert response.status_code == 503


def test_messages_422_on_an_empty_conversation(
    app: FastAPI, client: TestClient
) -> None:
    _use(app, [])
    response = client.post("/api/carrier-builder/messages", json={"messages": []})
    assert response.status_code == 422


def test_messages_edits_the_working_definition_and_returns_it(
    app: FastAPI, client: TestClient
) -> None:
    _use(
        app,
        [
            LlmReply(
                stop_reason="tool_use",
                text="",
                tool_uses=(
                    ToolUse("t1", "set_identity", {"carrier": "acme", "name": "Acme"}),
                    ToolUse(
                        "t2", "put_operation", {"name": "book", "operation": _OPERATION}
                    ),
                ),
            ),
            LlmReply(stop_reason="end_turn", text="Drafted it.", tool_uses=()),
        ],
    )
    response = client.post(
        "/api/carrier-builder/messages",
        json={"messages": [{"role": "user", "content": "onboard acme"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "Drafted it."
    assert body["definition"]["carrier"] == "acme"
    assert "book" in body["definition"]["operations"]


class _FailingLlm:
    def reply(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[dict[str, object]],
    ) -> LlmReply:
        raise anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com")
        )


def test_messages_502_when_the_model_is_unavailable(
    app: FastAPI, client: TestClient
) -> None:
    app.dependency_overrides[get_llm_client] = lambda: _FailingLlm()
    response = client.post(
        "/api/carrier-builder/messages",
        json={"messages": [{"role": "user", "content": "onboard acme"}]},
    )
    assert response.status_code == 502
