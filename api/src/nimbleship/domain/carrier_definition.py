"""The Carrier Definition schema (ADR 0009, CONTEXT.md: Carrier Definition).

A definition is data over closed vocabularies: mapping entries name their
source facts and transforms; the engine owns what those words mean. A
definition referencing an unknown fact root, transform, or step fails at
authoring time - never at booking time.
"""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from nimbleship.engine.field_plugins import field_plugin_names

FACT_ROOTS = ("shipment", "warehouse", "config")
# A manifest declares many consignments at once: its operation renders from
# manifest.* facts (the consignment list under an each-loop), never from a
# single shipment.
MANIFEST_FACT_ROOTS = ("manifest", "warehouse", "config")

# Transports whose step renders a file dropped on a server (a filename +
# content) rather than an HTTP request. They are fire-and-forget: no
# response comes back to extract from.
UPLOAD_TRANSPORTS = ("ftp_upload", "sftp_upload")

# Placeholder in a filename template, e.g. {shipment.order_number}.
FILENAME_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def operation_fact_roots(operation: str, fan_out: bool = False) -> tuple[str, ...]:
    # A fan-out manifest renders once per consignment from that consignment's
    # own shipment.* facts, so it uses the shipment roots, not the batch
    # manifest.* roots.
    if operation == "manifest" and not fan_out:
        return MANIFEST_FACT_ROOTS
    return FACT_ROOTS


class JoinTransform(BaseModel):
    name: Literal["join"]
    with_: str = Field(alias="with")


class UppercaseTransform(BaseModel):
    name: Literal["uppercase"]


class LowercaseTransform(BaseModel):
    name: Literal["lowercase"]


class SplitTransform(BaseModel):
    name: Literal["split"]
    on: str


class LookupTransform(BaseModel):
    name: Literal["lookup"]
    table: dict[str, str]


class FormatTransform(BaseModel):
    name: Literal["format"]
    # A template with exactly one `{}`, where the value is substituted -
    # e.g. "REF{}" prefixes an order number with a fixed reference code.
    template: str

    @model_validator(mode="after")
    def _single_placeholder(self) -> "FormatTransform":
        # Exactly one `{}` and no other brace: no placeholder would silently
        # drop the fact, more than one is ambiguous, and a stray brace would
        # survive the plain .replace into the rendered value.
        if not re.fullmatch(r"[^{}]*\{\}[^{}]*", self.template):
            raise ValueError("format template needs exactly one '{}' placeholder")
        return self


type Transform = Annotated[
    JoinTransform
    | UppercaseTransform
    | LowercaseTransform
    | SplitTransform
    | LookupTransform
    | FormatTransform,
    Field(discriminator="name"),
]


def insert_at_target(
    container: dict[str, object] | list[object], target: str, value: object
) -> None:
    """Place a value at a mapping target. Dots nest objects; an all-digit
    segment is a 0-based list position and must be used densely, in order.
    Structural conflicts (a target through another target's value, a list
    position skipped) fail loudly - and, because definitions dry-run their
    targets at authoring time, before a definition is ever stored."""
    _insert(container, target.split("."), value, target)


def _insert(
    container: dict[str, object] | list[object],
    parts: list[str],
    value: object,
    target: str,
) -> None:
    part, rest = parts[0], parts[1:]
    if isinstance(container, list) != part.isdigit():
        raise ValueError(f"mapping targets conflict at '{target}'")
    child: object
    if isinstance(container, list):
        index = int(part)
        if index > len(container):
            raise ValueError(
                f"mapping '{target}': list index {index} skips position "
                f"{len(container)}"
            )
        if not rest:
            if index < len(container):
                raise ValueError(f"mapping targets conflict at '{target}'")
            container.append(value)
            return
        if index == len(container):
            container.append([] if rest[0].isdigit() else {})
        child = container[index]
    else:
        if not rest:
            if part in container:
                raise ValueError(f"mapping targets conflict at '{target}'")
            container[part] = value
            return
        if part not in container:
            container[part] = [] if rest[0].isdigit() else {}
        child = container[part]
    if not isinstance(child, dict | list):
        raise ValueError(f"mapping targets conflict at '{target}'")
    _insert(child, rest, value, target)


