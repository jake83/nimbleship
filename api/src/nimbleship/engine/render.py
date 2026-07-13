"""The engine's pure renderer: (definition, operation, facts) -> rendered
requests, deterministically and with no I/O. Golden Replay (ADR 0009) diffs
these renders; execution through transports is a separate, later layer.

Unresolvable step outputs (facts the carrier has not answered yet) render
as stable placeholder tokens so multi-step operations still replay
deterministically offline."""

import csv
import io
import re
from typing import Literal, cast

from pydantic import BaseModel

from nimbleship.domain.carrier_definition import (
    UPLOAD_TRANSPORTS,
    CarrierDefinition,
    MappingEntry,
    RequestSpec,
    Step,
    Transform,
    insert_at_target,
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


class RenderedUpload(BaseModel):
    """A file rendered for an upload transport: the body is a flat file
    (`content`), dropped at `remote_path/filename`. Connection secrets are
    not here - the uploader reads them from config at execution, keeping the
    Golden Replay corpus secret-free (as http auth already is)."""

    step: str
    transport: str
    content_type: str
    remote_path: str
    filename: str
    content: str


type RenderedStep = RenderedRequest | RenderedUpload


def _resolve(path: str, facts: Facts) -> object:
    node: object = facts
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            if path.startswith("steps."):
                return UnresolvedStepOutput(f"<{path}>")
            raise ValueError(f"no fact at '{path}'")
    return node


def apply_transform(transform: Transform, value: object) -> object:
    """The closed transform vocabulary (ADR 0009). Shared with response
    extraction: an Extraction's transform means exactly what a mapping's
    does."""
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
        case "format":
            # The template is validated to hold exactly one '{}', so a plain
            # replace substitutes the value without str.format's brace rules.
            return transform.template.replace("{}", str(value))


def _render_mapping(entries: list[MappingEntry], facts: Facts) -> dict[str, Rendered]:
    """One canonical nesting implementation lives in the schema module
    (insert_at_target) so authoring-time target validation and rendering
    can never drift apart."""
    body: dict[str, object] = {}
    for entry in entries:
        insert_at_target(body, entry.target, _render_entry(entry, facts))
    # insert_at_target only stores Rendered values and containers of them.
    return cast("dict[str, Rendered]", body)


def _render_entry(entry: MappingEntry, facts: Facts) -> Rendered:
    if entry.const is not None:
        return entry.const
    if entry.plugin is not None:
        # Plugins compute from the facts alone - the render stays pure.
        # Stateful inputs (allocated numbers) are injected as facts before
        # render (see nimbleship.engine.field_plugins).
        value = field_plugin(entry.plugin).compute(facts)
        if isinstance(value, str | list | dict) or value is None:
            return value
        return str(value)
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
        value = apply_transform(entry.transform, value)
    if isinstance(value, str | list | dict) or value is None:
        return value
    return str(value)


def _render_filename(template: str, facts: Facts) -> str:
    """Substitute `{fact.path}` placeholders in a filename template. Pure
    fact substitution - no expressions - so a filename stays data, readable
    and validatable, not a template language (ADR 0009)."""

    def _substitute(match: "re.Match[str]") -> str:
        return str(_resolve(match.group(1), facts))

    return re.sub(r"\{([^{}]+)\}", _substitute, template)


def _render_csv(entries: list[MappingEntry], facts: Facts) -> str:
    """One RFC 4180 row (comma-delimited, CRLF, minimal quoting - what the
    carriers' legacy CSV writers produced) from the mapping entries in order.
    Targets name columns for readability; the row is positional."""
    row: list[str] = []
    for entry in entries:
        value = _render_entry(entry, facts)
        if isinstance(value, list | dict):
            raise ValueError(
                f"csv field '{entry.target}' rendered a {type(value).__name__}, "
                "not a scalar"
            )
        row.append("" if value is None else str(value))
    buffer = io.StringIO()
    csv.writer(buffer).writerow(row)
    return buffer.getvalue()


def _render_content(request: RequestSpec, facts: Facts) -> str:
    if request.content_type == "csv":
        return _render_csv(request.mapping, facts)
    raise ValueError(
        f"upload content_type '{request.content_type}' has no file rendering yet"
    )


def _render_upload(step: Step, facts: Facts) -> RenderedUpload:
    remote_path = str(_resolve(step.request.url, facts))
    # The schema requires a filename for upload steps.
    assert step.request.filename is not None
    return RenderedUpload(
        step=step.name,
        transport=step.transport,
        content_type=step.request.content_type,
        remote_path=remote_path,
        filename=_render_filename(step.request.filename, facts),
        content=_render_content(step.request, facts),
    )


def render_step(
    definition: CarrierDefinition, step: Step, facts: Facts
) -> RenderedStep:
    """Render one step. The executor renders step-by-step so each step sees
    the extractions of the steps before it; render_operation renders them
    all at once for offline replay."""
    if step.transport in UPLOAD_TRANSPORTS:
        return _render_upload(step, facts)
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
) -> list[RenderedStep]:
    if operation not in definition.operations:
        raise ValueError(f"definition has no operation '{operation}'")
    return [
        render_step(definition, step, facts)
        for step in definition.operations[operation].steps
    ]
