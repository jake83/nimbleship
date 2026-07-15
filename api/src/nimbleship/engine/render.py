"""The engine's pure renderer: (definition, operation, facts) -> rendered
requests, deterministically and with no I/O. Golden Replay (ADR 0009) diffs
these renders; execution through transports is a separate, later layer.

Unresolvable step outputs (facts the carrier has not answered yet) render
as stable placeholder tokens so multi-step operations still replay
deterministically offline."""

import csv
import io
import re
import xml.etree.ElementTree as ET
from typing import Literal, cast

from pydantic import BaseModel

from nimbleship.domain.carrier_definition import (
    FILENAME_PLACEHOLDER,
    UPLOAD_TRANSPORTS,
    CarrierDefinition,
    MappingEntry,
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


def _resolve(path: str, facts: Facts, for_execution: bool = False) -> object:
    node: object = facts
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            # A prior step's output that is not yet answered renders as a
            # placeholder for offline replay - but during live execution a
            # step reference must resolve, so an unresolved one raises here,
            # at the source, rather than travel to a carrier as literal token
            # text (pydantic coerces the placeholder subclass back to plain
            # str, so a post-render type check cannot catch it).
            if path.startswith("steps.") and not for_execution:
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


def _render_mapping(
    entries: list[MappingEntry], facts: Facts, for_execution: bool
) -> dict[str, Rendered]:
    """One canonical nesting implementation lives in the schema module
    (insert_at_target) so authoring-time target validation and rendering
    can never drift apart."""
    body: dict[str, object] = {}
    for entry in entries:
        insert_at_target(body, entry.target, _render_entry(entry, facts, for_execution))
    # insert_at_target only stores Rendered values and containers of them.
    return cast("dict[str, Rendered]", body)


def _render_entry(entry: MappingEntry, facts: Facts, for_execution: bool) -> Rendered:
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
    value = _resolve(entry.source, facts, for_execution)
    # An unresolved step output (offline replay only) stays a stable
    # placeholder token - through each-loops AND transforms alike - so
    # multi-step operations replay deterministically (refuter, PR #26).
    if isinstance(value, UnresolvedStepOutput):
        return str(value)
    if entry.each is not None:
        if not isinstance(value, list):
            raise ValueError(f"'{entry.source}' is not a collection")
        return [
            _render_mapping(entry.each, {**facts, "item": item}, for_execution)
            for item in value
        ]
    if entry.pluck is not None:
        # each yields a list of objects; pluck reads one item-relative source
        # from each element into a list of scalars (a JSON string array). The
        # path is item-rooted like an each inner entry (e.g. item.carrier_barcode).
        if not isinstance(value, list):
            raise ValueError(f"'{entry.source}' is not a collection")
        plucked: list[object] = []
        for item in value:
            scalar = _resolve(entry.pluck, {**facts, "item": item}, for_execution)
            # pluck's contract is a scalar per item; a path landing on a
            # compound value would ship a nested list silently.
            if isinstance(scalar, list | dict):
                raise ValueError(
                    f"pluck '{entry.pluck}' resolved a {type(scalar).__name__}, "
                    "not a scalar"
                )
            plucked.append(scalar)
        return plucked
    if entry.transform is not None:
        value = apply_transform(entry.transform, value)
    if isinstance(value, str | list | dict) or value is None:
        return value
    return str(value)


def _render_filename(template: str, facts: Facts, for_execution: bool) -> str:
    """Substitute `{fact.path}` placeholders in a filename template. Pure
    fact substitution - no expressions - so a filename stays data, readable
    and validatable, not a template language (ADR 0009). The placeholder
    grammar is owned by the schema so authoring validation and rendering
    cannot diverge."""

    def _substitute(match: "re.Match[str]") -> str:
        return str(_resolve(match.group(1), facts, for_execution))

    return FILENAME_PLACEHOLDER.sub(_substitute, template)


# A leading one of these makes a spreadsheet run a CSV field as a formula or
# DDE command on open (CSV injection, OWASP).
_CSV_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def _neutralise_csv_formula(value: str) -> str:
    """Prefix a quote to a formula-leading field so a spreadsheet shows it as
    text. The trade: a legitimate leading +/- value (a phone, a negative) is
    quoted too, accepted to close the vector."""
    return "'" + value if value.startswith(_CSV_FORMULA_LEADERS) else value


def _render_csv(entries: list[MappingEntry], facts: Facts, for_execution: bool) -> str:
    """One RFC 4180 row: comma-delimited, minimal quoting, CRLF-terminated -
    the format the receiving carriers require. Rendered from the mapping
    entries in order; targets name columns for readability, the row is
    positional."""
    row: list[str] = []
    for entry in entries:
        value = _render_entry(entry, facts, for_execution)
        if isinstance(value, list | dict):
            raise ValueError(
                f"csv field '{entry.target}' rendered a {type(value).__name__}, "
                "not a scalar"
            )
        row.append("" if value is None else _neutralise_csv_formula(str(value)))
    buffer = io.StringIO()
    csv.writer(buffer).writerow(row)
    return buffer.getvalue()


XML_PROLOG = '<?xml version="1.0" encoding="UTF-8"?>'

# Code points outside XML 1.0's `Char` production, which no document may
# contain even escaped: the C0 controls except tab/LF/CR, the surrogate range
# (a lone surrogate cannot even be UTF-8 encoded, so it would crash the
# uploader), and the U+FFFE/U+FFFF noncharacters (these encode cleanly and
# would otherwise ship as a broken EDI file). A rendered value carrying one -
# e.g. a stray character in a free-text shipment field, or mis-decoded input
# crossing the legacy edge - is refused rather than shipped non-well-formed.
_ILLEGAL_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _xml_value(value: object, where: str) -> str:
    text = "" if value is None else str(value)
    if _ILLEGAL_XML_CHARS.search(text):
        raise ValueError(f"{where} contains a character not permitted in XML")
    return text


def _append_xml(parent: ET.Element, name: str, value: object) -> None:
    """Attach `value` to `parent` as one or more `<name>` child elements: a
    list becomes repeated same-name siblings, a dict a nested element, and a
    scalar an element with text."""
    if isinstance(value, list):
        for item in value:
            _append_xml(parent, name, item)
    elif isinstance(value, dict):
        child = ET.SubElement(parent, name)
        _build_xml(child, value)
    else:
        child = ET.SubElement(parent, name)
        if value is not None:
            child.text = _xml_value(value, f"element '{name}'")


def _build_xml(parent: ET.Element, mapping: dict[str, Rendered]) -> None:
    """Populate `parent` from a rendered mapping dict: an `@`-prefixed key is
    an attribute of `parent` (and must be scalar), any other key is a child
    element. ElementTree escapes text and attribute values, so a rendered
    value can never inject markup."""
    for key, value in mapping.items():
        if key.startswith("@"):
            if isinstance(value, list | dict):
                raise ValueError(
                    f"xml attribute '{key}' rendered a {type(value).__name__}, "
                    "not a scalar"
                )
            parent.set(key[1:], _xml_value(value, f"attribute '{key}'"))
        else:
            _append_xml(parent, key, value)


def _render_xml(
    root_element: str, entries: list[MappingEntry], facts: Facts, for_execution: bool
) -> str:
    """Render the mapping as one XML document: a fixed UTF-8 prolog and the
    mapping wrapped in `root_element`. Reuses the same nesting/each machinery
    as every other render (insert_at_target), so xml is data, not a template
    language (ADR 0009/0010)."""
    root = ET.Element(root_element)
    _build_xml(root, _render_mapping(entries, facts, for_execution))
    return f"{XML_PROLOG}\n{ET.tostring(root, encoding='unicode')}"


def _render_upload(step: Step, facts: Facts, for_execution: bool) -> RenderedUpload:
    remote_path = str(_resolve(step.request.url, facts, for_execution))
    # The schema requires a filename and a csv or xml content_type for uploads.
    assert step.request.filename is not None
    if step.request.content_type == "xml":
        assert step.request.root_element is not None
        content = _render_xml(
            step.request.root_element, step.request.mapping, facts, for_execution
        )
    else:
        content = _render_csv(step.request.mapping, facts, for_execution)
    return RenderedUpload(
        step=step.name,
        transport=step.transport,
        content_type=step.request.content_type,
        remote_path=remote_path,
        filename=_render_filename(step.request.filename, facts, for_execution),
        content=content,
    )


def render_step(
    definition: CarrierDefinition,
    step: Step,
    facts: Facts,
    for_execution: bool = False,
) -> RenderedStep:
    """Render one step. The executor renders step-by-step so each step sees
    the extractions of the steps before it; render_operation renders them
    all at once for offline replay.

    `for_execution` is set by the executor: a step-output reference that is
    still unresolved then is a real failure (the carrier would receive token
    text), so it raises rather than rendering a replay placeholder."""
    if step.transport in UPLOAD_TRANSPORTS:
        return _render_upload(step, facts, for_execution)
    url = _resolve(step.request.url, facts, for_execution)
    query = dict(step.request.query)
    headers: dict[str, str] = {}
    auth = definition.auth
    # Auth belongs to the wire protocol: only http steps carry it. Anything
    # broader would embed secrets in renders (and the Golden Replay corpus)
    # for steps that never transmit them (refuter, PR #26 round 3).
    if step.transport == "http":
        if auth.scheme == "query_key":
            query[auth.param] = str(_resolve(auth.secret, facts, for_execution))
        elif auth.scheme == "header_key":
            headers[auth.header] = str(_resolve(auth.secret, facts, for_execution))
    body = _render_mapping(step.request.mapping, facts, for_execution)
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
