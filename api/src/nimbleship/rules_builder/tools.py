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


def add_service(state: WorkingCopy, tool_input: dict[str, object]) -> dict[str, object]:
    """Add a new service to the working copy. Rejects an invalid service or a
    duplicate code without changing anything, so the model can correct and retry."""
    service = tool_input.get("service")
    if not isinstance(service, dict):
        return {"error": "add_service needs a 'service' object"}
    try:
        declaration = ServiceDeclaration.model_validate(service)
    except ValidationError as error:
        return {"error": f"invalid service: {error}"}
    if state.find(declaration.code) is not None:
        return {"error": f"a service with code '{declaration.code}' already exists"}
    state.services.append(declaration)
    return {"added": declaration.code, "service_count": len(state.services)}


def update_service(
    state: WorkingCopy, tool_input: dict[str, object]
) -> dict[str, object]:
    """Change fields on an existing service, keeping the rest. Rejects an unknown
    code or an edit that makes the service invalid, without changing anything."""
    code = tool_input.get("code")
    changes = tool_input.get("changes")
    if not isinstance(code, str) or not isinstance(changes, dict):
        return {"error": "update_service needs a 'code' and a 'changes' object"}
    current = state.find(code)
    if current is None:
        return {"error": f"no service with code '{code}'"}
    merged = {**current.model_dump(mode="json"), **changes}
    try:
        updated = ServiceDeclaration.model_validate(merged)
    except ValidationError as error:
        return {"error": f"invalid change: {error}"}
    state.services = [updated if s.code == code else s for s in state.services]
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
