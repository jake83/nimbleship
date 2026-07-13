"""The executor: runs a Carrier Definition operation over real transports.

Each step is rendered (by the same pure renderer Golden Replay diffs),
sent, and its response parsed per the step's ResponseSpec; extractions
become facts under `steps.<name>.*` for later steps. Every attempted step
yields a StepRecord - success or failure - because the records are the
golden corpus ADR 0009 replays against.

Only the http transport executes today; the others raise
NotImplementedError naming the transport."""

import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Literal

import httpx
from pydantic import BaseModel

from nimbleship.domain.carrier_definition import (
    CarrierDefinition,
    ResponseSpec,
    Step,
)
from nimbleship.engine.auth_plugins import AUTH_PLUGINS
from nimbleship.engine.render import (
    Facts,
    RenderedRequest,
    UnresolvedStepOutput,
    apply_transform,
    render_step,
)

# Recorded response bodies are capped so one label-laden response cannot
# bloat the traffic table; parsing always sees the full body.
TRAFFIC_BODY_LIMIT = 64 * 1024

type _Format = Literal["json", "xml"]


class StepRecord(BaseModel):
    step: str
    request: RenderedRequest
    response_status: int | None
    response_body: str
    success: bool


class ExecutionResult(BaseModel):
    # Extractions from every step, merged in step order (later steps win).
    outputs: dict[str, object]
    records: list[StepRecord]


class CarrierCallError(Exception):
    """A carrier call that did not succeed: transport failure, error
    status, failed success condition, or an extraction the response cannot
    satisfy. Carries the records of every step attempted so far - the
    caller persists them; a failure is traffic too."""

    def __init__(self, message: str, records: list[StepRecord]) -> None:
        super().__init__(message)
        self.records = records


type Recorder = Callable[[StepRecord], None]

# A sentinel distinct from None: JSON null is a present value.
_MISSING = object()


def _json_path(document: object, path: str) -> object:
    node = document
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _xml_path(root: ET.Element, path: str) -> object:
    parts = [part for part in path.split("/") if part]
    if not parts or root.tag != parts[0]:
        return _MISSING
    node = root
    for part in parts[1:]:
        child = node.find(part)
        if child is None:
            return _MISSING
        node = child
    text = node.text
    return _MISSING if text is None or text.strip() == "" else text.strip()


def _parse(spec: ResponseSpec, body: str) -> tuple[object, _Format]:
    if spec.format == "json":
        return json.loads(body), "json"
    return ET.fromstring(body), "xml"


def _lookup(parsed: object, fmt: _Format, path: str) -> object:
    if fmt == "xml":
        assert isinstance(parsed, ET.Element)
        return _xml_path(parsed, path)
    return _json_path(parsed, path)


def _error_message(
    spec: ResponseSpec, parsed: object, fmt: _Format, fallback: str
) -> str:
    if spec.error_message is not None:
        found = _lookup(parsed, fmt, spec.error_message.path)
        if found is not _MISSING and str(found).strip():
            return str(found)
    return fallback


def _encode(request: RenderedRequest) -> dict[str, object]:
    """Build the httpx send kwargs for a rendered request's content type."""
    if request.content_type == "json":
        return {"json": request.body}
    if request.content_type == "form":
        fields: dict[str, str] = {}
        for target, value in request.body.items():
            if isinstance(value, list | dict):
                raise ValueError(
                    f"step '{request.step}': form encoding needs flat string "
                    f"fields, but '{target}' rendered a collection"
                )
            fields[target] = "" if value is None else value
        return {"data": fields}
    raise NotImplementedError(
        f"content_type '{request.content_type}' has no http encoding yet"
    )


def _apply_auth_plugin(
    definition: CarrierDefinition, request: RenderedRequest, facts: Facts
) -> RenderedRequest:
    if definition.auth.scheme != "plugin":
        return request
    plugin = AUTH_PLUGINS.get(definition.auth.plugin)
    if plugin is None:
        raise ValueError(f"auth plugin '{definition.auth.plugin}' is not registered")
    config = facts.get("config")
    return plugin.apply(request, config if isinstance(config, dict) else {})


def _failure_reason(
    step: Step, spec: ResponseSpec, parsed: object, fmt: _Format
) -> str | None:
    """The message a failed success condition earns, or None on success."""
    if spec.success_when is None:
        return None
    value = _lookup(parsed, fmt, spec.success_when.path)
    if value is _MISSING:
        return _error_message(
            spec,
            parsed,
            fmt,
            f"step '{step.name}' response has no value at '{spec.success_when.path}'",
        )
    expected = spec.success_when.equals
    if expected is not None and str(value) != expected:
        return _error_message(
            spec,
            parsed,
            fmt,
            f"step '{step.name}' reported '{value}', not '{expected}'",
        )
    return None


