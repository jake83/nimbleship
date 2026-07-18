"""The assistant route (ADR 0016): fails closed without a key, and runs the tool-use
loop over a scripted fake injected through the dependency - never the real API."""

from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nimbleship.assistant import LlmReply, ToolUse
from nimbleship.routers.assistant import get_llm_client

_ORDER = "95000254580"
_CONSIGNMENT = {
    "order_number": _ORDER,
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}, {"weight_kg": "3.1"}],
}

Message = dict[str, object]


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
    # No key is set in the test environment, so the assistant is not configured.
    assert client.get("/api/assistant/status").json() == {"configured": False}


def test_status_reports_configured_when_a_client_is_present(
    app: FastAPI, client: TestClient
) -> None:
    _use(app, [])
    assert client.get("/api/assistant/status").json() == {"configured": True}


def test_messages_503_when_not_configured(client: TestClient) -> None:
    response = client.post(
        "/api/assistant/messages",
        json={"messages": [{"role": "user", "content": "why did it ship?"}]},
    )
    assert response.status_code == 503


def test_messages_422_on_an_empty_conversation(
    app: FastAPI, client: TestClient
) -> None:
    _use(app, [])
    response = client.post("/api/assistant/messages", json={"messages": []})
    assert response.status_code == 422


def test_messages_returns_a_grounded_answer(app: FastAPI, client: TestClient) -> None:
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "DROPOUT-STD",
                "carrier": "dropout",
                "name": "Drop Out Standard",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 1,
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    client.post(f"/api/rulebook/versions/{version}/publish")
    client.post("/api/consignments", json=_CONSIGNMENT)
    _use(
        app,
        [
            LlmReply(
                stop_reason="tool_use",
                text="",
                tool_uses=(
                    ToolUse("t1", "allocation_trace", {"order_number": _ORDER}),
                ),
            ),
            LlmReply(
                stop_reason="end_turn", text="It shipped with dropout.", tool_uses=()
            ),
        ],
    )

    response = client.post(
        "/api/assistant/messages",
        json={"messages": [{"role": "user", "content": f"why did {_ORDER} ship?"}]},
    )

    assert response.status_code == 200
    assert "dropout" in response.json()["reply"]
