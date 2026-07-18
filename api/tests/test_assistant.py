"""The AI assistant (ADR 0016): read-only tools over one order, and the tool-use
loop exercised end to end with a scripted fake model - never the real API."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nimbleship.assistant import (
    AnthropicClient,
    LlmReply,
    ToolUse,
    answer,
    build_client,
)
from nimbleship.assistant.tools import (
    allocation_trace,
    manifest_status,
    order_timeline,
    run_tool,
    tracking,
)
from nimbleship.models import TrackingEvent

_ORDER = "95000254580"
_CONSIGNMENT = {
    "order_number": _ORDER,
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}, {"weight_kg": "3.1"}],  # 7.3kg total
}

Message = dict[str, object]


def _publish_two_service_rulebook(client: TestClient) -> None:
    # dropout is eligible and cheapest of the eligible; heavycarrier needs >=100kg,
    # so the 7.3kg order fails its weight check - a rejected service the trace explains.
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
            },
            {
                "code": "HEAVY-ONLY",
                "carrier": "heavycarrier",
                "name": "Heavy Only",
                "weight_min_kg": "100",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "3.00",
                "tie_break_order": 2,
            },
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


def test_allocation_trace_names_the_failed_check_of_a_rejected_service(
    app: FastAPI, client: TestClient
) -> None:
    _publish_two_service_rulebook(client)
    assert client.post("/api/consignments", json=_CONSIGNMENT).status_code == 201

    with app.state.session_factory() as session:
        trace = allocation_trace(session, _ORDER)

    assert trace["found"] is True
    assert trace["selected"] == {
        "carrier": "dropout",
        "service": "DROPOUT-STD",
        "cost": "4.50",
    }
    services = {s["service_code"]: s for s in trace["services"]}  # type: ignore[attr-defined]
    assert services["DROPOUT-STD"]["eligible"] is True
    heavy = services["HEAVY-ONLY"]
    assert heavy["eligible"] is False
    [weight] = [c for c in heavy["failed_checks"] if c["name"] == "weight"]
    assert weight["actual"] == "7.30kg"
    assert "100" in weight["expected"]


def test_order_timeline_lists_the_lifecycle_events(
    app: FastAPI, client: TestClient
) -> None:
    _publish_two_service_rulebook(client)
    client.post("/api/consignments", json=_CONSIGNMENT)

    with app.state.session_factory() as session:
        timeline = order_timeline(session, _ORDER)

    stages = [e["stage"] for e in timeline["events"]]  # type: ignore[attr-defined]
    assert "allocated" in stages
    assert "label_created" in stages


def test_tracking_and_manifest_are_empty_for_a_fresh_order(
    app: FastAPI, client: TestClient
) -> None:
    _publish_two_service_rulebook(client)
    client.post("/api/consignments", json=_CONSIGNMENT)

    with app.state.session_factory() as session:
        assert tracking(session, _ORDER)["current_status"] is None
        assert manifest_status(session, _ORDER)["found"] is False


def test_tools_tell_a_known_pending_order_from_an_unknown_one(
    app: FastAPI, client: TestClient
) -> None:
    # A real order that hasn't manifested yet and a mistyped order number both have
    # no manifest, but order_known distinguishes them, so the assistant can say "no
    # such order" instead of guessing (ADR 0016 grounding).
    _publish_two_service_rulebook(client)
    client.post("/api/consignments", json=_CONSIGNMENT)

    with app.state.session_factory() as session:
        pending = manifest_status(session, _ORDER)
        unknown = manifest_status(session, "NOT-A-REAL-ORDER")
        assert pending["found"] is False and pending["order_known"] is True
        assert unknown["found"] is False and unknown["order_known"] is False
        assert tracking(session, _ORDER)["order_known"] is True
        assert tracking(session, "NOT-A-REAL-ORDER")["order_known"] is False
        assert order_timeline(session, "NOT-A-REAL-ORDER")["order_known"] is False


def test_order_known_counts_a_tracking_only_order(app: FastAPI) -> None:
    # A carrier webhook can post tracking for an order with no consignment yet; that
    # order is known, so order_known must agree with the events tracking() returns -
    # not contradict them.
    with app.state.session_factory() as session:
        session.add(
            TrackingEvent(
                order_number="TRACK-ONLY",
                source="voila",
                external_id="E1",
                raw_status="4",
                status="in_transit",
                raw={},
            )
        )
        session.commit()

    with app.state.session_factory() as session:
        result = tracking(session, "TRACK-ONLY")
        assert len(result["events"]) == 1  # type: ignore[arg-type]
        assert result["order_known"] is True


def test_run_tool_returns_an_error_dict_for_a_bad_call(
    app: FastAPI, client: TestClient
) -> None:
    # A hallucinated tool name or a missing order number is handed back to the model
    # as an error, never raised - the loop must not crash on a malformed model call.
    with app.state.session_factory() as session:
        assert "unknown tool" in str(
            run_tool(session, "no_such_tool", {"order_number": _ORDER})["error"]
        )
        assert "order_number is required" in str(
            run_tool(session, "order_timeline", {})["error"]
        )
        assert "order_number is required" in str(
            run_tool(session, "order_timeline", {"order_number": ""})["error"]
        )


@dataclass
class _ScriptedLlm:
    """A fake model: returns pre-scripted replies and records the messages it was
    sent each turn, so a test can assert a tool result was fed back before answering."""

    replies: list[LlmReply]
    seen: list[list[Message]] = field(default_factory=list)

    def reply(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[dict[str, object]],
    ) -> LlmReply:
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0)


def test_the_loop_runs_a_tool_and_grounds_the_answer_in_its_result(
    app: FastAPI, client: TestClient
) -> None:
    # The model asks for the allocation trace; the loop runs the real tool, feeds the
    # structured result back, and the model's answer follows - grounding end to end.
    _publish_two_service_rulebook(client)
    client.post("/api/consignments", json=_CONSIGNMENT)
    llm = _ScriptedLlm(
        replies=[
            LlmReply(
                stop_reason="tool_use",
                text="",
                tool_uses=(
                    ToolUse("t1", "allocation_trace", {"order_number": _ORDER}),
                ),
            ),
            LlmReply(
                stop_reason="end_turn",
                text="Order shipped with dropout; HEAVY-ONLY failed the weight check.",
                tool_uses=(),
            ),
        ]
    )
    conversation: list[Message] = [
        {"role": "user", "content": f"why did order {_ORDER} ship with dropout?"}
    ]

    with app.state.session_factory() as session:
        result = answer(session, conversation, llm=llm)

    assert "dropout" in result
    # The second model turn was sent the tool result carrying the real trace.
    tool_results: list[dict[str, object]] = []
    for message in llm.seen[1]:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        tool_results.extend(
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
    assert any("HEAVY-ONLY" in str(block["content"]) for block in tool_results)


def test_the_loop_stops_at_the_step_budget(app: FastAPI, client: TestClient) -> None:
    _publish_two_service_rulebook(client)
    client.post("/api/consignments", json=_CONSIGNMENT)

    @dataclass
    class _AlwaysTool:
        def reply(
            self,
            *,
            system: str,
            messages: list[Message],
            tools: Sequence[dict[str, object]],
        ) -> LlmReply:
            return LlmReply(
                stop_reason="tool_use",
                text="",
                tool_uses=(ToolUse("t", "order_timeline", {"order_number": _ORDER}),),
            )

    with app.state.session_factory() as session:
        result = answer(
            session, [{"role": "user", "content": "loop"}], llm=_AlwaysTool()
        )

    assert "step budget" in result


def test_a_tool_use_stop_with_no_calls_is_terminal(
    app: FastAPI, client: TestClient
) -> None:
    # A degenerate reply (tool_use stop, no calls) can't advance the loop and would
    # be an empty message the real API rejects; the loop returns its text instead.
    _publish_two_service_rulebook(client)
    client.post("/api/consignments", json=_CONSIGNMENT)
    llm = _ScriptedLlm(
        replies=[LlmReply(stop_reason="tool_use", text="nothing to add", tool_uses=())]
    )

    with app.state.session_factory() as session:
        result = answer(session, [{"role": "user", "content": "hi"}], llm=llm)

    assert result == "nothing to add"


def test_build_client_is_none_without_a_key_and_a_client_with_one() -> None:
    # Fail-closed (ADR 0016): no key means no client, so the caller reports
    # "not configured" rather than the module erroring. A key builds a real client
    # (its constructor makes no network call).
    assert build_client(None, "claude-sonnet-4-6") is None
    assert build_client("", "claude-sonnet-4-6") is None
    assert isinstance(build_client("a-key", "claude-sonnet-4-6"), AnthropicClient)
