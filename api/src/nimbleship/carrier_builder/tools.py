"""The carrier builder's tools (ADR 0018): granular edits to an in-memory working
copy of a CarrierDefinition being assembled turn by turn. Like the rules builder,
these mutate the working copy only in memory; nothing is saved. The copy is a partial
definition dict, validated as a whole CarrierDefinition only at check/save - mid-build
it is legitimately incomplete."""

from copy import deepcopy
from dataclasses import dataclass, field

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from nimbleship.carrier_builder.handoff import blockers_for, raise_blocker
from nimbleship.domain.carrier_definition import Auth, CarrierDefinition, Operation

_AUTH_ADAPTER: TypeAdapter[Auth] = TypeAdapter(Auth)


@dataclass
class WorkingDefinition:
    """The carrier definition being co-authored, assembled key by key. Starts empty
    for a new carrier (onboarding); saved as a draft only when the operator commits,
    through the existing definition rails."""

    data: dict[str, object] = field(default_factory=dict)

    def operations(self) -> dict[str, object]:
        # The copy rides each turn from the client, so operations may be absent or, on
        # a malformed seed, not a dict - normalise to one rather than trusting it.
        ops = self.data.get("operations")
        if not isinstance(ops, dict):
            ops = {}
            self.data["operations"] = ops
        return ops