def assert_no_placeholders(request: RenderedRequest) -> None:
    """Defence in depth behind authoring validation: a request carrying an
    unresolved step-output token must never reach a carrier (refuter,
    PR #30). Placeholders exist for offline replay only."""

    def scan(value: object, where: str) -> None:
        if isinstance(value, UnresolvedStepOutput):
            raise ValueError(
                f"unresolved step output {value} at {where} must not be "
                "sent to a carrier"
            )
        if isinstance(value, dict):
            for key, inner in value.items():
                scan(inner, f"{where}.{key}")
        elif isinstance(value, list):
            for index, inner in enumerate(value):
                scan(inner, f"{where}[{index}]")

    scan(request.url, "url")
    scan(dict(request.query), "query")
    scan(dict(request.headers), "headers")
    scan(dict(request.body), "body")


def _extract(
    step: Step, spec: ResponseSpec, parsed: object, fmt: _Format
) -> dict[str, object]:
    extracted: dict[str, object] = {}
    for extraction in spec.extract:
        value = _lookup(parsed, fmt, extraction.path)
        if value is _MISSING:
            raise ValueError(
                f"step '{step.name}' response has no value at '{extraction.path}'"
            )
        if extraction.transform is not None:
            value = apply_transform(extraction.transform, value)
        extracted[extraction.name] = value
    return extracted


class _Execution:
    """One operation run: accumulates step records and step outputs."""

    def __init__(
        self,
        definition: CarrierDefinition,
        facts: Facts,
        client: httpx.Client,
        record: Recorder | None,
    ) -> None:
        self._definition = definition
        self._facts = facts
        self._client = client
        self._record = record
        self.records: list[StepRecord] = []
        self.step_outputs: dict[str, object] = {}
        self.outputs: dict[str, object] = {}

    def _recorded(
        self,
        step: Step,
        request: RenderedRequest,
        status: int | None,
        body: str,
        success: bool,
    ) -> None:
        step_record = StepRecord(
            step=step.name,
            request=request,
            response_status=status,
            response_body=body[:TRAFFIC_BODY_LIMIT],
            success=success,
        )
        self.records.append(step_record)
        if self._record is not None:
            self._record(step_record)

    def _fail(self, message: str) -> CarrierCallError:
        return CarrierCallError(message, self.records)

    def run_step(self, step: Step) -> None:
        if step.transport != "http":
            raise NotImplementedError(
                f"transport '{step.transport}' cannot execute yet; "
                "only http steps run today"
            )
        rendered = render_step(
            self._definition, step, {**self._facts, "steps": dict(self.step_outputs)}
        )
        rendered = _apply_auth_plugin(self._definition, rendered, self._facts)
        assert_no_placeholders(rendered)
        try:
            response = self._client.request(
                rendered.method,
                rendered.url,
                params=rendered.query,
                headers=rendered.headers,
                **_encode(rendered),  # type: ignore[arg-type]
            )
        except httpx.HTTPError as error:
            self._recorded(step, rendered, None, "", False)
            raise self._fail(
                f"step '{step.name}' could not reach the carrier: {error}"
            ) from error
        body = response.text
        spec = step.response

        if response.status_code >= 400:
            self._recorded(step, rendered, response.status_code, body, False)
            message = f"step '{step.name}' failed with HTTP {response.status_code}"
            if spec is not None:
                try:
                    parsed, fmt = _parse(spec, body)
                except (json.JSONDecodeError, ET.ParseError):
                    pass  # the status alone is the failure; the body is noise
                else:
                    message = _error_message(spec, parsed, fmt, message)
            raise self._fail(message)

        extracted: dict[str, object] = {}
        if spec is not None:
            try:
                parsed, fmt = _parse(spec, body)
            except (json.JSONDecodeError, ET.ParseError) as error:
                self._recorded(step, rendered, response.status_code, body, False)
                raise self._fail(
                    f"step '{step.name}' returned unparseable {spec.format}: {error}"
                ) from error
            reason = _failure_reason(step, spec, parsed, fmt)
            if reason is not None:
                self._recorded(step, rendered, response.status_code, body, False)
                raise self._fail(reason)
            try:
                extracted = _extract(step, spec, parsed, fmt)
            except ValueError as error:
                self._recorded(step, rendered, response.status_code, body, False)
                raise self._fail(str(error)) from error

        self._recorded(step, rendered, response.status_code, body, True)
        self.step_outputs[step.name] = extracted
        self.outputs.update(extracted)


def execute_operation(
    definition: CarrierDefinition,
    operation: str,
    facts: Facts,
    client: httpx.Client,
    record: Recorder | None = None,
) -> ExecutionResult:
    if operation not in definition.operations:
        raise ValueError(f"definition has no operation '{operation}'")
    execution = _Execution(definition, facts, client, record)
    for step in definition.operations[operation].steps:
        execution.run_step(step)
    return ExecutionResult(outputs=execution.outputs, records=execution.records)
