"""The rules builder route (ADR 0017): fails closed without a key, runs the edit loop
over a scripted fake injected through the dependency, and returns the edited working
copy. On the first turn (no working copy sent) it seeds from the live rulebook."""

from collections.abc import Sequence

import anthropic
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nimbleship.assistant import LlmReply, ToolUse
from nimbleship.routers.rules_builder import get_llm_client

Message = dict[str, object]

_DROPOUT = {
    "code": "DROPOUT-STD",
    "carrier": "dropout",
    "name": "Drop Out Standard",
    "weight_min_kg": "0",
    "weight_max_kg": "999",
    "countries": ["GB"],
    "cost": "4.50",
    "tie_break_order": 1,
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
    assert client.get("/api/rulebook/builder/status").json() == {"configured": False}


def test_messages_503_when_not_configured(client: TestClient) -> None:
    response = client.post(
        "/api/rulebook/builder/messages",
        json={"messages": [{"role": "user", "content": "add a service"}]},
    )
    assert response.status_code == 503


def test_messages_422_on_an_empty_conversation(
    app: FastAPI, client: TestClient
) -> None:
    _use(app, [])
    response = client.post("/api/rulebook/builder/messages", json={"messages": []})
    assert response.status_code == 422


def test_messages_edits_the_sent_working_copy_and_returns_it(
    app: FastAPI, client: TestClient
) -> None:
    _use(
        app,
        [
            LlmReply(
                stop_reason="tool_use",
                text="",
                tool_uses=(ToolUse("t1", "remove_service", {"code": "DROPOUT-STD"}),),
            ),
            LlmReply(stop_reason="end_turn", text="Removed it.", tool_uses=()),
        ],
    )
    response = client.post(
        "/api/rulebook/builder/messages",
        json={
            "messages": [{"role": "user", "content": "drop dropout"}],
            "services": [_DROPOUT],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "Removed it."
    assert body["services"] == []


def test_messages_rejects_a_client_seed_that_breaks_an_invariant(
    app: FastAPI, client: TestClient
) -> None:
    # The working copy is client-supplied each turn; two same-coded services in the
    # seed are a bad request (422), rejected before the model runs - not silently
    # operated on (a later remove would delete both).
    _use(app, [LlmReply(stop_reason="end_turn", text="unused", tool_uses=())])
    response = client.post(
        "/api/rulebook/builder/messages",
        json={
            "messages": [{"role": "user", "content": "go"}],
            "services": [_DROPOUT, {**_DROPOUT, "tie_break_order": 2}],
        },
    )
    assert response.status_code == 422
    assert "duplicate service code" in response.text


def test_messages_allows_an_empty_working_copy(
    app: FastAPI, client: TestClient
) -> None:
    # Editing down to zero services is a legal mid-session state (a save is what
    # min_length guards, not the working copy), so an empty seed is accepted.
    _use(
        app,
        [
            LlmReply(
                stop_reason="tool_use",
                text="",
                tool_uses=(ToolUse("t1", "add_service", {"service": _DROPOUT}),),
            ),
            LlmReply(stop_reason="end_turn", text="Added.", tool_uses=()),
        ],
    )
    response = client.post(
        "/api/rulebook/builder/messages",
        json={"messages": [{"role": "user", "content": "add one"}], "services": []},
    )
    assert response.status_code == 200
    assert [s["code"] for s in response.json()["services"]] == ["DROPOUT-STD"]


def test_messages_seeds_from_the_live_rulebook_when_no_copy_is_sent(
    app: FastAPI, client: TestClient
) -> None:
    # Publish a rulebook; a first turn with no working copy starts from it, so the
    # model sees today's services and the returned copy carries them forward.
    version = client.post(
        "/api/rulebook/drafts", json={"author": "j", "services": [_DROPOUT]}
    ).json()["version"]
    client.post(f"/api/rulebook/versions/{version}/publish")
    _use(app, [LlmReply(stop_reason="end_turn", text="What next?", tool_uses=())])

    response = client.post(
        "/api/rulebook/builder/messages",
        json={"messages": [{"role": "user", "content": "what do we have?"}]},
    )
    assert response.status_code == 200
    assert [s["code"] for s in response.json()["services"]] == ["DROPOUT-STD"]


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
        "/api/rulebook/builder/messages",
        json={"messages": [{"role": "user", "content": "add a service"}]},
    )
    assert response.status_code == 502


def test_messages_rejects_a_working_copy_over_the_cap(
    app: FastAPI, client: TestClient
) -> None:
    _use(app, [])
    too_many = [{**_DROPOUT, "code": f"S{i}", "tie_break_order": i} for i in range(501)]
    response = client.post(
        "/api/rulebook/builder/messages",
        json={"messages": [{"role": "user", "content": "x"}], "services": too_many},
    )
    assert response.status_code == 422


_GB_ORDER = {
    "order_number": "95000254580",
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}],
}


def test_dry_run_reports_working_copy_impact(client: TestClient) -> None:
    # Publish a rulebook and ship an order under it, then dry-run a working copy that
    # reroutes that order to a new cheaper service - the report names the change. No
    # API key needed: dry-run is pure allocation, not the model.
    version = client.post(
        "/api/rulebook/drafts", json={"author": "j", "services": [_DROPOUT]}
    ).json()["version"]
    client.post(f"/api/rulebook/versions/{version}/publish")
    assert client.post("/api/consignments", json=_GB_ORDER).status_code == 201

    cheaper = {**_DROPOUT, "code": "CHEAP", "cost": "1.00", "tie_break_order": 1}
    original = {**_DROPOUT, "tie_break_order": 2}
    response = client.post(
        "/api/rulebook/builder/dry-run", json={"services": [original, cheaper]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["changed"] == 1
    assert body["results"][0]["current_service"] == "DROPOUT-STD"
    assert body["results"][0]["draft_service"] == "CHEAP"


def test_dry_run_rejects_an_invalid_working_copy(client: TestClient) -> None:
    # Two services sharing a tie-break can't form a rulebook - a bad request (422),
    # not a 500.
    clash = {**_DROPOUT, "code": "OTHER"}  # same tie_break_order as _DROPOUT
    response = client.post(
        "/api/rulebook/builder/dry-run", json={"services": [_DROPOUT, clash]}
    )
    assert response.status_code == 422


def test_dry_run_over_the_cap_is_rejected(client: TestClient) -> None:
    too_many = [{**_DROPOUT, "code": f"S{i}", "tie_break_order": i} for i in range(501)]
    response = client.post("/api/rulebook/builder/dry-run", json={"services": too_many})
    assert response.status_code == 422