class MappingEntry(BaseModel):
    target: str
    source: str | None = None
    const: str | None = None
    # A computed-field plugin name (ADR 0009): the engine calls the
    # registered plugin with the facts and maps its value to the target.
    plugin: str | None = None
    transform: Transform | None = None
    # Loop over a collection source; inner entries read from the `item.` root.
    each: list["MappingEntry"] | None = None

    @model_validator(mode="after")
    def _exactly_one_value_origin(self) -> "MappingEntry":
        origins = (self.source, self.const, self.plugin)
        if sum(origin is not None for origin in origins) != 1:
            raise ValueError(
                f"mapping '{self.target}': exactly one of source, const, or plugin"
            )
        if self.const is not None and (self.transform or self.each):
            raise ValueError(
                f"mapping '{self.target}': const takes no transform or each"
            )
        if self.plugin is not None and (self.transform or self.each):
            raise ValueError(
                f"mapping '{self.target}': plugin takes no transform or each"
            )
        if self.plugin is not None and self.plugin not in field_plugin_names():
            raise ValueError(
                f"mapping '{self.target}': unknown field plugin '{self.plugin}'"
            )
        if self.each is not None and self.transform is not None:
            raise ValueError(
                f"mapping '{self.target}': each and transform are exclusive"
            )
        return self


# A conservative XML 1.0 Name (no namespace colon - namespaces are out of
# scope): a letter or underscore, then letters, digits, underscore, hyphen,
# or period. Element and attribute names must match so a target can never
# render a document no XML parser can read.
_XML_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")


def _require_xml_name(name: str, where: str) -> None:
    if not _XML_NAME.fullmatch(name):
        raise ValueError(f"{where} is not a legal XML name: {name!r}")


def _validate_xml_targets(entries: list[MappingEntry]) -> None:
    """Enforce the @-attribute convention (ADR 0010): an @-prefixed segment
    names an attribute of its parent element, so it must be the terminal
    segment of its target and cannot be a repeated element (an each-loop
    produces sibling elements, never an attribute). Every element/attribute
    name is also checked to be a legal XML name (an all-digit segment is a
    list position, not a name). Nested each-loops are checked recursively so
    an attribute inside a repeated element is covered too."""
    for entry in entries:
        segments = entry.target.split(".")
        for segment in segments[:-1]:
            if segment.startswith("@"):
                raise ValueError(
                    f"mapping '{entry.target}': an @attribute must be the last "
                    "segment of its target"
                )
            if not segment.isdigit():
                _require_xml_name(segment, f"mapping '{entry.target}': element name")
        terminal = segments[-1]
        if terminal.startswith("@"):
            if terminal == "@":
                raise ValueError(
                    f"mapping '{entry.target}': an @attribute needs a name after the @"
                )
            if terminal[1:] == "xmlns":
                raise ValueError(
                    f"mapping '{entry.target}': xmlns declares a namespace, which is "
                    "out of scope (a prefixed xmlns:* is already rejected by the "
                    "no-colon rule)"
                )
            _require_xml_name(terminal[1:], f"mapping '{entry.target}': attribute name")
            if entry.each is not None:
                raise ValueError(
                    f"mapping '{entry.target}': an @attribute cannot be a repeated "
                    "element (each)"
                )
        elif not terminal.isdigit():
            _require_xml_name(terminal, f"mapping '{entry.target}': element name")
        if entry.each is not None:
            _validate_xml_targets(entry.each)


class RequestSpec(BaseModel):
    method: Literal["GET", "POST", "PUT"] = "POST"
    # A source path (usually config.*) resolved at render time. For an
    # upload transport this is the remote directory rather than a URL.
    url: str
    query: dict[str, str] = {}
    content_type: Literal["form", "json", "csv", "xml"]
    mapping: list[MappingEntry]
    # Upload transports only: the remote filename, a template whose
    # `{fact.path}` placeholders are substituted at render time (e.g.
    # "{warehouse.code}-{shipment.order_number}.csv").
    filename: str | None = None
    # content_type xml only: the single document wrapper element. The renderer
    # emits a fixed prolog and wraps the mapping in this element.
    root_element: str | None = None

    @model_validator(mode="after")
    def _xml_shape(self) -> "RequestSpec":
        if self.content_type == "xml":
            if not self.root_element:
                raise ValueError("an xml request needs a root_element")
            _require_xml_name(self.root_element, "root_element")
            _validate_xml_targets(self.mapping)
        elif self.root_element is not None:
            raise ValueError("root_element is only for content_type xml")
        return self


