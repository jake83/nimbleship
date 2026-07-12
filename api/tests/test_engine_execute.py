"""The executor: rendered requests over real transports, responses parsed
per ResponseSpec, every step recorded. All transports here are
httpx.MockTransport - the tests never touch a network."""

import json
from collections.abc import Iterator

import httpx
import pytest

from nimbleship.domain.carrier_definition import CarrierDefinition
from nimbleship.engine.auth_plugins import AUTH_PLUGINS
from nimbleship.engine.execute import (
    TRAFFIC_BODY_LIMIT,
    CarrierCallError,
    StepRecord,
    execute_operation,
)
from nimbleship.engine.render import RenderedRequest

FORM_DEFINITION = CarrierDefinition.model_validate(
    {
        "carrier": "furdeco",
        "name": "Furdeco",
        "auth": {"scheme": "query_key", "param": "key", "secret": "config.api_key"},
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "query": {"action": "save"},
                            "content_type": "form",
                            "mapping": [
                                {
                                    "target": "OrderNumber",
                                    "source": "shipment.order_number",
                                },
                                {"target": "ServiceLevel", "const": "2 Man"},
                            ],
                        },
                        "response": {
                            "format": "xml",
                            "success_when": {"path": "/response/carrier_reference"},
                            "error_message": {"path": "/response/error"},
                            "extract": [
                                {
                                    "name": "tracking_reference",
                                    "path": "/response/carrier_reference",
                                },
                                {
                                    "name": "barcodes",
                                    "path": "/response/barcodes",
                                    "transform": {"name": "split", "on": ", "},
                                },
                            ],
                        },
                    }
                ],
            }
        },
    }
)

FACTS: dict[str, object] = {
    "shipment": {"order_number": "95000254580"},
    "config": {
        "api_key": "SECRET-KEY",
        "base_url": "https://api.furdeco.example/orders",
    },
}

XML_OK = (
    "<response>"
    "<carrier_reference>F12345678910</carrier_reference>"
    "<barcodes>001122334455667688, 123456789123456789</barcodes>"
    "</response>"
)


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_form_step_posts_encoded_body_with_query_auth_and_extracts_xml() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=XML_OK)

    with _client(httpx.MockTransport(handle)) as client:
        result = execute_operation(FORM_DEFINITION, "book", FACTS, client)

    [request] = seen
    assert request.method == "POST"
    assert request.url.host == "api.furdeco.example"
    assert request.url.params["action"] == "save"
    assert request.url.params["key"] == "SECRET-KEY"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert b"OrderNumber=95000254580" in request.content
    assert b"ServiceLevel=2+Man" in request.content
    assert result.outputs["tracking_reference"] == "F12345678910"
    assert result.outputs["barcodes"] == [
        "001122334455667688",
        "123456789123456789",
    ]


def test_every_step_is_recorded_with_status_and_body() -> None:
    records: list[StepRecord] = []

    with _client(
        httpx.MockTransport(lambda request: httpx.Response(200, text=XML_OK))
    ) as client:
        result = execute_operation(
            FORM_DEFINITION, "book", FACTS, client, record=records.append
        )

    [record] = records
    assert record.step == "save"
    assert record.success is True
    assert record.response_status == 200
    assert record.response_body == XML_OK
    assert record.request.body["OrderNumber"] == "95000254580"
    assert result.records == records


def test_recorded_response_bodies_are_truncated() -> None:
    huge = XML_OK + " " * (TRAFFIC_BODY_LIMIT * 2)

    with _client(
        httpx.MockTransport(lambda request: httpx.Response(200, text=huge))
    ) as client:
        result = execute_operation(FORM_DEFINITION, "book", FACTS, client)

    [record] = result.records
    assert len(record.response_body) == TRAFFIC_BODY_LIMIT


def test_missing_success_field_fails_with_the_carrier_error_message() -> None:
    error_xml = "<response><error>Postcode not covered</error></response>"
    records: list[StepRecord] = []

    with (
        _client(
            httpx.MockTransport(lambda request: httpx.Response(200, text=error_xml))
        ) as client,
        pytest.raises(CarrierCallError) as excinfo,
    ):
        execute_operation(FORM_DEFINITION, "book", FACTS, client, record=records.append)

    assert str(excinfo.value) == "Postcode not covered"
    # The failed step is still recorded - failures feed the golden corpus too.
    [record] = records
    assert record.success is False
    assert record.response_body == error_xml
    assert excinfo.value.records == records


def test_http_error_status_fails_loudly_and_is_recorded() -> None:
    records: list[StepRecord] = []

    with (
        _client(
            httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
        ) as client,
        pytest.raises(CarrierCallError, match=r"step 'save' failed with HTTP 500"),
    ):
        execute_operation(FORM_DEFINITION, "book", FACTS, client, record=records.append)

    [record] = records
    assert record.success is False
    assert record.response_status == 500
    assert record.response_body == "boom"


def test_network_failure_fails_loudly_with_a_status_less_record() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    records: list[StepRecord] = []

    with (
        _client(httpx.MockTransport(handle)) as client,
        pytest.raises(CarrierCallError, match=r"step 'save'.*connection refused"),
    ):
        execute_operation(FORM_DEFINITION, "book", FACTS, client, record=records.append)

    [record] = records
    assert record.success is False
    assert record.response_status is None


def test_unparseable_response_body_is_a_failure_not_a_crash() -> None:
    with (
        _client(
            httpx.MockTransport(
                lambda request: httpx.Response(200, text="not xml at all")
            )
        ) as client,
        pytest.raises(CarrierCallError, match=r"step 'save'.*xml"),
    ):
        execute_operation(FORM_DEFINITION, "book", FACTS, client)


