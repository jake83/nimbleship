"""The rules builder's tools (ADR 0017): granular edits to an in-memory working
copy of the rulebook, and a dry-run of it against historical orders. Unlike the
assistant's read tools, these mutate the working copy - but only in memory; nothing
is saved. Untouched services are never re-typed, so an edit can't perturb them."""

from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.orm import Session

from nimbleship.domain.allocation import Rulebook, ServiceDeclaration
from nimbleship.domain.dry_run import dry_run_rulebook

_DRY_RUN_SAMPLE = 10


@dataclass
class WorkingCopy:
    """The rulebook draft being co-authored, seeded from the live version. Mutable;
    saved as a draft only when the operator commits, through the existing rails."""

    services: list[ServiceDeclaration] = field(default_factory=list)

    def find(self, code: str) -> ServiceDeclaration | None:
        return next((s for s in self.services if s.code == code), None)


# The flat service fields the builder authors (ADR 0017). Banded cost/charge tables
# are out by nature - pricing, not routing - so they're not offered here.
_SERVICE_PROPERTIES: dict[str, object] = {
    "code": {"type": "string"},
    "carrier": {"type": "string"},
    "name": {"type": "string"},
    "weight_min_kg": {"type": "string", "description": "Decimal, e.g. '0'"},
    "weight_max_kg": {"type": "string", "description": "Decimal, e.g. '30'"},
    "cost": {"type": "string", "description": "Flat Delivery Cost, e.g. '4.50'"},
    "countries": {"type": "array", "items": {"type": "string"}},
    "tie_break_order": {"type": "integer"},
    "max_dimension_cm": {"type": ["string", "null"]},
    "max_girth_cm": {"type": ["string", "null"]},
    "areas_served": {
        "type": ["array", "null"],
        "items": {"type": "string"},
        "description": "null = anywhere in the allowed countries",
    },
    "areas_blocked": {"type": "array", "items": {"type": "string"}},
    "propositions": {"type": "array", "items": {"type": "string"}},
    "service_groups": {"type": "array", "items": {"type": "string"}},
}

_REQUIRED_FIELDS = [
    "code",
    "carrier",
    "name",
    "weight_min_kg",
    "weight_max_kg",
    "cost",
    "countries",
    "tie_break_order",
]


# Banded pricing is out of the builder's scope by nature (ADR 0017): it's a rate
# card managed elsewhere, influencing routing only through the cheapest tie-break.
# It's absent from the tool schema, but a model could still put it in a raw tool
# input, so the scope is enforced here too - not left to the model's goodwill.
_PRICING_FIELDS = ("cost_bands", "charge_bands")


def _authors_pricing(fields: dict[str, object]) -> str | None:
    """The reason model-authored `fields` reach outside the builder's scope, or None.
    Only the model's own input is checked - a service seeded from the live rulebook
    keeps its existing bands untouched, which is preservation, not authoring. Any
    mention of a band field is out of scope, including a null that would clear an
    existing band (a pricing change with routing impact the builder must not make)."""
    named = [field for field in _PRICING_FIELDS if field in fields]
    if named:
        return (
            f"{' and '.join(named)}: banded pricing is managed elsewhere; the builder "
            "sets only the flat cost"
        )
    return None


def working_copy_error(services: list[ServiceDeclaration]) -> str | None:
    """The reason `services` can't be a working copy, or None: a duplicate code or
    tie-break. These are the same cross-service invariants Rulebook enforces (a
    standalone ServiceDeclaration can't), but an empty copy is allowed here - a
    legal mid-edit state, where a saved rulebook (Rulebook's min_length) is not. The
    single gate for both the client-supplied seed and every edit's result, so a
    corrupt copy can never take effect and only fail later, at save."""
    seen_codes: set[str] = set()
    seen_orders: set[int] = set()
    for service in services:
        if service.code in seen_codes:
            return f"duplicate service code: {service.code}"
        if service.tie_break_order in seen_orders:
            return f"duplicate tie-break order: {service.tie_break_order}"
        seen_codes.add(service.code)
        seen_orders.add(service.tie_break_order)
    return None