class Extraction(BaseModel):
    name: str
    path: str
    transform: Transform | None = None


class SuccessCondition(BaseModel):
    """Success means the value at `path` exists and is non-empty; with
    `equals`, it must also match exactly."""

    path: str
    equals: str | None = None


class ErrorMessageSource(BaseModel):
    """Where the carrier puts its human-readable error message."""

    path: str


class ResponseSpec(BaseModel):
    format: Literal["json", "xml"]
    success_when: SuccessCondition | None = None
    error_message: ErrorMessageSource | None = None
    extract: list[Extraction] = []


class Step(BaseModel):
    name: str
    transport: Literal["http", "ftp_upload", "sftp_upload", "local_render"]
    request: RequestSpec
    response: ResponseSpec | None = None

    @model_validator(mode="after")
    def _transport_shape(self) -> "Step":
        where = f"step '{self.name}'"
        if self.transport in UPLOAD_TRANSPORTS:
            # An upload renders a file to a named remote path and hears
            # nothing back, so it needs a filename, must be a file format the
            # engine renders (csv or xml), and declares no response.
            if self.request.filename is None:
                raise ValueError(f"{where}: an upload step needs a filename")
            if self.request.content_type not in ("csv", "xml"):
                raise ValueError(
                    f"{where}: an upload step must be content_type csv or xml"
                )
            if self.response is not None:
                raise ValueError(
                    f"{where}: an upload step is fire-and-forget and takes no response"
                )
            # The remote directory is a per-install account fact, not
            # shipment data: pinning it to config.* keeps an
            # attacker/customer-influenced value out of the upload path
            # (a shipment-sourced directory could carry `..` and escape).
            if not self.request.url.startswith("config."):
                raise ValueError(
                    f"{where}: an upload step's remote directory (url) must be a "
                    "config.* source"
                )
        else:
            if self.request.filename is not None:
                raise ValueError(
                    f"{where}: filename is for upload transports, not http"
                )
            # xml is a file format for upload steps only - there is no
            # http-xml request body until a carrier needs one.
            if self.request.content_type == "xml":
                raise ValueError(f"{where}: xml is an upload-only content_type")
        return self


class LabelSpec(BaseModel):
    source: Literal["local_render", "base64_pdf", "png_pages", "fetch_step"]
    template: str | None = None
    from_extract: str | None = None


class Operation(BaseModel):
    steps: list[Step] = []
    label: LabelSpec | None = None
    # Manifest operations only: emit one document per consignment (rendered
    # from each consignment's shipment.* facts) rather than one batch document
    # from manifest.* facts. CarrierDefinition rejects it on any other
    # operation.
    fan_out: bool = False

    @model_validator(mode="after")
    def _requires_steps_or_local_render(self) -> "Operation":
        if not self.steps and (
            self.label is None or self.label.source != "local_render"
        ):
            raise ValueError(
                "an operation needs at least one step or a local_render label"
            )
        return self

    @model_validator(mode="after")
    def _base64_pdf_label_resolves(self) -> "Operation":
        # A base64_pdf label is a base64 PDF the carrier returned, so it names
        # the step extraction that carries it; that name must resolve to a real
        # extraction of this operation, or the label would fail at booking.
        label = self.label
        if label is None or label.source != "base64_pdf":
            return self
        if label.from_extract is None:
            raise ValueError(
                "a base64_pdf label needs a from_extract naming a step extraction"
            )
        extraction_names = {
            extraction.name
            for step in self.steps
            if step.response is not None
            for extraction in step.response.extract
        }
        if label.from_extract not in extraction_names:
            raise ValueError(
                f"base64_pdf label from_extract '{label.from_extract}' is not an "
                "extraction of this operation"
            )
        return self


class QueryKeyAuth(BaseModel):
    scheme: Literal["query_key"]
    param: str
    secret: str  # a source path, usually config.*


class HeaderKeyAuth(BaseModel):
    scheme: Literal["header_key"]
    header: str
    secret: str


class NoAuth(BaseModel):
    scheme: Literal["none"]


class PluginAuth(BaseModel):
    scheme: Literal["plugin"]
    plugin: str


type Auth = Annotated[
    QueryKeyAuth | HeaderKeyAuth | NoAuth | PluginAuth,
    Field(discriminator="scheme"),
]