def test_missing_extraction_path_names_the_path() -> None:
    no_barcodes = "<response><carrier_reference>F1</carrier_reference></response>"

    with (
        _client(
            httpx.MockTransport(lambda request: httpx.Response(200, text=no_barcodes))
        ) as client,
        pytest.raises(CarrierCallError, match=r"/response/barcodes"),
    ):
        execute_operation(FORM_DEFINITION, "book", FACTS, client)


JSON_DEFINITION = CarrierDefinition.model_validate(
    {
        "carrier": "palletforce",
        "name": "PalletForce",
        "auth": {"scheme": "none"},
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "manifest",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "order", "source": "shipment.order_number"}
                            ],
                        },
                        "response": {
                            "format": "json",
                            "success_when": {
                                "path": "result.status",
                                "equals": "OK",
                            },
                            "error_message": {"path": "result.message"},
                            "extract": [
                                {"name": "consignment_id", "path": "result.id"}
                            ],
                        },
                    },
                    {
                        "name": "fetch",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "id",
                                    "source": "steps.manifest.consignment_id",
                                }
                            ],
                        },
                        "response": {
                            "format": "json",
                            "extract": [{"name": "label_data", "path": "label"}],
                        },
                    },
                ],
            }
        },
    }
)


def test_json_steps_chain_extractions_into_later_steps() -> None:
    bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "order" in body:
            return httpx.Response(200, json={"result": {"status": "OK", "id": "C-77"}})
        return httpx.Response(200, json={"label": "UERG"})

    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"base_url": "https://api.pf.example"},
    }

    with _client(httpx.MockTransport(handle)) as client:
        result = execute_operation(JSON_DEFINITION, "book", facts, client)

    assert bodies == [{"order": "95000254580"}, {"id": "C-77"}]
    assert result.outputs["consignment_id"] == "C-77"
    assert result.outputs["label_data"] == "UERG"
    assert [record.step for record in result.records] == ["manifest", "fetch"]


def test_success_when_equals_mismatch_uses_the_declared_error_path() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"result": {"status": "REJECTED", "message": "No pallets left"}}
        )

    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"base_url": "https://api.pf.example"},
    }

    with (
        _client(httpx.MockTransport(handle)) as client,
        pytest.raises(CarrierCallError, match="No pallets left"),
    ):
        execute_operation(JSON_DEFINITION, "book", facts, client)


def test_non_http_transports_are_named_not_implemented() -> None:
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "fagans",
            "name": "Fagans",
            "auth": {"scheme": "none"},
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "upload",
                            "transport": "ftp_upload",
                            "request": {
                                "url": "config.ftp_path",
                                "content_type": "csv",
                                "mapping": [
                                    {
                                        "target": "order",
                                        "source": "shipment.order_number",
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"ftp_path": "/outbound"},
    }

    with (
        _client(httpx.MockTransport(lambda request: httpx.Response(200))) as client,
        pytest.raises(NotImplementedError, match="ftp_upload"),
    ):
        execute_operation(definition, "book", facts, client)


@pytest.fixture
def stamp_plugin() -> Iterator[None]:
    class StampPlugin:
        def apply(
            self, request: RenderedRequest, config: dict[str, object]
        ) -> RenderedRequest:
            return request.model_copy(
                update={
                    "headers": {
                        **request.headers,
                        "X-Stamp": str(config["stamp"]),
                    }
                }
            )

    AUTH_PLUGINS["stamp"] = StampPlugin()
    yield
    del AUTH_PLUGINS["stamp"]


def _plugin_definition() -> CarrierDefinition:
    return CarrierDefinition.model_validate(
        {
            "carrier": "fedex",
            "name": "FedEx",
            "auth": {"scheme": "plugin", "plugin": "stamp"},
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "ship",
                            "transport": "http",
                            "request": {
                                "method": "POST",
                                "url": "config.base_url",
                                "content_type": "json",
                                "mapping": [
                                    {
                                        "target": "order",
                                        "source": "shipment.order_number",
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        }
    )


def test_plugin_auth_is_applied_to_each_http_request(stamp_plugin: None) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"base_url": "https://api.fedex.example", "stamp": "S-9"},
    }

    with _client(httpx.MockTransport(handle)) as client:
        execute_operation(_plugin_definition(), "book", facts, client)

    [request] = seen
    assert request.headers["X-Stamp"] == "S-9"


def test_unregistered_auth_plugin_fails_loudly() -> None:
    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"base_url": "https://api.fedex.example", "stamp": "S-9"},
    }

    with (
        _client(httpx.MockTransport(lambda request: httpx.Response(200))) as client,
        pytest.raises(ValueError, match=r"auth plugin 'stamp' is not registered"),
    ):
        execute_operation(_plugin_definition(), "book", facts, client)


def test_form_step_with_a_collection_field_is_rejected() -> None:
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "x",
            "name": "X",
            "auth": {"scheme": "none"},
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "save",
                            "transport": "http",
                            "request": {
                                "url": "config.base_url",
                                "content_type": "form",
                                "mapping": [
                                    {
                                        "target": "items",
                                        "source": "shipment.parcels",
                                        "each": [
                                            {
                                                "target": "w",
                                                "source": "item.weight_kg",
                                            }
                                        ],
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"parcels": [{"weight_kg": "4.2"}]},
        "config": {"base_url": "https://api.x.example"},
    }

    with (
        _client(httpx.MockTransport(lambda request: httpx.Response(200))) as client,
        pytest.raises(ValueError, match=r"step 'save'.*'items'"),
    ):
        execute_operation(definition, "book", facts, client)
