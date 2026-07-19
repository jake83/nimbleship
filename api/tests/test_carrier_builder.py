"""The AI carrier builder (ADR 0018): granular edits to an in-memory working copy of a
CarrierDefinition, exercised end to end with a scripted fake model - never the real
API. Edits mutate the working copy only; nothing is saved (the operator commits it as a
draft through the definition rails)."""

from collections.abc import Sequence

from nimbleship.assistant import LlmReply, ToolUse
from nimbleship.carrier_builder import WorkingDefinition, build
from nimbleship.carrier_builder.tools import (
    check,
    put_operation,
    remove_operation,
    run_carrier_builder_tool,
    set_auth,
    set_identity,
)

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


def _complete() -> WorkingDefinition:
    return WorkingDefinition(
        data={
            "carrier": "acme",
            "name": "Acme",
            "auth": {"scheme": "none"},
            "operations": {"book": _OPERATION},
        }
    )


def test_set_identity_sets_carrier_and_name() -> None:
    state = WorkingDefinition()
    assert set_identity(state, {"carrier": "acme", "name": "Acme"})["carrier"] == "acme"
    assert state.data["carrier"] == "acme"
    assert state.data["name"] == "Acme"


def test_set_auth_rejects_an_invalid_scheme_and_accepts_a_valid_one() -> None:
    state = WorkingDefinition()
    assert "invalid auth" in str(set_auth(state, {"auth": {"scheme": "nope"}})["error"])
    assert "auth" not in state.data
    assert set_auth(state, {"auth": {"scheme": "none"}})["auth_scheme"] == "none"
    assert state.data["auth"] == {"scheme": "none"}


def test_set_auth_rejects_an_unregistered_plugin() -> None:
    # Inherits the authoring plugin gate: an auth the engine has no plugin for is a
    # defer-to-engineer signal, rejected rather than written.
    state = WorkingDefinition()
    result = set_auth(state, {"auth": {"scheme": "plugin", "plugin": "nonesuch"}})
    assert "unknown auth plugin" in str(result["error"])
    assert "auth" not in state.data


def test_put_operation_rejects_malformed_and_adds_valid() -> None:
    state = WorkingDefinition()
    bad = put_operation(state, {"name": "book", "operation": {"steps": "nope"}})
    assert "invalid operation" in str(bad["error"])
    assert state.operations() == {}
    assert put_operation(state, {"name": "book", "operation": _OPERATION})["operation"]
    assert "book" in state.operations()


def test_put_operation_rejects_a_misspelt_field_instead_of_dropping_it() -> None:
    # pydantic ignores an unknown key, so a mistyped `allocate` would silently drop the
    # SSCC minting the operator asked for while check() still reports valid - reject it.
    state = WorkingDefinition()
    operation = {
        **_OPERATION,
        "alloacte": [{"kind": "sscc", "per": "parcel", "prefix": "config.p"}],
    }
    result = put_operation(state, {"name": "book", "operation": operation})
    assert "unknown field 'alloacte'" in str(result["error"])
    assert state.operations() == {}


def test_remove_operation_drops_by_name() -> None:
    state = _complete()
    assert remove_operation(state, {"name": "book"})["removed"] == "book"
    assert state.operations() == {}
    assert "no operation" in str(remove_operation(state, {"name": "book"})["error"])


def test_check_reports_incomplete_then_valid() -> None:
    state = WorkingDefinition(data={"carrier": "acme", "name": "Acme"})
    incomplete = check(state, {})
    assert incomplete["valid"] is False
    assert "operations" in str(incomplete["errors"])
    assert check(_complete(), {})["valid"] is True


def test_a_malformed_operations_seed_is_normalised_not_crashed() -> None:
    # The working copy rides each turn from the client; a garbage operations value must
    # normalise to a usable dict rather than raise (an uncaught 500 at the route).
    state = WorkingDefinition(data={"operations": "not a dict"})
    assert put_operation(state, {"name": "book", "operation": _OPERATION})["operation"]
    assert list(state.operations()) == ["book"]


def test_run_tool_reports_an_unknown_tool() -> None:
    assert "unknown tool" in str(
        run_carrier_builder_tool(WorkingDefinition(), "no_such_tool", {})["error"]
    )


class _FakeLlm:
    def __init__(self, replies: list[LlmReply]) -> None:
        self._replies = replies
        self.systems: list[str] = []

    def reply(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[dict[str, object]],
    ) -> LlmReply:
        self.systems.append(system)
        return self._replies.pop(0)


def test_build_assembles_a_definition_from_the_conversation() -> None:
    # The model sets identity/auth/operation, then replies; the loop applies each edit
    # and hands back the working copy for the operator to review.
    llm = _FakeLlm(
        [
            LlmReply(
                stop_reason="tool_use",
                text="",
                tool_uses=(
                    ToolUse("t1", "set_identity", {"carrier": "acme", "name": "Acme"}),
                    ToolUse("t2", "set_auth", {"auth": {"scheme": "none"}}),
                    ToolUse(
                        "t3", "put_operation", {"name": "book", "operation": _OPERATION}
                    ),
                ),
            ),
            LlmReply(
                stop_reason="end_turn", text="Drafted Acme's book call.", tool_uses=()
            ),
        ]
    )

    result = build([{"role": "user", "content": "onboard acme"}], {}, llm=llm)

    assert "Drafted" in result.reply
    assert result.definition["carrier"] == "acme"
    assert "book" in result.definition["operations"]  # type: ignore[operator]
