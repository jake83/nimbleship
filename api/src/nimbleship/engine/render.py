"""The engine's pure renderer: (definition, operation, facts) -> rendered
requests, deterministically and with no I/O. Golden Replay (ADR 0009) diffs
these renders; execution through transports is a separate, later layer.

Unresolvable step outputs (facts the carrier has not answered yet) render
as stable placeholder tokens so multi-step operations still replay
deterministically offline."""

from typing import Literal, cast

from pydantic import BaseModel

from nimbleship.domain.carrier_definition import (
    CarrierDefinition,
    MappingEntry,
    Step,
    Transform,
)


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


# A dotted target nests: `accountNumber.value` builds an object member,
# a numeric segment (`recipients.0.city`) a list position. List positions
# must arrive in order - an index past the next free slot is a definition
# mistake and fails loudly rather than padding silently.


def _place(
    node: dict[str, object] | list[object], parts: list[str], value: object, target: str
) -> None:
    head, rest = parts[0], parts[1:]
    if isinstance(node, list):
        if not head.isdigit():
            raise ValueError(f"mapping '{target}': '{head}' indexes into a list")
        index = int(head)
        if index > len(node):
            raise ValueError(f"mapping '{target}': index {index} skips a position")
        if not rest:
            if index < len(node):
                raise ValueError(f"mapping '{target}': already mapped")
            node.append(value)
            return
        if index == len(node):
            node.append([] if rest[0].isdigit() else {})
        child = node[index]
        if not isinstance(child, dict | list):
            raise ValueError(f"mapping '{target}': collides with an earlier mapping")
        _place(child, rest, value, target)
        return
    if not rest:
        if head in node:
            raise ValueError(f"mapping '{target}': already mapped")
        node[head] = value
        return
    if head not in node:
        node[head] = [] if rest[0].isdigit() else {}
    child = node[head]
    if not isinstance(child, dict | list):
        raise ValueError(f"mapping '{target}': collides with an earlier mapping")
    _place(child, rest, value, target)


def _render_mapping(entries: list[MappingEntry], facts: Facts) -> dict[str, Rendered]:
    body: dict[str, object] = {}
    for entry in entries:
        _place(body, entry.target.split("."), _render_entry(entry, facts), entry.target)
    # _place only ever stores Rendered values and containers of them; the
    # cast spares every nesting level an invariant-dict re-shuffle.
    return cast("dict[str, Rendered]", body)


def _render_entry(entry: MappingEntry, facts: Facts) -> Rendered:
    if entry.const is not None:
        return entry.const
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
        return [_render_mapping(entry.each, {**facts, "item": item}) for item in value]
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
    body = _render_mapping(step.request.mapping, facts)
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
