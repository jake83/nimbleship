"""The AI rules builder (ADR 0017): granular edits to an in-memory working copy and
a dry-run of it, exercised end to end with a scripted fake model - never the real API.
Edits mutate the working copy only; nothing is saved here (the operator commits it as
a draft through the rulebook rails)."""

from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nimbleship.assistant import LlmReply, ToolUse
from nimbleship.domain.allocation import ServiceDeclaration
from nimbleship.rules_builder import WorkingCopy, build
from nimbleship.rules_builder.tools import (
    add_service,
    dry_run,
    remove_service,
    run_builder_tool,
    update_service,
)

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


def _copy(*services: dict[str, object]) -> WorkingCopy:
    return WorkingCopy(
        services=[ServiceDeclaration.model_validate(s) for s in services]
    )


def test_add_service_appends_and_rejects_a_duplicate_code() -> None:
    state = WorkingCopy()
    assert add_service(state, {"service": _DROPOUT})["added"] == "DROPOUT-STD"
    assert len(state.services) == 1
    # A second add of the same code changes nothing and hands back an error to retry.
    result = add_service(state, {"service": _DROPOUT})
    assert "already exists" in str(result["error"])
    assert len(state.services) == 1


def test_add_service_rejects_an_invalid_service_without_changing_anything() -> None:
    state = WorkingCopy()
    result = add_service(state, {"service": {"code": "X"}})  # missing required fields
    assert "invalid service" in str(result["error"])
    assert state.services == []


def test_update_service_changes_one_field_and_keeps_the_rest() -> None:
    state = _copy(_DROPOUT)
    result = update_service(state, {"code": "DROPOUT-STD", "changes": {"cost": "9.99"}})
    assert result["updated"] == "DROPOUT-STD"
    [service] = state.services
    assert str(service.cost) == "9.99"
    assert service.carrier == "dropout"  # untouched


def test_update_service_rejects_unknown_code_and_invalid_change() -> None:
    state = _copy(_DROPOUT)
    assert "no service with code" in str(
        update_service(state, {"code": "NOPE", "changes": {"cost": "1"}})["error"]
    )
    bad = update_service(
        state, {"code": "DROPOUT-STD", "changes": {"weight_min_kg": "heavy"}}
    )
    assert "invalid change" in str(bad["error"])
    assert str(state.services[0].weight_min_kg) == "0"  # unchanged


def test_remove_service_drops_by_code() -> None:
    state = _copy(_DROPOUT)
    assert remove_service(state, {"code": "DROPOUT-STD"})["removed"] == "DROPOUT-STD"
    assert state.services == []
    assert "no service with code" in str(
        remove_service(state, {"code": "DROPOUT-STD"})["error"]
    )


def test_dry_run_reports_impact_over_historical_orders(
    app: FastAPI, client: TestClient
) -> None:
    # Publish a rulebook and ship an order under it, then dry-run a working copy that
    # reroutes that order to a new, cheaper service - the report names the change.
    version = client.post(
        "/api/rulebook/drafts", json={"author": "j", "services": [_DROPOUT]}
    ).json()["version"]
    client.post(f"/api/rulebook/versions/{version}/publish")
    order = {
        "order_number": "95000254580",
        "recipient_name": "John Doe",
        "address_lines": ["10 Downing Street", "London"],
        "postcode": "SW1A 2AA",
        "destination_country": "GB",
        "parcels": [{"weight_kg": "4.2"}],
    }
    assert client.post("/api/consignments", json=order).status_code == 201

    cheaper = {**_DROPOUT, "code": "CHEAP", "cost": "1.00", "tie_break_order": 1}
    original = {**_DROPOUT, "tie_break_order": 2}
    state = _copy(original, cheaper)
    with app.state.session_factory() as session:
        report = dry_run(session, state, {})

    assert report["orders_considered"] == 1
    assert report["orders_changed"] == 1
    changes = report["sample_changes"]
    assert isinstance(changes, list)
    assert changes[0]["from"] == "DROPOUT-STD" and changes[0]["to"] == "CHEAP"


def test_dry_run_rejects_an_invalid_working_copy(app: FastAPI) -> None:
    # Two services sharing a tie-break can't form a rulebook; the tool reports it
    # rather than crashing the loop.
    clash = {**_DROPOUT, "code": "OTHER"}  # same tie_break_order as _DROPOUT
    state = _copy(_DROPOUT, clash)
    with app.state.session_factory() as session:
        result = dry_run(session, state, {})
    assert "not a valid rulebook" in str(result["error"])


def test_run_builder_tool_reports_an_unknown_tool(app: FastAPI) -> None:
    with app.state.session_factory() as session:
        assert "unknown tool" in str(
            run_builder_tool(session, WorkingCopy(), "no_such_tool", {})["error"]
        )


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


def test_build_applies_an_edit_and_returns_the_working_copy(app: FastAPI) -> None:
    # The model adds a service, then replies; the loop applies the edit to the working
    # copy and hands it back for the operator to review and commit.
    llm = _FakeLlm(
        [
            LlmReply(
                stop_reason="tool_use",
                text="",
                tool_uses=(ToolUse("t1", "add_service", {"service": _DROPOUT}),),
            ),
            LlmReply(
                stop_reason="end_turn", text="Added Drop Out Standard.", tool_uses=()
            ),
        ]
    )
    with app.state.session_factory() as session:
        result = build(
            session,
            [{"role": "user", "content": "add drop out standard"}],
            [],
            llm=llm,
        )

    assert "Added" in result.reply
    assert [s.code for s in result.services] == ["DROPOUT-STD"]
