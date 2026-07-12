"""The engine's pure renderer: (definition, operation, facts) -> rendered
requests, deterministically and with no I/O. Golden Replay (ADR 0009) diffs
these renders; execution through transports is a separate, later layer.

Unresolvable step outputs (facts the carrier has not answered yet) render
as stable placeholder tokens so multi-step operations still replay
deterministically offline."""

from typing import Literal

from pydantic import BaseModel

from nimbleship.domain.carrier_definition import (
    CarrierDefinition,
    MappingEntry,
    Step,
    Transform,
)

type Facts = dict[str, object]
type Rendered = str | list[object] | dict[str, object] | None


class RenderedRequest(BaseModel):
    step: str
    transport: str
    method: Literal["GET", "POST", "PUT"]
    url: str
    query: dict[str, str]
    content_type: str
    body: dict[str, Rendered]


def _resolve(path: str, facts: Facts) -> object:
    node: object = facts
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            if path.startswith("steps."):
                return f"<{path}>"
            raise ValueError(f"no fact at '{path}'")
    return node


def _apply(transform: Transform, value: object) -> object:
    match transform.name:
        case "join":
            if not isinstance(value, list):
                raise ValueError("join expects a list")
            return transform.with_.join(str(item) for item in value)
        case "uppercase":
            return str(value).upper()
        case "lowercase":
            return str(value).lower()
        case "split":
            return [part for part in str(value).split(transform.on) if part]
        case "lookup":
            key = str(value).lower() if isinstance(value, bool) else str(value)
            if key not in transform.table:
                raise ValueError(f"lookup has no entry for '{key}'")
            return transform.table[key]


def _render_entry(entry: MappingEntry, facts: Facts) -> Rendered:
    if entry.const is not None:
        return entry.const
    assert entry.source is not None  # schema guarantees exactly one
    value = _resolve(entry.source, facts)
    if entry.each is not None:
        # An unresolved step output stays a stable placeholder token so
        # multi-step operations replay offline (refuter, PR #26 - the
        # PalletForce label loop is the motivating case).
        if isinstance(value, str) and value == f"<{entry.source}>":
            return value
        if not isinstance(value, list):
            raise ValueError(f"'{entry.source}' is not a collection")
        return [
            {
                inner.target: _render_entry(inner, {**facts, "item": item})
                for inner in entry.each
            }
            for item in value
        ]
    if entry.transform is not None:
        value = _apply(entry.transform, value)
    if isinstance(value, str | list | dict) or value is None:
        return value
    return str(value)


def _render_step(
    definition: CarrierDefinition, step: Step, facts: Facts
) -> RenderedRequest:
    url = _resolve(step.request.url, facts)
    query = dict(step.request.query)
    auth = definition.auth
    if auth.scheme == "query_key":
        query[auth.param] = str(_resolve(auth.secret, facts))
    body = {entry.target: _render_entry(entry, facts) for entry in step.request.mapping}
    return RenderedRequest(
        step=step.name,
        transport=step.transport,
        method=step.request.method,
        url=str(url),
        query=query,
        content_type=step.request.content_type,
        body=body,
    )


def render_operation(
    definition: CarrierDefinition, operation: str, facts: Facts
) -> list[RenderedRequest]:
    if operation not in definition.operations:
        raise ValueError(f"definition has no operation '{operation}'")
    return [
        _render_step(definition, step, facts)
        for step in definition.operations[operation].steps
    ]