def add_service(state: WorkingCopy, tool_input: dict[str, object]) -> dict[str, object]:
    """Add a new service to the working copy. Rejects an invalid service, or one
    whose code or tie-break clashes with an existing service, without changing
    anything, so the model can correct and retry."""
    service = tool_input.get("service")
    if not isinstance(service, dict):
        return {"error": "add_service needs a 'service' object"}
    pricing = _authors_pricing(service)
    if pricing is not None:
        return {"error": pricing}
    try:
        declaration = ServiceDeclaration.model_validate(service)
    except ValidationError as error:
        return {"error": f"invalid service: {error}"}
    candidate = [*state.services, declaration]
    clash = working_copy_error(candidate)
    if clash is not None:
        return {"error": clash}
    state.services = candidate
    return {"added": declaration.code, "service_count": len(state.services)}


def update_service(
    state: WorkingCopy, tool_input: dict[str, object]
) -> dict[str, object]:
    """Change fields on an existing service, keeping the rest. Rejects an unknown
    code, an edit that makes the service invalid, or a rename/tie-break change that
    would clash with another service - without changing anything."""
    code = tool_input.get("code")
    changes = tool_input.get("changes")
    if not isinstance(code, str) or not isinstance(changes, dict):
        return {"error": "update_service needs a 'code' and a 'changes' object"}
    pricing = _authors_pricing(changes)
    if pricing is not None:
        return {"error": pricing}
    current = state.find(code)
    if current is None:
        return {"error": f"no service with code '{code}'"}
    merged = {**current.model_dump(mode="json"), **changes}
    try:
        updated = ServiceDeclaration.model_validate(merged)
    except ValidationError as error:
        return {"error": f"invalid change: {error}"}
    candidate = [updated if s.code == code else s for s in state.services]
    clash = working_copy_error(candidate)
    if clash is not None:
        return {"error": clash}
    state.services = candidate
    return {"updated": updated.code}


def remove_service(
    state: WorkingCopy, tool_input: dict[str, object]
) -> dict[str, object]:
    """Remove a service from the working copy by code."""
    code = tool_input.get("code")
    if not isinstance(code, str):
        return {"error": "remove_service needs a 'code'"}
    if state.find(code) is None:
        return {"error": f"no service with code '{code}'"}
    state.services = [s for s in state.services if s.code != code]
    return {"removed": code, "service_count": len(state.services)}


def dry_run(
    session: Session, state: WorkingCopy, tool_input: dict[str, object]
) -> dict[str, object]:
    """Replay the working copy against recent orders and report the impact - how
    many reroute, with a sample - so the model reasons about a change before saving.
    Rejects an invalid working copy (e.g. duplicate tie-break) rather than crashing."""
    try:
        rulebook = Rulebook(version=0, services=state.services)
    except ValidationError as error:
        return {"error": f"the working copy is not a valid rulebook: {error}"}
    report = dry_run_rulebook(session, rulebook)
    changed = [r for r in report.results if r.changed]
    return {
        "orders_considered": report.total,
        "orders_changed": report.changed,
        "sample_changes": [
            {
                "order_number": r.order_number,
                "from": r.current_service,
                "to": r.draft_service,
            }
            for r in changed[:_DRY_RUN_SAMPLE]
        ],
    }


def _service_schema(
    name: str, description: str, extra: dict[str, object]
) -> dict[str, object]:
    return {"name": name, "description": description, "input_schema": extra}


TOOL_SCHEMAS: list[dict[str, object]] = [
    _service_schema(
        "add_service",
        add_service.__doc__ or "",
        {
            "type": "object",
            "properties": {
                "service": {
                    "type": "object",
                    "properties": _SERVICE_PROPERTIES,
                    "required": _REQUIRED_FIELDS,
                }
            },
            "required": ["service"],
        },
    ),
    _service_schema(
        "update_service",
        update_service.__doc__ or "",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "changes": {"type": "object", "properties": _SERVICE_PROPERTIES},
            },
            "required": ["code", "changes"],
        },
    ),
    _service_schema(
        "remove_service",
        remove_service.__doc__ or "",
        {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    ),
    _service_schema(
        "dry_run",
        dry_run.__doc__ or "",
        {"type": "object", "properties": {}},
    ),
]


def run_builder_tool(
    session: Session, state: WorkingCopy, name: str, tool_input: dict[str, object]
) -> dict[str, object]:
    """Dispatch a model tool call against the working copy. An unknown tool returns
    an error rather than raising, so the loop hands it back to the model."""
    if name == "add_service":
        return add_service(state, tool_input)
    if name == "update_service":
        return update_service(state, tool_input)
    if name == "remove_service":
        return remove_service(state, tool_input)
    if name == "dry_run":
        return dry_run(session, state, tool_input)
    return {"error": f"unknown tool '{name}'"}
