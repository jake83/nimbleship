"""The Carrier Definition schema (ADR 0009, CONTEXT.md: Carrier Definition).

A definition is data over closed vocabularies: mapping entries name their
source facts and transforms; the engine owns what those words mean. A
definition referencing an unknown fact root, transform, or step fails at
authoring time - never at booking time.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from nimbleship.engine.field_plugins import field_plugin_names

FACT_ROOTS = ("shipment", "warehouse", "config")
# A manifest declares many consignments at once: its operation renders from
# manifest.* facts (the consignment list under an each-loop), never from a
# single shipment.
MANIFEST_FACT_ROOTS = ("manifest", "warehouse", "config")


def operation_fact_roots(operation: str) -> tuple[str, ...]:
    return MANIFEST_FACT_ROOTS if operation == "manifest" else FACT_ROOTS


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


type Transform = Annotated[
    JoinTransform
    | UppercaseTransform
    | LowercaseTransform
    | SplitTransform
    | LookupTransform,
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


class RequestSpec(BaseModel):
    method: Literal["GET", "POST", "PUT"] = "POST"
    # A source path (usually config.*) resolved at render time.
    url: str
    query: dict[str, str] = {}
    content_type: Literal["form", "json", "csv"]
    mapping: list[MappingEntry]


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


class LabelSpec(BaseModel):
    source: Literal["local_render", "base64_pdf", "png_pages", "fetch_step"]
    template: str | None = None
    from_extract: str | None = None


class Operation(BaseModel):
    steps: list[Step] = []
    label: LabelSpec | None = None

    @model_validator(mode="after")
    def _requires_steps_or_local_render(self) -> "Operation":
        if not self.steps and (
            self.label is None or self.label.source != "local_render"
        ):
            raise ValueError(
                "an operation needs at least one step or a local_render label"
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
                    *(set(operation_fact_roots(op)) for op in self.operations)
                )
                if self.operations
                else set(FACT_ROOTS)
            )
            _validate_source(self.auth.secret, {}, False, "auth", tuple(sorted(common)))
        for op_name, operation in self.operations.items():
            roots = operation_fact_roots(op_name)
            earlier: dict[str, set[str]] = {}
            for step in operation.steps:
                where = f"{op_name}.{step.name}"
                _validate_source(step.request.url, earlier, False, where, roots)
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
