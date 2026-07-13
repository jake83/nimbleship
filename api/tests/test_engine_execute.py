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
    assert isinstance(record.request, RenderedRequest)
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


def test_an_upload_transport_without_a_backend_is_named_not_implemented() -> None:
    # sftp_upload renders like ftp_upload (a RenderedUpload) but has no
    # execution backend yet, so running it fails loudly and by name.
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "dachser",
            "name": "Dachser",
            "auth": {"scheme": "none"},
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "upload",
                            "transport": "sftp_upload",
                            "request": {
                                "url": "config.sftp_path",
                                "filename": "{shipment.order_number}.csv",
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
                    "label": {"source": "local_render"},
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"sftp_path": "/outbound"},
    }

    with (
        _client(httpx.MockTransport(lambda request: httpx.Response(200))) as client,
        pytest.raises(NotImplementedError, match="sftp_upload"),
    ):
        execute_operation(definition, "book", facts, client, uploader=_FakeUploader())


def test_a_local_render_step_transport_is_never_sent_to_the_wire() -> None:
    # local_render is a label source; a step declaring it renders like an
    # http request but must fail loudly rather than be HTTP-executed.
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "dropout",
            "name": "Drop Out",
            "auth": {"scheme": "none"},
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "render",
                            "transport": "local_render",
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
                    "label": {"source": "local_render"},
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"base_url": "https://api.example/x"},
    }
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    with (
        _client(httpx.MockTransport(handle)) as client,
        pytest.raises(NotImplementedError, match="local_render"),
    ):
        execute_operation(definition, "book", facts, client)
    # It never reached the carrier client.
    assert seen == []


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


def test_the_transmit_guard_refuses_placeholder_tokens() -> None:
    """Defence in depth behind the authoring validation: a rendered request
    still carrying an unresolved step output must never reach a carrier
    (refuter, PR #30)."""
    from nimbleship.engine.execute import assert_no_placeholders
    from nimbleship.engine.render import RenderedRequest, UnresolvedStepOutput

    request = RenderedRequest(
        step="label",
        transport="http",
        method="POST",
        url="https://api.example",
        query={},
        headers={},
        content_type="json",
        body={"codes": [{"code": UnresolvedStepOutput("<steps.manifest.tracking>")}]},
    )

    with pytest.raises(ValueError, match=r"steps\.manifest\.tracking"):
        assert_no_placeholders(request)


FTP_DEFINITION = CarrierDefinition.model_validate(
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
                            "url": "config.ftp_remote_dir",
                            "filename": "DMC{shipment.order_number}.csv",
                            "content_type": "csv",
                            "mapping": [
                                {"target": "order", "source": "shipment.order_number"},
                                {"target": "account", "source": "config.account_code"},
                            ],
                        },
                    }
                ],
                "label": {"source": "local_render", "template": "standard_a6"},
            }
        },
    }
)

FTP_FACTS: dict[str, object] = {
    "shipment": {"order_number": "95000254580"},
    "config": {
        "account_code": "LIM2",
        "ftp_remote_dir": "/outbound",
        "ftp_host": "ftp.fagans.example",
        "ftp_username": "nimbleship",
        "ftp_password": "SECRET-PW",
    },
}


class _FakeUploader:
    """Records what the executor hands the transport; never touches FTP."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], str, str, str]] = []
        self.fail_with: str | None = None

    def upload(
        self,
        config: dict[str, object],
        remote_path: str,
        filename: str,
        content: str,
    ) -> None:
        from nimbleship.ftp_client import UploadError

        self.calls.append((config, remote_path, filename, content))
        if self.fail_with is not None:
            raise UploadError(self.fail_with)


def test_ftp_upload_step_uploads_the_rendered_file_and_records_traffic() -> None:
    uploader = _FakeUploader()
    records: list[StepRecord] = []

    with _client(httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        result = execute_operation(
            FTP_DEFINITION, "book", FTP_FACTS, client, records.append, uploader
        )

    [(config, remote_path, filename, content)] = uploader.calls
    assert remote_path == "/outbound"
    assert filename == "DMC95000254580.csv"
    assert content == "95000254580,LIM2\r\n"
    # The uploader gets config to connect with; the host/creds live there.
    assert config["ftp_host"] == "ftp.fagans.example"
    # Fire-and-forget: nothing extracted, no tracking reference.
    assert result.outputs == {}
    # Recorded as carrier traffic with no HTTP status, marked successful.
    [record] = records
    assert record.step == "upload"
    assert record.success is True
    assert record.response_status is None


def test_ftp_upload_records_the_file_but_never_the_credentials() -> None:
    uploader = _FakeUploader()
    records: list[StepRecord] = []

    with _client(httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        execute_operation(
            FTP_DEFINITION, "book", FTP_FACTS, client, records.append, uploader
        )

    # The golden corpus records the rendered file, not the connection secret.
    dumped = str(records[0].request.model_dump())
    assert "SECRET-PW" not in dumped
    assert "ftp_host" not in dumped


def test_a_failed_ftp_upload_is_a_carrier_call_error_with_traffic_kept() -> None:
    uploader = _FakeUploader()
    uploader.fail_with = "530 Login incorrect"
    records: list[StepRecord] = []

    with (
        _client(httpx.MockTransport(lambda r: httpx.Response(500))) as client,
        pytest.raises(CarrierCallError, match="530 Login incorrect"),
    ):
        execute_operation(
            FTP_DEFINITION, "book", FTP_FACTS, client, records.append, uploader
        )

    [record] = records
    assert record.success is False
    assert record.response_status is None
    assert "530 Login incorrect" in record.response_body