def set_identity(
    state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """Set the carrier's code and human name."""
    carrier = tool_input.get("carrier")
    name = tool_input.get("name")
    if not isinstance(carrier, str) or not isinstance(name, str):
        return {"error": "set_identity needs 'carrier' and 'name' strings"}
    # The model rejects a blank identity too; checking here as well gives the model
    # an immediate, retryable tool error instead of a failure surfacing only at check.
    if not carrier.strip() or not name.strip():
        return {"error": "carrier and name must not be blank"}
    state.data["carrier"] = carrier
    state.data["name"] = name
    return {"carrier": carrier, "name": name}


def set_auth(
    state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """Set the auth scheme (query_key, header_key, none, or a named plugin). Rejects an
    invalid or unregistered-plugin scheme without changing anything, so the model can
    correct it - a plugin the engine doesn't have is a defer-to-engineer signal."""
    auth = tool_input.get("auth")
    if not isinstance(auth, dict):
        return {"error": "set_auth needs an 'auth' object"}
    try:
        _AUTH_ADAPTER.validate_python(auth)
    except ValidationError as error:
        return {"error": f"invalid auth: {error}"}
    state.data["auth"] = auth
    return {"auth_scheme": auth.get("scheme")}


def _dropped_key(provided: object, kept: object) -> str | None:
    """The first key in `provided` that `kept` doesn't have, comparing recursively, or
    None. `kept` is the validated operation re-dumped with only its set fields, so a key
    pydantic ignored (a misspelt field, at any depth) is exactly one that didn't
    survive - which would otherwise silently drop what the operator asked for while
    check() still reports the definition valid."""
    if isinstance(provided, dict) and isinstance(kept, dict):
        for key, value in provided.items():
            if key not in kept:
                return str(key)
            deeper = _dropped_key(value, kept[key])
            if deeper is not None:
                return deeper
    elif isinstance(provided, list) and isinstance(kept, list):
        for item, kept_item in zip(provided, kept, strict=False):
            deeper = _dropped_key(item, kept_item)
            if deeper is not None:
                return deeper
    return None


def _operation_error(name: str, operation: dict[str, object]) -> str | None:
    """The reason `operation` can't be stored, or None: malformed, or carrying a
    misspelt field pydantic would silently drop. The single gate for every write path
    into an operation - whole-operation and granular edits alike."""
    try:
        validated = Operation.model_validate(operation)
    except ValidationError as error:
        return f"invalid operation '{name}': {error}"
    kept = validated.model_dump(mode="json", by_alias=True, exclude_unset=True)
    dropped = _dropped_key(operation, kept)
    if dropped is not None:
        return f"unknown field '{dropped}' - check the spelling"
    return None


def put_operation(
    state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """Add or replace one named operation (book, manifest, ...) with its steps. For a
    small change to an existing operation, prefer put_step or put_mapping_entry -
    re-sending the whole operation risks perturbing the untouched parts. Rejects a
    malformed operation, or one carrying a misspelt field, without changing anything.
    Cross-operation rules (e.g. fan_out only on a manifest) are checked by `check`,
    which validates the whole definition."""
    name = tool_input.get("name")
    operation = tool_input.get("operation")
    if not isinstance(name, str) or not isinstance(operation, dict):
        return {"error": "put_operation needs a 'name' and an 'operation' object"}
    problem = _operation_error(name, operation)
    if problem is not None:
        return {"error": problem}
    state.operations()[name] = operation
    return {"operation": name}


def _existing_operation(
    state: WorkingDefinition, name: object
) -> tuple[str, dict[str, object]] | str:
    """The (name, deep copy) of an existing operation to edit, or an error string. A
    copy, so a rejected granular edit never leaves a half-applied operation behind."""
    if not isinstance(name, str):
        return "an 'operation' name is required"
    operation = state.operations().get(name)
    if not isinstance(operation, dict):
        return f"no operation '{name}'"
    return name, deepcopy(operation)


def _steps_of(operation: dict[str, object]) -> list[dict[str, object]]:
    steps = operation.setdefault("steps", [])
    assert isinstance(steps, list)  # _operation_error validated any stored operation
    return steps


def put_step(
    state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """Add or replace one named step within an existing operation, keeping its other
    steps untouched. Rejects an invalid result without changing anything."""
    found = _existing_operation(state, tool_input.get("operation"))
    if isinstance(found, str):
        return {"error": found}
    operation_name, candidate = found
    step = tool_input.get("step")
    if not isinstance(step, dict) or not isinstance(step.get("name"), str):
        return {"error": "put_step needs a 'step' object with a 'name'"}
    steps = _steps_of(candidate)
    index = next(
        (i for i, s in enumerate(steps) if s.get("name") == step["name"]), None
    )
    if index is None:
        steps.append(step)
    else:
        steps[index] = step
    problem = _operation_error(operation_name, candidate)
    if problem is not None:
        return {"error": problem}
    state.operations()[operation_name] = candidate
    return {"operation": operation_name, "step": step["name"]}


def remove_step(
    state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """Remove one named step from an existing operation. Rejects a removal that leaves
    the operation invalid (e.g. its only step, with no local_render label) - remove the
    whole operation instead."""
    found = _existing_operation(state, tool_input.get("operation"))
    if isinstance(found, str):
        return {"error": found}
    operation_name, candidate = found
    name = tool_input.get("name")
    if not isinstance(name, str):
        return {"error": "remove_step needs a 'name'"}
    steps = _steps_of(candidate)
    remaining = [s for s in steps if s.get("name") != name]
    if len(remaining) == len(steps):
        return {"error": f"no step '{name}' in operation '{operation_name}'"}
    candidate["steps"] = remaining
    problem = _operation_error(operation_name, candidate)
    if problem is not None:
        return {"error": problem}
    state.operations()[operation_name] = candidate
    return {"operation": operation_name, "removed": name}


def _step_in(
    candidate: dict[str, object], operation_name: str, step_name: object
) -> dict[str, object] | str:
    if not isinstance(step_name, str):
        return "a 'step' name is required"
    step = next((s for s in _steps_of(candidate) if s.get("name") == step_name), None)
    if step is None:
        return f"no step '{step_name}' in operation '{operation_name}'"
    return step


def put_mapping_entry(
    state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """Add or replace one mapping entry (keyed by its target field) in a step's
    request, keeping every other entry untouched - the granular edit for tweaking one
    field of a large request. Rejects an invalid result without changing anything."""
    found = _existing_operation(state, tool_input.get("operation"))
    if isinstance(found, str):
        return {"error": found}
    operation_name, candidate = found
    step = _step_in(candidate, operation_name, tool_input.get("step"))
    if isinstance(step, str):
        return {"error": step}
    entry = tool_input.get("entry")
    if not isinstance(entry, dict) or not isinstance(entry.get("target"), str):
        return {"error": "put_mapping_entry needs an 'entry' object with a 'target'"}
    request = step.setdefault("request", {})
    if not isinstance(request, dict):
        return {"error": f"step '{step.get('name')}' has no request object"}
    mapping = request.setdefault("mapping", [])
    if not isinstance(mapping, list):
        return {"error": f"step '{step.get('name')}' has no mapping list"}
    index = next(
        (
            i
            for i, e in enumerate(mapping)
            if isinstance(e, dict) and e.get("target") == entry["target"]
        ),
        None,
    )
    if index is None:
        mapping.append(entry)
    else:
        mapping[index] = entry
    problem = _operation_error(operation_name, candidate)
    if problem is not None:
        return {"error": problem}
    state.operations()[operation_name] = candidate
    return {"operation": operation_name, "target": entry["target"]}


def remove_mapping_entry(
    state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """Remove one mapping entry (by its target field) from a step's request."""
    found = _existing_operation(state, tool_input.get("operation"))
    if isinstance(found, str):
        return {"error": found}
    operation_name, candidate = found
    step = _step_in(candidate, operation_name, tool_input.get("step"))
    if isinstance(step, str):
        return {"error": step}
    target = tool_input.get("target")
    if not isinstance(target, str):
        return {"error": "remove_mapping_entry needs a 'target'"}
    request = step.get("request")
    mapping = request.get("mapping") if isinstance(request, dict) else None
    if not isinstance(mapping, list):
        return {"error": f"step '{step.get('name')}' has no mapping list"}
    remaining = [
        e for e in mapping if not (isinstance(e, dict) and e.get("target") == target)
    ]
    if len(remaining) == len(mapping):
        return {"error": f"no mapping entry targeting '{target}'"}
    assert isinstance(request, dict)  # mapping came from it above
    request["mapping"] = remaining
    problem = _operation_error(operation_name, candidate)
    if problem is not None:
        return {"error": problem}
    state.operations()[operation_name] = candidate
    return {"operation": operation_name, "removed": target}


def remove_operation(
    state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """Remove a named operation from the working copy."""
    name = tool_input.get("name")
    if not isinstance(name, str):
        return {"error": "remove_operation needs a 'name'"}
    if name not in state.operations():
        return {"error": f"no operation '{name}'"}
    del state.operations()[name]
    return {"removed": name}


def check(state: WorkingDefinition, tool_input: dict[str, object]) -> dict[str, object]:
    """Validate the working copy as a whole CarrierDefinition and report what's still
    missing or wrong, so the model knows when it's complete and publishable - and what
    to ask the operator or defer to the engineer."""
    try:
        CarrierDefinition.model_validate(state.data)
    except ValidationError as error:
        return {"valid": False, "errors": str(error)}
    return {"valid": True}


def raise_blocker_tool(
    session: Session, state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """Park a technical gap for the engineer (ADR 0018): something the definition
    vocabulary can't express (kind needs_plugin, naming the plugin to build) or a
    question the docs don't answer (kind needs_decision). State what is needed and
    what you already tried; then keep building everything else. The blocker is
    durable - the engineer resolves it, possibly days later."""
    carrier = state.data.get("carrier")
    if not isinstance(carrier, str) or not carrier.strip():
        return {"error": "set the carrier identity before raising a blocker"}
    kind = tool_input.get("kind")
    title = tool_input.get("title")
    detail = tool_input.get("detail")
    plugin_name = tool_input.get("plugin_name")
    if (
        not isinstance(kind, str)
        or not isinstance(title, str)
        or not isinstance(detail, str)
    ):
        return {"error": "raise_blocker needs 'kind', 'title' and 'detail' strings"}
    if plugin_name is not None and not isinstance(plugin_name, str):
        return {"error": "plugin_name must be a string when given"}
    try:
        # The domain enforces the rest (known kind, needs_plugin names its plugin,
        # column-length bounds) where the row is built.
        blocker = raise_blocker(session, carrier, kind, title, detail, plugin_name)
    except ValueError as error:
        return {"error": str(error)}
    return {"blocker_id": blocker.id, "status": "open"}


def list_blockers_tool(
    session: Session, state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """This carrier's blockers, open and resolved. A resolved blocker carries the
    engineer's answer - apply it to the working copy (e.g. set the now-shipped plugin)
    and tell the operator what moved forward."""
    carrier = state.data.get("carrier")
    if not isinstance(carrier, str) or not carrier.strip():
        return {"blockers": []}
    return {
        "blockers": [
            {
                "id": blocker.id,
                "kind": blocker.kind,
                "title": blocker.title,
                "detail": blocker.detail,
                "plugin_name": blocker.plugin_name,
                "status": blocker.status,
                "resolution": blocker.resolution,
            }
            for blocker in blockers_for(session, carrier)
        ]
    }


def _schema(
    name: str, description: str, properties: dict[str, object]
) -> dict[str, object]:
    return {"name": name, "description": description, "input_schema": properties}


# The nested auth/operation shapes are validated in Python (against the real models),
# so the tool schemas keep those as free objects the prompt describes rather than
# restating the whole definition schema here.
TOOL_SCHEMAS: list[dict[str, object]] = [
    _schema(
        "set_identity",
        set_identity.__doc__ or "",
        {
            "type": "object",
            "properties": {"carrier": {"type": "string"}, "name": {"type": "string"}},
            "required": ["carrier", "name"],
        },
    ),
    _schema(
        "set_auth",
        set_auth.__doc__ or "",
        {
            "type": "object",
            "properties": {"auth": {"type": "object"}},
            "required": ["auth"],
        },
    ),
    _schema(
        "put_operation",
        put_operation.__doc__ or "",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "operation": {"type": "object"},
            },
            "required": ["name", "operation"],
        },
    ),
    _schema(
        "remove_operation",
        remove_operation.__doc__ or "",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    ),
    _schema(
        "put_step",
        put_step.__doc__ or "",
        {
            "type": "object",
            "properties": {
                "operation": {"type": "string"},
                "step": {"type": "object"},
            },
            "required": ["operation", "step"],
        },
    ),
    _schema(
        "remove_step",
        remove_step.__doc__ or "",
        {
            "type": "object",
            "properties": {
                "operation": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["operation", "name"],
        },
    ),
    _schema(
        "put_mapping_entry",
        put_mapping_entry.__doc__ or "",
        {
            "type": "object",
            "properties": {
                "operation": {"type": "string"},
                "step": {"type": "string"},
                "entry": {"type": "object"},
            },
            "required": ["operation", "step", "entry"],
        },
    ),
    _schema(
        "remove_mapping_entry",
        remove_mapping_entry.__doc__ or "",
        {
            "type": "object",
            "properties": {
                "operation": {"type": "string"},
                "step": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["operation", "step", "target"],
        },
    ),
    _schema("check", check.__doc__ or "", {"type": "object", "properties": {}}),
    _schema(
        "raise_blocker",
        raise_blocker_tool.__doc__ or "",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["needs_plugin", "needs_decision"]},
                "title": {"type": "string"},
                "detail": {"type": "string"},
                "plugin_name": {"type": "string"},
            },
            "required": ["kind", "title", "detail"],
        },
    ),
    _schema(
        "list_blockers",
        list_blockers_tool.__doc__ or "",
        {"type": "object", "properties": {}},
    ),
]


def run_carrier_builder_tool(
    session: Session, state: WorkingDefinition, name: str, tool_input: dict[str, object]
) -> dict[str, object]:
    """Dispatch a model tool call against the working copy. An unknown tool returns an
    error rather than raising, so the loop hands it back to the model."""
    if name == "raise_blocker":
        return raise_blocker_tool(session, state, tool_input)
    if name == "list_blockers":
        return list_blockers_tool(session, state, tool_input)
    if name == "set_identity":
        return set_identity(state, tool_input)
    if name == "set_auth":
        return set_auth(state, tool_input)
    if name == "put_step":
        return put_step(state, tool_input)
    if name == "remove_step":
        return remove_step(state, tool_input)
    if name == "put_mapping_entry":
        return put_mapping_entry(state, tool_input)
    if name == "remove_mapping_entry":
        return remove_mapping_entry(state, tool_input)
    if name == "put_operation":
        return put_operation(state, tool_input)
    if name == "remove_operation":
        return remove_operation(state, tool_input)
    if name == "check":
        return check(state, tool_input)
    return {"error": f"unknown tool '{name}'"}
