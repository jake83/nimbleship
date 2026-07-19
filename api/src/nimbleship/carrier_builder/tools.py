"""The carrier builder's tools (ADR 0018): granular edits to an in-memory working
copy of a CarrierDefinition being assembled from an onboarding packet. Like the rules
builder, these mutate the working copy only in memory; nothing is saved. The copy is a
partial definition dict, validated as a whole CarrierDefinition only at check/save -
mid-build it is legitimately incomplete."""

from dataclasses import dataclass, field

from pydantic import BaseModel, TypeAdapter, ValidationError

from nimbleship.domain.carrier_definition import (
    Auth,
    CarrierDefinition,
    MappingEntry,
    Operation,
    RequestSpec,
    Step,
)

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


def _unknown_field(data: dict[str, object], model: type[BaseModel]) -> str | None:
    allowed = set(model.model_fields)
    allowed |= {f.alias for f in model.model_fields.values() if f.alias is not None}
    return next((key for key in data if key not in allowed), None)


def _first_misspelt_field(operation: dict[str, object]) -> str | None:
    """The first operation/step/request/mapping key no model field claims, or None.
    pydantic ignores an unknown key, so a mistyped optional field (`alloacte` for
    `allocate`, `fanout` for `fan_out`) would silently drop what the operator asked for
    while check() still reports the definition valid - reject it, as the rules builder
    does for its own edits."""
    unknown = _unknown_field(operation, Operation)
    if unknown is not None:
        return unknown
    steps = operation.get("steps")
    if not isinstance(steps, list):
        return None
    for step in steps:
        if not isinstance(step, dict):
            continue
        unknown = _unknown_field(step, Step)
        if unknown is not None:
            return unknown
        request = step.get("request")
        if not isinstance(request, dict):
            continue
        unknown = _unknown_field(request, RequestSpec)
        if unknown is not None:
            return unknown
        mapping = request.get("mapping")
        if isinstance(mapping, list):
            for entry in mapping:
                if isinstance(entry, dict):
                    unknown = _unknown_field(entry, MappingEntry)
                    if unknown is not None:
                        return unknown
    return None


def put_operation(
    state: WorkingDefinition, tool_input: dict[str, object]
) -> dict[str, object]:
    """Add or replace one named operation (book, manifest, ...) with its steps. Rejects
    a malformed operation without changing anything. Cross-operation rules (e.g. fan_out
    only on a manifest) are checked by `check`, which validates the whole definition."""
    name = tool_input.get("name")
    operation = tool_input.get("operation")
    if not isinstance(name, str) or not isinstance(operation, dict):
        return {"error": "put_operation needs a 'name' and an 'operation' object"}
    misspelt = _first_misspelt_field(operation)
    if misspelt is not None:
        return {"error": f"unknown field '{misspelt}' - check the spelling"}
    try:
        Operation.model_validate(operation)
    except ValidationError as error:
        return {"error": f"invalid operation '{name}': {error}"}
    state.operations()[name] = operation
    return {"operation": name}


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
    _schema("check", check.__doc__ or "", {"type": "object", "properties": {}}),
]


def run_carrier_builder_tool(
    state: WorkingDefinition, name: str, tool_input: dict[str, object]
) -> dict[str, object]:
    """Dispatch a model tool call against the working copy. An unknown tool returns an
    error rather than raising, so the loop hands it back to the model."""
    if name == "set_identity":
        return set_identity(state, tool_input)
    if name == "set_auth":
        return set_auth(state, tool_input)
    if name == "put_operation":
        return put_operation(state, tool_input)
    if name == "remove_operation":
        return remove_operation(state, tool_input)
    if name == "check":
        return check(state, tool_input)
    return {"error": f"unknown tool '{name}'"}
