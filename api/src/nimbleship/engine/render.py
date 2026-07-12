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
from nimbleship.engine.field_plugins import field_plugin


class UnresolvedStepOutput(str):
    """A placeholder for a step output the carrier has not answered yet.
    A distinct type, not a magic string: real data shaped like a token
    must never be mistaken for one (refuter, PR #26 round 3)."""

    __slots__ = ()


type Facts = dict[str, object]
type Rendered = str | list[object] | dict[str, object] | None


class RenderedRequest(BaseModel):
    step: str
    transport: str
    method: Literal["GET", "POST", "PUT"]
    url: str
    query: dict[str, str]
    headers: dict[str, str] = {}
    content_type: str
    body: dict[str, Rendered]


def _resolve(path: str, facts: Facts) -> object:
    node: object = facts
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            if path.startswith("steps."):
                return UnresolvedStepOutput(f"<{path}>")
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


def _coerce(value: object) -> Rendered:
    if isinstance(value, str | list | dict) or value is None:
        return value
    return str(value)


def _render_entry(entry: MappingEntry, facts: Facts) -> Rendered:
    if entry.const is not None:
        return entry.const
    if entry.plugin is not None:
        # Plugins compute from the facts alone - the render stays pure.
        # Stateful inputs (allocated numbers, tokens) are injected as facts
        # before render (see nimbleship.engine.field_plugins).
        return _coerce(field_plugin(entry.plugin).compute(facts))
    assert entry.source is not None  # schema guarantees exactly one
    value = _resolve(entry.source, facts)
    # An unresolved step output stays a stable placeholder token - through
    # each-loops AND transforms alike - so multi-step operations replay
    # offline deterministically (refuter, PR #26, both rounds).
    if isinstance(value, UnresolvedStepOutput):
        return str(value)
    if entry.each is not None:
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
    return _coerce(value)


def _render_step(
    definition: CarrierDefinition, step: Step, facts: Facts
) -> RenderedRequest:
    url = _resolve(step.request.url, facts)
    query = dict(step.request.query)
    headers: dict[str, str] = {}
    auth = definition.auth
    # Auth belongs to the wire protocol: only http steps carry it. Anything
    # broader would embed secrets in renders (and the Golden Replay corpus)
    # for steps that never transmit them (refuter, PR #26 round 3).
    if step.transport == "http":
        if auth.scheme == "query_key":
            query[auth.param] = str(_resolve(auth.secret, facts))
        elif auth.scheme == "header_key":
            headers[auth.header] = str(_resolve(auth.secret, facts))
    body = {entry.target: _render_entry(entry, facts) for entry in step.request.mapping}
    return RenderedRequest(
        step=step.name,
        transport=step.transport,
        method=step.request.method,
        url=str(url),
        query=query,
        headers=headers,
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
