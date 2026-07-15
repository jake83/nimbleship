"""Carrier Definition storage rails: per-carrier versioned documents on the
draft/test/publish pattern (ADR 0003 via ADR 0009), with Golden Replay as
the test step - draft renders diffed against the active definition's."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nimbleship.models import CarrierDefinitionVersion, Consignment, Warehouse

TEST_CARRIER_DEFINITION = {
    "carrier": "testcarrier",
    "name": "Test Carrier",
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
                        "content_type": "json",
                        "mapping": [
                            {
                                "target": "order",
                                "source": "shipment.order_number",
                            },
                            {"target": "channel", "const": "nimbleship"},
                        ],
                    },
                }
            ],
        }
    },
}

CONSIGNMENT = {
    "order_number": "95000254580",
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}],
}


def _publish_v1_with_config(client: TestClient) -> None:
    client.put(
        "/api/carriers/testcarrier/config",
        json={"api_key": "K-1", "base_url": "https://api.test.example"},
    )
    client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    )
    client.post("/api/carriers/testcarrier/definitions/versions/1/publish")


def test_dropout_definition_seeds_and_is_active(client: TestClient) -> None:
    response = client.get("/api/carriers/dropout/definitions/active")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["definition"]["carrier"] == "dropout"
    assert body["definition"]["operations"]["book"]["label"]["source"] == (
        "local_render"
    )


def test_draft_publish_lifecycle_per_carrier(client: TestClient) -> None:
    # The definition references config.api_key and config.base_url, so publish
    # needs them present - the config-completeness gate refuses otherwise.
    client.put(
        "/api/carriers/testcarrier/config",
        json={"api_key": "K-1", "base_url": "https://api.test.example"},
    )
    created = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    )
    assert created.status_code == 201
    assert created.json() == {
        "carrier": "testcarrier",
        "version": 1,
        "status": "draft",
        "author": "jake",
    }

    published = client.post("/api/carriers/testcarrier/definitions/versions/1/publish")
    assert published.status_code == 200

    active = client.get("/api/carriers/testcarrier/definitions/active").json()
    assert active["version"] == 1

    versions = client.get("/api/carriers/testcarrier/definitions/versions").json()
    assert [(v["version"], v["status"]) for v in versions] == [(1, "published")]


def test_draft_carrier_must_match_the_url(client: TestClient) -> None:
    response = client.post(
        "/api/carriers/other/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    )

    assert response.status_code == 422


def test_invalid_definition_is_rejected_at_draft(client: TestClient) -> None:
    bad = {
        **TEST_CARRIER_DEFINITION,
        "auth": {"scheme": "query_key", "param": "key", "secret": "typo.api_key"},
    }

    response = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": bad},
    )

    assert response.status_code == 422
    assert "unknown source root" in response.text


def test_publishing_a_stale_draft_conflicts(client: TestClient) -> None:
    for _ in range(2):
        client.post(
            "/api/carriers/testcarrier/definitions/drafts",
            json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
        )
    client.post("/api/carriers/testcarrier/definitions/versions/2/publish")

    response = client.post("/api/carriers/testcarrier/definitions/versions/1/publish")

    assert response.status_code == 409


def test_golden_replay_diffs_draft_renders_against_active(
    client: TestClient,
) -> None:
    _publish_v1_with_config(client)
    client.post("/api/consignments", json=CONSIGNMENT)

    changed = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "order",
                                    "source": "shipment.order_number",
                                },
                                {"target": "channel", "const": "CHANGED"},
                            ],
                        },
                    }
                ],
            }
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": changed},
    ).json()

    replay = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/replay",
        json={},
    )

    assert replay.status_code == 200
    body = replay.json()
    assert body["total"] == 1
    assert body["changed"] == 1
    [result] = body["results"]
    assert result["order_number"] == "95000254580"
    assert result["changed"] is True
    assert result["differences"] == [
        {
            "step": "save",
            "field": "body.channel",
            "active": "nimbleship",
            "draft": "CHANGED",
        }
    ]


def test_golden_replay_of_an_identical_draft_reports_no_changes(
    client: TestClient,
) -> None:
    _publish_v1_with_config(client)
    client.post("/api/consignments", json=CONSIGNMENT)
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    ).json()

    replay = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/replay",
        json={},
    ).json()

    assert replay["changed"] == 0


def test_a_definition_without_a_book_operation_fails_loudly_at_dispatch(
    client: TestClient,
) -> None:
    trackonly = {
        "carrier": "trackonly",
        "name": "Track Only",
        "auth": {"scheme": "none"},
        "operations": {
            "track": {
                "steps": [
                    {
                        "name": "status",
                        "transport": "http",
                        "request": {
                            "method": "GET",
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
    client.put("/api/carriers/trackonly/config", json={"base_url": "https://x"})
    client.post(
        "/api/carriers/trackonly/definitions/drafts",
        json={"author": "jake", "definition": trackonly},
    )
    client.post("/api/carriers/trackonly/definitions/versions/1/publish")
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "TRACKONLY-STD",
                "carrier": "trackonly",
                "name": "Bookless",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 1,
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    client.post(f"/api/rulebook/versions/{version}/publish")

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 500
    assert "book" in response.text
    assert "trackonly" in response.text


def test_publish_refuses_a_draft_whose_renders_error(client: TestClient) -> None:
    """ADR 0009: a green replay (renders succeed - diffs are fine, errors
    are not) is required to publish. The gate runs inline at publish time
    against recent consignments."""
    _publish_v1_with_config(client)
    client.post("/api/consignments", json=CONSIGNMENT)

    broken = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            # shipment.nope is a valid root (passes draft), but no
                            # such fact exists: it fails at render, not authoring.
                            "mapping": [{"target": "order", "source": "shipment.nope"}],
                        },
                    }
                ],
            }
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": broken},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 409
    assert "render" in response.text.lower()
    assert "shipment.nope" in response.text


def test_publish_refuses_a_draft_whose_other_operations_cannot_render(
    client: TestClient,
) -> None:
    """The gate covers every declared operation, not just book: a broken
    track mapping must not publish behind a healthy book operation."""
    _publish_v1_with_config(client)
    client.post("/api/consignments", json=CONSIGNMENT)

    broken_track = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": TEST_CARRIER_DEFINITION["operations"]["book"],  # type: ignore[index]
            "track": {
                "steps": [
                    {
                        "name": "status",
                        "transport": "http",
                        "request": {
                            "method": "GET",
                            "url": "config.base_url",
                            "content_type": "json",
                            # shipment.nope renders no fact: a render error, not a
                            # config gap, so the render gate is what refuses here.
                            "mapping": [{"target": "order", "source": "shipment.nope"}],
                        },
                    }
                ],
            },
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": broken_track},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 409
    assert "'track'" in response.text
    assert "shipment.nope" in response.text


def test_publish_refuses_a_definition_with_an_incomplete_config(
    client: TestClient,
) -> None:
    # config.api_key and config.base_url are referenced; only api_key is set, so
    # the gate names the still-missing key and refuses the publish.
    client.put("/api/carriers/testcarrier/config", json={"api_key": "K-1"})
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 409
    assert "config incomplete" in response.text
    assert "base_url" in response.text
    assert "api_key" not in response.text  # the provided key is not reported


def test_publish_config_gate_lists_every_missing_key(client: TestClient) -> None:
    # No config at all: both referenced keys are reported in one refusal, not
    # one-at-a-time as the render gate would surface them.
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 409
    assert "api_key" in response.text
    assert "base_url" in response.text


def test_publish_config_gate_holds_without_any_consignment_history(
    client: TestClient,
) -> None:
    # The render gate passes trivially with nothing to render; the config gate
    # still refuses an incomplete config, so a fresh carrier cannot publish a
    # definition it has no credentials for.
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 409
    assert "config incomplete" in response.text


def test_publish_gate_treats_a_null_config_value_as_missing(
    client: TestClient,
) -> None:
    # A null value is present in the dict but renders as the literal "None", so
    # the gate must treat it as absent, not as a provided key.
    client.put(
        "/api/carriers/testcarrier/config",
        json={"api_key": "K-1", "base_url": None},
    )
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 409
    assert "config incomplete" in response.text
    assert "base_url" in response.text


def test_publish_gate_covers_a_plugin_auths_config_keys(client: TestClient) -> None:
    # A plugin auth reads token_url/client_id/client_secret straight from config;
    # they are not config.* sources, so the gate must still require them or an
    # OAuth carrier would publish and then fail every booking at token fetch.
    client.put(
        "/api/carriers/testcarrier/config",
        json={"ship_url": "https://ship.example"},
    )
    plugin_auth_def = {
        "carrier": "testcarrier",
        "name": "Test Carrier",
        "auth": {"scheme": "plugin", "plugin": "oauth_client_credentials"},
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.ship_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "order", "source": "shipment.order_number"}
                            ],
                        },
                    }
                ],
            }
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": plugin_auth_def},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 409
    assert "config incomplete" in response.text
    assert "token_url" in response.text
    assert "client_id" in response.text
    assert "client_secret" in response.text


def test_put_config_reports_keys_the_active_definition_still_needs(
    client: TestClient,
) -> None:
    # Saving never blocks (config may be built up incrementally), but the
    # response names the keys the live definition references and the payload
    # omits, as early feedback ahead of the publish gate.
    _publish_v1_with_config(client)

    response = client.put("/api/carriers/testcarrier/config", json={"api_key": "K-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "saved"
    assert body["missing"] == ["base_url"]


def test_put_config_reports_no_missing_keys_when_complete(client: TestClient) -> None:
    _publish_v1_with_config(client)

    response = client.put(
        "/api/carriers/testcarrier/config",
        json={"api_key": "K-1", "base_url": "https://api.test.example"},
    )

    assert response.json()["missing"] == []


def test_put_config_reports_nothing_missing_without_an_active_definition(
    client: TestClient,
) -> None:
    # Config can precede the definition (a fresh install is a deploy plus
    # configuration), so with nothing published there is no requirement to
    # measure the payload against.
    response = client.put("/api/carriers/newcarrier/config", json={"api_key": "K-1"})

    assert response.status_code == 200
    assert response.json()["missing"] == []


def test_patch_config_merges_keeping_keys_the_payload_omits(
    client: TestClient,
) -> None:
    # The live definition needs api_key and base_url. Rotating just api_key via
    # PATCH must keep base_url - a PUT would replace the row and drop it, which
    # the empty missing report here confirms did not happen.
    _publish_v1_with_config(client)

    response = client.patch("/api/carriers/testcarrier/config", json={"api_key": "K-2"})

    assert response.status_code == 200
    assert response.json()["missing"] == []


def test_patch_config_creates_the_config_when_none_exists(
    client: TestClient,
) -> None:
    response = client.patch("/api/carriers/newcarrier/config", json={"api_key": "K-1"})

    assert response.status_code == 200
    assert response.json() == {
        "carrier": "newcarrier",
        "status": "saved",
        "missing": [],
    }


def test_patch_config_reports_keys_still_missing_after_the_merge(
    client: TestClient,
) -> None:
    # A partial patch that does not complete the config still reports what the
    # active definition lacks, measured against the merged result.
    _publish_v1_with_config(client)
    client.put("/api/carriers/testcarrier/config", json={})

    response = client.patch("/api/carriers/testcarrier/config", json={"api_key": "K-1"})

    assert response.status_code == 200
    assert response.json()["missing"] == ["base_url"]


def _add_history(app: FastAPI, order_number: str, carrier: str) -> None:
    with app.state.session_factory() as session:
        session.add(
            Consignment(
                order_number=order_number,
                recipient_name="Jane Doe",
                address_lines=["1 High Street"],
                postcode="AB1 2CD",
                destination_country="GB",
                status="allocated",
                carrier=carrier,
                service="STD",
                allocation={},
            )
        )
        session.commit()


def _add_warehoused_history(
    app: FastAPI, order_number: str, carrier: str, warehouse_code: str
) -> None:
    with app.state.session_factory() as session:
        session.add(
            Warehouse(
                code=warehouse_code,
                name="Depot",
                address_lines=["1 Depot Way"],
                postcode="AB1 2CD",
                country="GB",
            )
        )
        session.add(
            Consignment(
                order_number=order_number,
                recipient_name="Jane Doe",
                address_lines=["1 High Street"],
                postcode="AB1 2CD",
                destination_country="GB",
                status="allocated",
                carrier=carrier,
                service="STD",
                warehouse=warehouse_code,
                allocation={},
            )
        )
        session.commit()


_FAN_OUT_MANIFEST = {
    "fan_out": True,
    "steps": [
        {
            "name": "drop",
            "transport": "sftp_upload",
            "request": {
                "url": "config.sftp_remote_dir",
                "filename": "{shipment.order_number}.xml",
                "content_type": "xml",
                "root_element": "Order",
                "mapping": [{"target": "Nope", "source": "shipment.nope"}],
            },
        }
    ],
}


_BATCH_MANIFEST = {
    "steps": [
        {
            "name": "declare",
            "transport": "http",
            "request": {
                "method": "POST",
                "url": "config.base_url",
                "content_type": "json",
                "mapping": [{"target": "count", "source": "manifest.nope"}],
            },
        }
    ],
}


def test_the_publish_gate_renders_a_batch_manifest(
    app: FastAPI, client: TestClient
) -> None:
    # A non-fan-out manifest renders once from a synthesized manifest of the
    # recent consignments, so the gate covers it too: a broken manifest.* source
    # - valid at draft (roots only) - is caught here, not first at trailer-close.
    _publish_v1_with_config(client)
    _add_history(app, "95000254580", "testcarrier")

    broken = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": TEST_CARRIER_DEFINITION["operations"]["book"],  # type: ignore[index]
            "manifest": _BATCH_MANIFEST,
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": broken},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 409
    assert "'manifest'" in response.text
    assert "manifest.nope" in response.text


def test_the_publish_gate_supplies_warehouse_facts_to_a_batch_manifest(
    app: FastAPI, client: TestClient
) -> None:
    # A batch manifest may reference warehouse.*; the gate renders only if it
    # supplies a representative warehouse's facts (the first recent consignment
    # that has one), so a valid warehouse-referencing manifest publishes.
    _publish_v1_with_config(client)
    _add_warehoused_history(app, "W-00001", "testcarrier", "DEPOT1")

    with_depot_manifest = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": TEST_CARRIER_DEFINITION["operations"]["book"],  # type: ignore[index]
            "manifest": {
                "steps": [
                    {
                        "name": "declare",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "depot", "source": "warehouse.code"}
                            ],
                        },
                    }
                ],
            },
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": with_depot_manifest},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 200


def test_the_batch_manifest_gate_uses_only_this_carriers_own_consignments(
    app: FastAPI, client: TestClient
) -> None:
    # A manifest is single-carrier: the gate synthesizes it from THIS carrier's
    # own recent consignments, never another carrier's traffic. testcarrier has
    # no history of its own, so a broken manifest.* source has nothing to render
    # against and the gate skips it - a neighbour's consignment must not stand in
    # and produce a spurious 409.
    _publish_v1_with_config(client)
    _add_history(app, "THEIRS-00001", "othercarrier")

    broken = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": TEST_CARRIER_DEFINITION["operations"]["book"],  # type: ignore[index]
            "manifest": _BATCH_MANIFEST,
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": broken},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 200


def test_the_batch_manifest_gate_skips_a_stale_representative_warehouse(
    app: FastAPI, client: TestClient
) -> None:
    # A consignment's warehouse code is a denormalised string that can outlive
    # its Warehouse row. The most recent code may be stale while an older one is
    # live; the gate must supply the live depot's facts, not drop them because
    # the newest code no longer resolves - else a warehouse.* manifest 409s a
    # publish that would render fine.
    _publish_v1_with_config(client)
    _add_warehoused_history(app, "OLD-00001", "testcarrier", "DEPOT1")
    _add_history(app, "NEW-00001", "testcarrier")
    with app.state.session_factory() as session:
        newest = (
            session.execute(
                select(Consignment).where(Consignment.order_number == "NEW-00001")
            )
            .scalars()
            .one()
        )
        newest.warehouse = "GHOST-DEPOT"  # more recent, but no Warehouse row exists
        session.commit()

    with_depot_manifest = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": TEST_CARRIER_DEFINITION["operations"]["book"],  # type: ignore[index]
            "manifest": {
                "steps": [
                    {
                        "name": "declare",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "depot", "source": "warehouse.code"}
                            ],
                        },
                    }
                ],
            },
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": with_depot_manifest},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 200, response.text


def test_the_publish_gate_renders_a_fan_out_manifest(
    app: FastAPI, client: TestClient
) -> None:
    # A fan-out manifest renders per consignment, so the gate covers it like a
    # book op: its broken shipment source - which validates at draft time, roots
    # only - is caught here at publish.
    _publish_v1_with_config(client)
    client.put(
        "/api/carriers/testcarrier/config",
        json={
            "api_key": "K-1",
            "base_url": "https://api.test.example",
            "sftp_remote_dir": "/inbox",
        },
    )
    _add_history(app, "95000254580", "testcarrier")

    broken = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": TEST_CARRIER_DEFINITION["operations"]["book"],  # type: ignore[index]
            "manifest": _FAN_OUT_MANIFEST,
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": broken},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 409
    assert "'manifest'" in response.text
    assert "shipment.nope" in response.text


def test_the_publish_gate_supplies_warehouse_facts(
    app: FastAPI, client: TestClient
) -> None:
    # An operation referencing warehouse.* renders only if the gate supplies
    # warehouse facts (it 409'd unconditionally before).
    _publish_v1_with_config(client)
    _add_warehoused_history(app, "W-00001", "testcarrier", "DEPOT1")

    with_depot = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "order", "source": "shipment.order_number"},
                                {"target": "depot", "source": "warehouse.code"},
                            ],
                        },
                    }
                ]
            }
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": with_depot},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 200


def test_golden_replay_covers_all_carriers_history_by_default(
    app: FastAPI, client: TestClient
) -> None:
    """Any historical shipment is a valid render input, whichever carrier
    dispatched it - the default corpus is every recent consignment."""
    _publish_v1_with_config(client)
    _add_history(app, "OURS-00001", "testcarrier")
    _add_history(app, "THEIRS-00001", "othercarrier")
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    ).json()

    replay = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/replay",
        json={},
    ).json()

    assert replay["total"] == 2
    assert {r["order_number"] for r in replay["results"]} == {
        "OURS-00001",
        "THEIRS-00001",
    }


def test_golden_replay_filters_to_the_definitions_carrier_when_asked(
    app: FastAPI, client: TestClient
) -> None:
    _publish_v1_with_config(client)
    _add_history(app, "OURS-00001", "testcarrier")
    _add_history(app, "THEIRS-00001", "othercarrier")
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    ).json()

    replay = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/replay",
        json={"only_this_carrier": True},
    ).json()

    assert replay["total"] == 1
    assert [r["order_number"] for r in replay["results"]] == ["OURS-00001"]


_CSV_PLUCK_DEFINITION = {
    "carrier": "csvcarrier",
    "name": "CSV Carrier",
    "auth": {"scheme": "none"},
    "operations": {
        "book": {
            "steps": [
                {
                    "name": "upload",
                    "transport": "ftp_upload",
                    "request": {
                        "url": "config.dir",
                        "filename": "{shipment.order_number}.csv",
                        "content_type": "csv",
                        "mapping": [
                            {
                                "target": "codes",
                                "source": "shipment.parcels",
                                "pluck": "item.barcode",
                            }
                        ],
                    },
                }
            ],
            "label": {"source": "local_render"},
        }
    },
}


def test_the_active_endpoint_loads_a_stored_definition_leniently(
    app: FastAPI, client: TestClient
) -> None:
    # A published def that breaks a since-tightened authoring-policy rule still
    # books, so the active endpoint must show it, not 500. Inserted directly, as
    # publish would reject it today.
    with app.state.session_factory() as session:
        session.add(
            CarrierDefinitionVersion(
                carrier="csvcarrier",
                version=1,
                status="published",
                author="test",
                data=_CSV_PLUCK_DEFINITION,
            )
        )
        session.commit()

    response = client.get("/api/carriers/csvcarrier/definitions/active")

    assert response.status_code == 200
    assert response.json()["version"] == 1


def test_golden_replay_refuses_a_stale_active_definition(
    app: FastAPI, client: TestClient
) -> None:
    # A published def that only breaks a since-tightened rule - booking loads
    # it, but replay flags it rather than diff a stale baseline.
    with app.state.session_factory() as session:
        session.add(
            CarrierDefinitionVersion(
                carrier="csvcarrier",
                version=1,
                status="published",
                author="test",
                data=_CSV_PLUCK_DEFINITION,
            )
        )
        session.commit()
    valid_draft = {
        **_CSV_PLUCK_DEFINITION,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "upload",
                        "transport": "ftp_upload",
                        "request": {
                            "url": "config.dir",
                            "filename": "{shipment.order_number}.csv",
                            "content_type": "csv",
                            "mapping": [
                                {"target": "order", "source": "shipment.order_number"}
                            ],
                        },
                    }
                ],
                "label": {"source": "local_render"},
            }
        },
    }
    draft = client.post(
        "/api/carriers/csvcarrier/definitions/drafts",
        json={"author": "jake", "definition": valid_draft},
    ).json()

    replay = client.post(
        f"/api/carriers/csvcarrier/definitions/versions/{draft['version']}/replay",
        json={},
    )

    assert replay.status_code == 409
    assert "no longer valid" in replay.text


# step 1 declares output `real_ref`; a two-step book op where step 2 reads it.
_STEP1: dict[str, object] = {
    "name": "step1",
    "transport": "http",
    "request": {
        "method": "POST",
        "url": "config.url",
        "content_type": "json",
        "mapping": [{"target": "o", "source": "shipment.order_number"}],
    },
    "response": {"format": "json", "extract": [{"name": "real_ref", "path": "id"}]},
}


def _stepcarrier_def(step2_source: str) -> dict[str, object]:
    return {
        "carrier": "stepcarrier",
        "name": "Step Carrier",
        "auth": {"scheme": "none"},
        "operations": {
            "book": {
                "steps": [
                    _STEP1,
                    {
                        "name": "step2",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.url",
                            "content_type": "json",
                            "mapping": [{"target": "r", "source": step2_source}],
                        },
                    },
                ],
                "label": {"source": "local_render"},
            }
        },
    }


def test_golden_replay_refuses_an_active_with_an_unknown_step_output(
    app: FastAPI, client: TestClient
) -> None:
    # step 2 references an output step 1 never declares - the placeholder case
    # from #60, which renders offline as a token rather than an error.
    with app.state.session_factory() as session:
        session.add(
            CarrierDefinitionVersion(
                carrier="stepcarrier",
                version=1,
                status="published",
                author="test",
                data=_stepcarrier_def("steps.step1.typoed_ref"),
            )
        )
        session.commit()
    draft = client.post(
        "/api/carriers/stepcarrier/definitions/drafts",
        json={"author": "jake", "definition": _stepcarrier_def("steps.step1.real_ref")},
    ).json()

    replay = client.post(
        f"/api/carriers/stepcarrier/definitions/versions/{draft['version']}/replay",
        json={},
    )

    assert replay.status_code == 409
    assert "no longer valid" in replay.text


def test_golden_replay_refuses_a_stale_draft(app: FastAPI, client: TestClient) -> None:
    # The draft role carries the same staleness risk as the active: a draft
    # valid when created can break a since-tightened rule. Replay flags it (409),
    # not 500 at load.
    with app.state.session_factory() as session:
        session.add(
            CarrierDefinitionVersion(
                carrier="stepcarrier",
                version=1,
                status="published",
                author="test",
                data=_stepcarrier_def("steps.step1.real_ref"),
            )
        )
        session.add(
            CarrierDefinitionVersion(
                carrier="stepcarrier",
                version=2,
                status="draft",
                author="test",
                data=_stepcarrier_def("steps.step1.typoed_ref"),
            )
        )
        session.commit()

    replay = client.post(
        "/api/carriers/stepcarrier/definitions/versions/2/replay", json={}
    )

    assert replay.status_code == 409
    assert "draft version 2 is no longer valid" in replay.text


def test_golden_replay_ignores_staleness_in_an_operation_it_does_not_render(
    app: FastAPI, client: TestClient
) -> None:
    # Replay renders only the book op, so staleness in an unrelated operation
    # (here a track step referencing an unknown prior step, which a tightened
    # rule now rejects) must not block a healthy book replay.
    stale_track_active = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": TEST_CARRIER_DEFINITION["operations"]["book"],  # type: ignore[index]
            "track": {
                "steps": [
                    {
                        "name": "status",
                        "transport": "http",
                        "request": {
                            "method": "GET",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [{"target": "x", "source": "steps.nope.out"}],
                        },
                    }
                ],
            },
        },
    }
    with app.state.session_factory() as session:
        session.add(
            CarrierDefinitionVersion(
                carrier="testcarrier",
                version=1,
                status="published",
                author="test",
                data=stale_track_active,
            )
        )
        session.commit()
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    ).json()

    replay = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/replay",
        json={},
    )

    assert replay.status_code == 200
