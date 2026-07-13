"""Test helper: render an operation whose steps are all http and narrow the
result to RenderedRequest. render_operation returns the RenderedRequest |
RenderedUpload union now that upload transports exist; http-carrier tests
that read .body/.url/.method use this to stay typed without repeating an
isinstance narrow in every test."""

from nimbleship.domain.carrier_definition import CarrierDefinition
from nimbleship.engine.render import Facts, RenderedRequest, render_operation


def http_renders(
    definition: CarrierDefinition, operation: str, facts: Facts
) -> list[RenderedRequest]:
    rendered = render_operation(definition, operation, facts)
    result: list[RenderedRequest] = []
    for step in rendered:
        assert isinstance(step, RenderedRequest)
        result.append(step)
    return result
