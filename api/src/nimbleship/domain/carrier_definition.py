"""The Carrier Definition schema (ADR 0009, CONTEXT.md: Carrier Definition).

A definition is data over closed vocabularies: mapping entries name their
source facts and transforms; the engine owns what those words mean. A
definition referencing an unknown fact root, transform, or step fails at
authoring time - never at booking time.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

FACT_ROOTS = ("shipment", "warehouse", "config")


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


class MappingEntry(BaseModel):
    target: str
    source: str | None = None
    const: str | None = None
    transform: Transform | None = None
    # Loop over a collection source; inner entries read from the `item.` root.
    each: list["MappingEntry"] | None = None

    @model_validator(mode="after")
    def _exactly_one_value_origin(self) -> "MappingEntry":
        if (self.source is None) == (self.const is None):
            raise ValueError(f"mapping '{self.target}': exactly one of source or const")
        if self.const is not None and (self.transform or self.each):
            raise ValueError(
                f"mapping '{self.target}': const takes no transform or each"
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


class ResponseSpec(BaseModel):
    format: Literal["json", "xml"]
    success_when: dict[str, str] | None = None
    error_message: dict[str, str] | None = None
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
    source: str, known_steps: set[str], in_each: bool, where: str
) -> None:
    root = source.split(".", 1)[0]
    if root in FACT_ROOTS:
        return
    if in_each and root == "item":
        return
    if root == "steps":
        parts = source.split(".")
        if len(parts) < 3 or parts[1] not in known_steps:
            raise ValueError(f"{where}: unknown step in source '{source}'")
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
        # authoring like any other unknown fact (refuter, PR #26).
        if isinstance(self.auth, QueryKeyAuth | HeaderKeyAuth):
            _validate_source(self.auth.secret, set(), False, "auth")
        for op_name, operation in self.operations.items():
            earlier: set[str] = set()
            for step in operation.steps:
                where = f"{op_name}.{step.name}"
                _validate_source(step.request.url, earlier, False, where)
                for entry in step.request.mapping:
                    self._validate_entry(entry, earlier, False, where)
                earlier.add(step.name)
        return self

    def _validate_entry(
        self,
        entry: MappingEntry,
        known_steps: set[str],
        in_each: bool,
        where: str,
    ) -> None:
        if entry.source is not None:
            _validate_source(entry.source, known_steps, in_each, where)
        for inner in entry.each or []:
            self._validate_entry(inner, known_steps, True, where)