def _validate_source(
    source: str,
    known_steps: dict[str, set[str]],
    in_each: bool,
    where: str,
    roots: tuple[str, ...] = FACT_ROOTS,
) -> None:
    root = source.split(".", 1)[0]
    if root in roots:
        return
    if in_each and root == "item":
        return
    if root == "steps":
        parts = source.split(".")
        if len(parts) < 3 or parts[1] not in known_steps:
            raise ValueError(f"{where}: unknown step in source '{source}'")
        # The output name must be one of the referenced step's declared
        # extractions: a typo here would render a placeholder token and
        # send it to a live carrier (refuter, PR #30).
        if parts[2] not in known_steps[parts[1]]:
            raise ValueError(
                f"{where}: unknown output '{parts[2]}' of step "
                f"'{parts[1]}' in source '{source}'"
            )
        return
    raise ValueError(f"{where}: unknown source root '{root}'")


class CarrierDefinition(BaseModel):
    carrier: str = Field(max_length=64)
    name: str = Field(max_length=255)
    auth: Auth
    operations: dict[str, Operation]

    @model_validator(mode="after")
    def _fan_out_shape(self) -> "CarrierDefinition":
        for op_name, operation in self.operations.items():
            if not operation.fan_out:
                continue
            if op_name != "manifest":
                raise ValueError(
                    f"operation '{op_name}': fan_out is only for the manifest operation"
                )
            # Whole-manifest retry re-sends every document, so fan_out is
            # restricted to upload transports, whose overwrite-idempotence
            # makes re-sending an already-landed document safe; an http step
            # would double-submit an already-accepted order on retry.
            for step in operation.steps:
                if step.transport not in UPLOAD_TRANSPORTS:
                    raise ValueError(
                        f"{op_name}.{step.name}: a fan_out manifest must use an "
                        "upload transport, not "
                        f"'{step.transport}' (retry re-sends every document)"
                    )
        return self

    @model_validator(mode="after")
    def _sources_resolve(self) -> "CarrierDefinition":
        # The auth secret is a source path too: a typo there must fail at
        # authoring like any other unknown fact (refuter, PR #26). One auth
        # block serves every operation, so its secret must resolve in every
        # operation's fact context - validate it against the roots common to
        # all of them, not just a book operation's (a manifest operation has
        # no shipment facts).
        if isinstance(self.auth, QueryKeyAuth | HeaderKeyAuth):
            common = (
                set.intersection(
                    *(
                        set(operation_fact_roots(op_name, operation.fan_out))
                        for op_name, operation in self.operations.items()
                    )
                )
                if self.operations
                else set(FACT_ROOTS)
            )
            _validate_source(self.auth.secret, {}, False, "auth", tuple(sorted(common)))
        for op_name, operation in self.operations.items():
            roots = operation_fact_roots(op_name, operation.fan_out)
            earlier: dict[str, set[str]] = {}
            for step in operation.steps:
                where = f"{op_name}.{step.name}"
                _validate_source(step.request.url, earlier, False, where, roots)
                if step.request.filename is not None:
                    # A filename placeholder is a source too: a typo must
                    # fail at authoring, not name a phantom file at upload.
                    for match in FILENAME_PLACEHOLDER.finditer(step.request.filename):
                        _validate_source(
                            match.group(1),
                            earlier,
                            False,
                            f"{where}: filename",
                            roots,
                        )
                for entry in step.request.mapping:
                    self._validate_entry(entry, earlier, False, where, roots)
                self._validate_targets(step.request.mapping, where)
                earlier[step.name] = (
                    {extraction.name for extraction in step.response.extract}
                    if step.response is not None
                    else set()
                )
        return self

    def _validate_targets(self, entries: list[MappingEntry], where: str) -> None:
        # Dry-run the targets so structural conflicts fail at authoring
        # time, not when the first booking renders.
        probe: dict[str, object] = {}
        for entry in entries:
            try:
                insert_at_target(probe, entry.target, None)
            except ValueError as error:
                raise ValueError(f"{where}: {error}") from error
            if entry.each:
                self._validate_targets(entry.each, where)

    def _validate_entry(
        self,
        entry: MappingEntry,
        known_steps: dict[str, set[str]],
        in_each: bool,
        where: str,
        roots: tuple[str, ...],
    ) -> None:
        if entry.source is not None:
            _validate_source(entry.source, known_steps, in_each, where, roots)
        for inner in entry.each or []:
            self._validate_entry(inner, known_steps, True, where, roots)
