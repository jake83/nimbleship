"""The AI carrier builder (ADR 0018): granular edits to an in-memory working copy of a
CarrierDefinition, exercised end to end with a scripted fake model - never the real
API. Edits mutate the working copy only; nothing is saved (the operator commits it as a
draft through the definition rails)."""

from collections.abc import Sequence
from copy import deepcopy

import pytest
from fastapi import FastAPI

from nimbleship.assistant import LlmReply, ToolUse
from nimbleship.carrier_builder import WorkingDefinition, build
from nimbleship.carrier_builder.handoff import (
    blockers_for,
    raise_blocker,
    resolve_blocker,
)
from nimbleship.carrier_builder.tools import (
    check,
    list_blockers_tool,
    mark_applicable,
    mark_not_applicable,
    put_mapping_entry,
    put_operation,
    put_step,
    raise_blocker_tool,
    remove_mapping_entry,
    remove_operation,
    remove_step,
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


def test_set_identity_rejects_a_blank_carrier_or_name() -> None:
    # A blank identity would pass whole-definition validation (no min_length) but is
    # meaningless as a rails key and unsaveable in the surface.
    state = WorkingDefinition()
    assert "must not be blank" in str(
        set_identity(state, {"carrier": "", "name": "Acme"})["error"]
    )
    assert "must not be blank" in str(
        set_identity(state, {"carrier": "acme", "name": "  "})["error"]
    )
    assert state.data == {}


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


def test_put_operation_rejects_a_deeply_nested_misspelt_field() -> None:
    # The guard reaches any depth: a typo inside a mapping entry's transform is dropped
    # by pydantic too, and must be rejected, not just a top-level operation key.
    state = WorkingDefinition()
    operation = {
        "steps": [
            {
                "name": "book",
                "transport": "http",
                "request": {
                    "method": "POST",
                    "url": "config.url",
                    "content_type": "json",
                    "mapping": [
                        {
                            "target": "order",
                            "source": "shipment.order_number",
                            "transform": {"name": "uppercase", "bogus": 1},
                        }
                    ],
                },
            }
        ]
    }
    result = put_operation(state, {"name": "book", "operation": operation})
    assert "unknown field 'bogus'" in str(result["error"])
    assert state.operations() == {}


def _op_with_book_step() -> WorkingDefinition:
    state = WorkingDefinition()
    put_operation(state, {"name": "book", "operation": deepcopy(_OPERATION)})
    return state


def test_put_mapping_entry_replaces_one_entry_and_keeps_the_rest() -> None:
    state = _op_with_book_step()
    put_mapping_entry(
        state,
        {
            "operation": "book",
            "step": "book",
            "entry": {"target": "channel", "const": "nimbleship"},
        },
    )
    result = put_mapping_entry(
        state,
        {
            "operation": "book",
            "step": "book",
            "entry": {"target": "order", "source": "shipment.order_number"},
        },
    )
    assert result == {"operation": "book", "target": "order"}
    mapping = state.operations()["book"]["steps"][0]["request"]["mapping"]  # type: ignore[index]
    assert [e["target"] for e in mapping] == ["order", "channel"]


def test_put_mapping_entry_rejects_an_invalid_entry_without_mutating() -> None:
    state = _op_with_book_step()
    # Two value origins on one entry is a shape the engine can't resolve.
    result = put_mapping_entry(
        state,
        {
            "operation": "book",
            "step": "book",
            "entry": {
                "target": "order",
                "source": "shipment.order_number",
                "const": "x",
            },
        },
    )
    assert "error" in result
    mapping = state.operations()["book"]["steps"][0]["request"]["mapping"]  # type: ignore[index]
    assert len(mapping) == 1  # unchanged


def test_put_mapping_entry_rejects_a_misspelt_field_at_depth() -> None:
    state = _op_with_book_step()
    result = put_mapping_entry(
        state,
        {
            "operation": "book",
            "step": "book",
            "entry": {"target": "x", "soruce": "shipment.order_number"},
        },
    )
    assert "error" in result


def test_remove_mapping_entry_drops_by_target() -> None:
    state = _op_with_book_step()
    put_mapping_entry(
        state,
        {
            "operation": "book",
            "step": "book",
            "entry": {"target": "channel", "const": "nimbleship"},
        },
    )
    assert remove_mapping_entry(
        state, {"operation": "book", "step": "book", "target": "channel"}
    ) == {"operation": "book", "removed": "channel"}
    assert "no mapping entry" in str(
        remove_mapping_entry(
            state, {"operation": "book", "step": "book", "target": "channel"}
        )["error"]
    )


def test_put_step_adds_and_replaces_by_name_keeping_siblings() -> None:
    state = _op_with_book_step()
    label_step = {
        "name": "label",
        "transport": "http",
        "request": {
            "method": "GET",
            "url": "config.label_url",
            "content_type": "json",
            "mapping": [{"target": "ref", "source": "steps.book.tracking_reference"}],
        },
    }
    assert put_step(state, {"operation": "book", "step": label_step}) == {
        "operation": "book",
        "step": "label",
    }
    steps = state.operations()["book"]["steps"]  # type: ignore[index]
    assert [s["name"] for s in steps] == ["book", "label"]


def test_remove_step_refuses_to_leave_an_invalid_operation() -> None:
    # Removing the only step of an operation with no local_render label would leave
    # an operation the engine can't run - remove the whole operation instead.
    state = _op_with_book_step()
    result = remove_step(state, {"operation": "book", "name": "book"})
    assert "error" in result
    assert "book" in state.operations()  # unchanged


def test_granular_edits_refuse_an_ambiguous_duplicate_key() -> None:
    # A whole-op put (or client seed) can carry two entries sharing a target; the
    # granular tools address by that key, so the ambiguity is refused - removing all
    # matches would silently delete more than asked, editing the first would strand
    # the duplicate.
    duplicated = {
        "steps": [
            {
                "name": "book",
                "transport": "http",
                "request": {
                    "method": "POST",
                    "url": "config.url",
                    "content_type": "json",
                    "mapping": [
                        {"target": "t", "const": "one"},
                        {"target": "keep", "const": "kept"},
                        {"target": "t", "const": "two"},
                    ],
                },
            }
        ]
    }
    state = WorkingDefinition(data={"operations": {"book": duplicated}})
    removed = remove_mapping_entry(
        state, {"operation": "book", "step": "book", "target": "t"}
    )
    assert "matches more than one mapping entry" in str(removed["error"])
    replaced = put_mapping_entry(
        state,
        {"operation": "book", "step": "book", "entry": {"target": "t", "const": "x"}},
    )
    assert "matches more than one" in str(replaced["error"])
    mapping = state.operations()["book"]["steps"][0]["request"]["mapping"]  # type: ignore[index]
    assert len(mapping) == 3  # nothing was deleted or edited


def test_granular_edits_reject_a_malformed_client_seeded_base() -> None:
    # An operation can arrive straight from the client's seed without ever passing
    # the write gate; a malformed one must be a clean tool error, not a crash (500).
    state = WorkingDefinition(data={"operations": {"book": {"steps": "not-a-list"}}})
    step = {"name": "x", "transport": "local_render", "request": {}}
    for result in (
        put_step(state, {"operation": "book", "step": step}),
        remove_step(state, {"operation": "book", "name": "x"}),
        put_mapping_entry(
            state, {"operation": "book", "step": "x", "entry": {"target": "t"}}
        ),
        remove_mapping_entry(state, {"operation": "book", "step": "x", "target": "t"}),
    ):
        assert "cannot edit" in str(result["error"])


def test_granular_edits_report_unknown_operation_and_step() -> None:
    state = _op_with_book_step()
    assert "no operation" in str(
        put_step(state, {"operation": "nope", "step": {"name": "x"}})["error"]
    )
    assert "no step" in str(
        put_mapping_entry(
            state, {"operation": "book", "step": "nope", "entry": {"target": "x"}}
        )["error"]
    )


def test_remove_operation_drops_by_name() -> None:
    state = _complete()
    assert remove_operation(state, {"name": "book"})["removed"] == "book"
    assert state.operations() == {}
    assert "no operation" in str(remove_operation(state, {"name": "book"})["error"])


def test_mark_not_applicable_records_and_mark_applicable_clears() -> None:
    state = WorkingDefinition()
    result = mark_not_applicable(
        state, {"capability": "manifest", "reason": "no end-of-day process"}
    )
    assert result == {"not_applicable": "manifest"}
    assert state.not_applicable == {"manifest": "no end-of-day process"}
    assert mark_applicable(state, {"capability": "manifest"}) == {
        "applicable": "manifest"
    }
    assert state.not_applicable == {}


def test_mark_not_applicable_is_bounded_to_the_prunable_frame() -> None:
    state = WorkingDefinition()
    # A status for something the engine cannot invoke is meaningless (ADR 0018).
    assert "error" in mark_not_applicable(
        state, {"capability": "webhooks", "reason": "x"}
    )
    # An integration that cannot book is not an integration.
    assert "error" in mark_not_applicable(state, {"capability": "book", "reason": "x"})
    assert "error" in mark_not_applicable(
        state, {"capability": "manifest", "reason": " "}
    )
    assert "error" in mark_applicable(state, {"capability": "manifest"})  # not marked
    assert state.not_applicable == {}


def test_mark_not_applicable_refuses_a_drafted_capability() -> None:
    # A capability can't be both drafted and N/A; the draft wins.
    state = _complete()
    put_operation(state, {"name": "manifest", "operation": _OPERATION})
    assert "drafted" in str(
        mark_not_applicable(state, {"capability": "manifest", "reason": "none"})[
            "error"
        ]
    )
    # Label lives on an operation's label spec, not as an operation of its own.
    labelled = deepcopy(_OPERATION)
    labelled["label"] = {"source": "local_render", "template": "labels/acme"}
    put_operation(state, {"name": "book", "operation": labelled})
    assert "drafted" in str(
        mark_not_applicable(state, {"capability": "label", "reason": "none"})["error"]
    )
    assert state.not_applicable == {}


def test_put_operation_clears_a_stale_not_applicable_mark() -> None:
    # Drafting the capability is decisive evidence it applies; the mark clears with
    # the edit rather than leaving the board contradicting the definition.
    state = WorkingDefinition()
    mark_not_applicable(state, {"capability": "manifest", "reason": "assumed absent"})
    result = put_operation(state, {"name": "manifest", "operation": _OPERATION})
    assert result["operation"] == "manifest"
    assert result["cleared_not_applicable"] == ["manifest"]
    assert state.not_applicable == {}


def test_an_operation_merely_named_label_does_not_clear_the_label_mark() -> None:
    # Label is spec-keyed, never name-keyed: an operation named "label" with no label
    # spec drafts nothing, so the documented mark must survive the edit intact.
    state = WorkingDefinition()
    mark_not_applicable(state, {"capability": "label", "reason": "no label of its own"})
    result = put_operation(state, {"name": "label", "operation": _OPERATION})
    assert result["operation"] == "label"
    assert "cleared_not_applicable" not in result
    assert state.not_applicable == {"label": "no label of its own"}


def test_a_malformed_not_applicable_seed_is_normalised_not_crashed() -> None:
    # The mark rides each turn from the client like the rest of the working copy:
    # junk keys and blank reasons are dropped rather than trusted or crashed on.
    state = WorkingDefinition(
        not_applicable={"webhooks": "x", "book": "x", "label": " ", "manifest": "real"}
    )
    assert state.not_applicable == {"manifest": "real"}


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


def test_run_tool_reports_an_unknown_tool(app: FastAPI) -> None:
    with app.state.session_factory() as session:
        assert "unknown tool" in str(
            run_carrier_builder_tool(session, WorkingDefinition(), "no_such_tool", {})[
                "error"
            ]
        )


def test_the_applicability_tools_dispatch_by_name(app: FastAPI) -> None:
    state = WorkingDefinition()
    with app.state.session_factory() as session:
        marked = run_carrier_builder_tool(
            session,
            state,
            "mark_not_applicable",
            {"capability": "manifest", "reason": "no end-of-day"},
        )
        assert marked == {"not_applicable": "manifest"}
        cleared = run_carrier_builder_tool(
            session, state, "mark_applicable", {"capability": "manifest"}
        )
        assert cleared == {"applicable": "manifest"}
    assert state.not_applicable == {}


def test_raise_and_resolve_a_blocker(app: FastAPI) -> None:
    with app.state.session_factory() as session:
        blocker = raise_blocker(
            session,
            "acme",
            "needs_decision",
            "Live or test endpoint?",
            "Docs list two.",
        )
        assert blocker.status == "open"
        resolved = resolve_blocker(session, blocker.id, "Use the live endpoint.")
        assert resolved.status == "resolved"
        assert resolved.resolution == "Use the live endpoint."
        assert resolved.resolved_at is not None
        # Resolving twice would silently overwrite the recorded answer - refused.
        with pytest.raises(ValueError, match="already resolved"):
            resolve_blocker(session, blocker.id, "Different answer.")


def test_raise_blocker_rejects_an_unknown_kind(app: FastAPI) -> None:
    with (
        app.state.session_factory() as session,
        pytest.raises(ValueError, match="unknown blocker kind"),
    ):
        raise_blocker(session, "acme", "needs_coffee", "t", "d")


def test_raise_blocker_enforces_its_invariants_at_the_domain(app: FastAPI) -> None:
    # Enforced where the row is built, not just in the tool wrapper: a needs_plugin
    # blocker names its plugin, and over-column-length values are refused here rather
    # than passing SQLite silently and 500ing on Postgres at flush.
    with app.state.session_factory() as session:
        with pytest.raises(ValueError, match="must name the plugin"):
            raise_blocker(session, "acme", "needs_plugin", "t", "d")
        with pytest.raises(ValueError, match="carrier must be"):
            raise_blocker(session, "c" * 65, "needs_decision", "t", "d")
        with pytest.raises(ValueError, match="must not be blank"):
            raise_blocker(session, "acme", "needs_decision", "  ", "d")
        with pytest.raises(ValueError, match="must not be blank"):
            raise_blocker(session, "acme", "needs_decision", "t", "")
        with pytest.raises(ValueError, match="title must be"):
            raise_blocker(session, "acme", "needs_decision", "x" * 256, "d")
        with pytest.raises(ValueError, match="plugin_name must be"):
            raise_blocker(
                session, "acme", "needs_plugin", "t", "d", plugin_name="p" * 65
            )


def test_raise_blocker_tool_requires_identity_and_plugin_name(app: FastAPI) -> None:
    with app.state.session_factory() as session:
        # No carrier identity yet: the blocker would be unkeyed - refused.
        no_identity = raise_blocker_tool(
            session,
            WorkingDefinition(),
            {"kind": "needs_decision", "title": "t", "detail": "d"},
        )
        assert "set the carrier identity" in str(no_identity["error"])
        # needs_plugin must name the plugin the definition will reference.
        state = WorkingDefinition(data={"carrier": "acme", "name": "Acme"})
        unnamed = raise_blocker_tool(
            session, state, {"kind": "needs_plugin", "title": "t", "detail": "d"}
        )
        assert "must name the plugin" in str(unnamed["error"])


def test_list_blockers_tool_surfaces_the_engineers_resolution(app: FastAPI) -> None:
    state = WorkingDefinition(data={"carrier": "acme", "name": "Acme"})
    with app.state.session_factory() as session:
        raised = raise_blocker_tool(
            session,
            state,
            {"kind": "needs_decision", "title": "Which endpoint?", "detail": "Two."},
        )
        resolve_blocker(session, int(str(raised["blocker_id"])), "Use live.")
        listed = list_blockers_tool(session, state, {})

    blockers = listed["blockers"]
    assert isinstance(blockers, list)
    assert blockers[0]["status"] == "resolved"
    assert blockers[0]["resolution"] == "Use live."


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


def test_build_assembles_a_definition_from_the_conversation(app: FastAPI) -> None:
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

    with app.state.session_factory() as session:
        result = build(
            session, [{"role": "user", "content": "onboard acme"}], {}, llm=llm
        )

    assert "Drafted" in result.reply
    assert result.definition["carrier"] == "acme"
    assert "book" in result.definition["operations"]  # type: ignore[operator]


def test_build_can_raise_a_durable_blocker_and_keep_building(app: FastAPI) -> None:
    # The model parks a technical gap for the engineer and continues with the rest;
    # the blocker outlives the conversation (a plugin ships days later).
    llm = _FakeLlm(
        [
            LlmReply(
                stop_reason="tool_use",
                text="",
                tool_uses=(
                    ToolUse("t1", "set_identity", {"carrier": "acme", "name": "Acme"}),
                    ToolUse(
                        "t2",
                        "raise_blocker",
                        {
                            "kind": "needs_plugin",
                            "title": "HMAC request signing",
                            "detail": "Requests need an HMAC signature; no plugin.",
                            "plugin_name": "acme_hmac",
                        },
                    ),
                    ToolUse(
                        "t3", "put_operation", {"name": "book", "operation": _OPERATION}
                    ),
                ),
            ),
            LlmReply(
                stop_reason="end_turn",
                text="Parked the signing for engineering; drafted the rest.",
                tool_uses=(),
            ),
        ]
    )

    with app.state.session_factory() as session:
        result = build(
            session, [{"role": "user", "content": "onboard acme"}], {}, llm=llm
        )
        session.commit()

    assert "book" in result.definition["operations"]  # type: ignore[operator]
    with app.state.session_factory() as session:
        [blocker] = blockers_for(session, "acme")
        assert blocker.kind == "needs_plugin"
        assert blocker.plugin_name == "acme_hmac"
        assert blocker.status == "open"


def test_build_round_trips_the_not_applicable_marks(app: FastAPI) -> None:
    # The marks ride each turn with the working copy: the seed survives the turn and
    # the model's new mark comes back with it, so the board never loses a pruning.
    llm = _FakeLlm(
        [
            LlmReply(
                stop_reason="tool_use",
                text="",
                tool_uses=(
                    ToolUse(
                        "t1",
                        "mark_not_applicable",
                        {"capability": "label", "reason": "paperwork only"},
                    ),
                ),
            ),
            LlmReply(stop_reason="end_turn", text="Pruned the board.", tool_uses=()),
        ]
    )

    with app.state.session_factory() as session:
        result = build(
            session,
            [{"role": "user", "content": "they have no labels"}],
            {},
            not_applicable={"manifest": "no end-of-day process"},
            llm=llm,
        )

    assert result.not_applicable == {
        "manifest": "no end-of-day process",
        "label": "paperwork only",
    }
