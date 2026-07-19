"""The AI rules builder (ADR 0017): granular edits to an in-memory working copy and
a dry-run of it, exercised end to end with a scripted fake model - never the real API.
Edits mutate the working copy only; nothing is saved here (the operator commits it as
a draft through the rulebook rails)."""

from collections.abc import Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nimbleship.assistant import LlmReply, ToolUse
from nimbleship.domain.allocation import ServiceDeclaration
from nimbleship.rules_builder import (
    InvalidWorkingCopy,
    WorkingCopy,
    build,
    suggest_rationale,
)
from nimbleship.rules_builder.tools import (
    _SERVICE_PROPERTIES,
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
    assert "duplicate service code" in str(result["error"])
    assert len(state.services) == 1


def test_add_service_rejects_a_clashing_tie_break() -> None:
    # A new service with a distinct code but a tie-break already in use is rejected -
    # the cross-service invariant a single ServiceDeclaration can't enforce.
    state = _copy(_DROPOUT)
    clash = {**_DROPOUT, "code": "OTHER"}  # tie_break_order 1, same as _DROPOUT
    result = add_service(state, {"service": clash})
    assert "duplicate tie-break order" in str(result["error"])
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


def test_update_service_rejects_a_rename_onto_another_code() -> None:
    # Renaming one service's code onto another's would leave two same-coded services;
    # the working copy the operator sees must not silently reach that state (it also
    # makes a later remove-by-code delete both). Rejected, nothing changed.
    other = {**_DROPOUT, "code": "OTHER", "tie_break_order": 2}
    state = _copy(_DROPOUT, other)
    result = update_service(
        state, {"code": "OTHER", "changes": {"code": "DROPOUT-STD"}}
    )
    assert "duplicate service code" in str(result["error"])
    assert [s.code for s in state.services] == ["DROPOUT-STD", "OTHER"]


def test_update_service_rejects_a_clashing_tie_break() -> None:
    other = {**_DROPOUT, "code": "OTHER", "tie_break_order": 2}
    state = _copy(_DROPOUT, other)
    result = update_service(state, {"code": "OTHER", "changes": {"tie_break_order": 1}})
    assert "duplicate tie-break order" in str(result["error"])
    assert [s.tie_break_order for s in state.services] == [1, 2]


_BAND = {
    "cost_type": "consignment_weight",
    "min_weight_kg": "0",
    "max_weight_kg": "999",
    "charge": "0.01",
}


def test_add_service_rejects_banded_pricing_fields() -> None:
    # Banded pricing is out of scope (ADR 0017) - a rate card managed elsewhere. A
    # tool call that carries it (a model hallucination, free-text steering) is
    # rejected, so a band can't be silently attached to the working copy.
    state = WorkingCopy()
    result = add_service(state, {"service": {**_DROPOUT, "cost_bands": [_BAND]}})
    assert "not fields the builder sets: cost_bands" in str(result["error"])
    assert state.services == []


def test_update_service_rejects_authoring_banded_pricing() -> None:
    state = _copy(_DROPOUT)
    result = update_service(
        state, {"code": "DROPOUT-STD", "changes": {"charge_bands": [_BAND]}}
    )
    assert "not fields the builder sets: charge_bands" in str(result["error"])
    assert state.services[0].charge_bands is None


def test_update_service_rejects_clearing_bands_with_a_null() -> None:
    # A null band field is still out of scope: clearing an existing band is a pricing
    # change with routing impact, so it's rejected, not silently applied.
    state = _copy({**_DROPOUT, "cost_bands": [_BAND]})
    result = update_service(
        state, {"code": "DROPOUT-STD", "changes": {"cost_bands": None}}
    )
    assert "not fields the builder sets: cost_bands" in str(result["error"])
    assert state.services[0].cost_bands is not None  # unchanged


def test_the_settable_fields_are_the_declaration_fields_minus_pricing() -> None:
    # The allow-list must stay exactly the ServiceDeclaration fields minus the two
    # banded-pricing fields, so a field added to the model later forces a deliberate
    # choice here rather than being silently rejected (or a new band silently let in).
    settable = set(ServiceDeclaration.model_fields) - {"cost_bands", "charge_bands"}
    assert set(_SERVICE_PROPERTIES) == settable


def test_update_service_rejects_a_misnamed_field_instead_of_no_op() -> None:
    # A typo'd field name (pydantic would silently drop it, validating the unchanged
    # service and reporting a success that changed nothing) is rejected, so the model
    # gets a signal to retry rather than telling the operator of a change never made.
    state = _copy(_DROPOUT)
    result = update_service(
        state, {"code": "DROPOUT-STD", "changes": {"max_weight_kg": "50"}}
    )
    assert "not fields the builder sets: max_weight_kg" in str(result["error"])
    assert str(state.services[0].weight_max_kg) == "999"  # unchanged


def test_update_service_preserves_a_seeded_services_existing_bands() -> None:
    # A real service seeded from the live rulebook may carry bands; editing a flat
    # field keeps them byte-for-byte (the builder preserves what it doesn't author).
    state = _copy({**_DROPOUT, "cost_bands": [_BAND]})
    result = update_service(state, {"code": "DROPOUT-STD", "changes": {"cost": "9.99"}})
    assert result["updated"] == "DROPOUT-STD"
    assert str(state.services[0].cost) == "9.99"
    dumped = state.services[0].model_dump(mode="json")["cost_bands"]
    assert dumped is not None and dumped[0]["charge"] == "0.01"


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


class _CapturingLlm:
    """Records the diff text the rationale suggester feeds it, so a test can pin that
    the one-liner is grounded in the real change, not guessed."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.seen: list[list[Message]] = []

    def reply(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[dict[str, object]],
    ) -> LlmReply:
        self.seen.append(messages)
        return LlmReply(stop_reason="end_turn", text=self.text, tool_uses=())


def test_suggest_rationale_grounds_a_one_liner_in_the_computed_diff() -> None:
    active = [ServiceDeclaration.model_validate(_DROPOUT)]
    added = ServiceDeclaration.model_validate(
        {**_DROPOUT, "code": "FR-NEXT", "tie_break_order": 2, "countries": ["FR"]}
    )
    llm = _CapturingLlm("Added FR-NEXT for France.")

    result = suggest_rationale(active, [*active, added], llm=llm)

    assert result == "Added FR-NEXT for France."
    # The model was handed the actual added service, not left to guess.
    assert "FR-NEXT" in str(llm.seen[0])


def test_suggest_rationale_is_none_and_calls_nothing_when_unchanged() -> None:
    # An unchanged copy has nothing to describe - and must not spend a model call
    # (the fake has no replies, so a call would raise).
    active = [ServiceDeclaration.model_validate(_DROPOUT)]
    assert suggest_rationale(active, list(active), llm=_FakeLlm([])) is None


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


def test_build_rejects_a_client_seed_that_breaks_an_invariant(app: FastAPI) -> None:
    # The working copy rides the request each turn, so a caller (a stale tab, a
    # buggy integration) can resend two same-coded services. The server validates
    # the seed before any edit, rather than operating on a copy where a later remove
    # would delete both.
    duplicate = [
        ServiceDeclaration.model_validate(_DROPOUT),
        ServiceDeclaration.model_validate({**_DROPOUT, "tie_break_order": 2}),
    ]
    conversation: list[Message] = [{"role": "user", "content": "x"}]
    with (
        app.state.session_factory() as session,
        pytest.raises(InvalidWorkingCopy, match="duplicate service code"),
    ):
        build(session, conversation, duplicate, llm=_FakeLlm([]))
