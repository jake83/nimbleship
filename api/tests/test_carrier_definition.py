"""The Carrier Definition schema (ADR 0009): declarative mappings over
closed vocabularies, validated at authoring time - a definition referencing
unknown facts or transforms must fail before it is ever stored."""

import pytest
from pydantic import ValidationError

from nimbleship.domain.carrier_definition import CarrierDefinition

MINIMAL = {
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
                        "content_type": "form",
                        "mapping": [
                            {
                                "target": "order_number",
                                "source": "shipment.order_number",
                            },
                            {
                                "target": "address",
                                "source": "shipment.address_lines",
                                "transform": {"name": "join", "with": ", "},
                            },
                            {"target": "service_level", "const": "2 Man"},
                        ],
                    },
                    "response": {
                        "format": "xml",
                        "extract": [
                            {
                                "name": "tracking_reference",
                                "path": "/response/carrier_reference",
                            }
                        ],
                    },
                }
            ],
            "label": {"source": "local_render"},
        }
    },
}


def test_a_minimal_definition_validates() -> None:
    definition = CarrierDefinition.model_validate(MINIMAL)

    assert definition.carrier == "furdeco"
    step = definition.operations["book"].steps[0]
    assert step.request.mapping[0].source == "shipment.order_number"


def test_unknown_transform_is_rejected_at_authoring() -> None:
    bad = {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "form",
                            "mapping": [
                                {
                                    "target": "x",
                                    "source": "shipment.order_number",
                                    "transform": {"name": "reticulate"},
                                }
                            ],
                        },
                    }
                ],
            }
        },
    }

    with pytest.raises(ValidationError):
        CarrierDefinition.model_validate(bad)


def test_unknown_source_root_is_rejected_at_authoring() -> None:
    bad = {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "form",
                            "mapping": [
                                {"target": "x", "source": "shipmnt.order_number"}
                            ],
                        },
                    }
                ],
            }
        },
    }

    with pytest.raises(ValidationError, match="unknown source root"):
        CarrierDefinition.model_validate(bad)


def test_mapping_requires_exactly_one_of_source_or_const() -> None:
    for entry in (
        {"target": "x"},
        {"target": "x", "source": "shipment.value", "const": "both"},
    ):
        bad = {
            **MINIMAL,
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "save",
                            "transport": "http",
                            "request": {
                                "method": "POST",
                                "url": "config.base_url",
                                "content_type": "form",
                                "mapping": [entry],
                            },
                        }
                    ],
                }
            },
        }
        with pytest.raises(ValidationError, match="exactly one of"):
            CarrierDefinition.model_validate(bad)


def test_step_outputs_are_valid_sources_for_later_steps() -> None:
    two_step = {
        **MINIMAL,
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
                                {
                                    "target": "order",
                                    "source": "shipment.order_number",
                                }
                            ],
                        },
                        "response": {
                            "format": "json",
                            "extract": [{"name": "tracking", "path": "trackingCodes"}],
                        },
                    },
                    {
                        "name": "label",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "trackingNumber",
                                    "source": "steps.manifest.tracking",
                                }
                            ],
                        },
                    },
                ],
            }
        },
    }

    definition = CarrierDefinition.model_validate(two_step)

    assert definition.operations["book"].steps[1].request.mapping[0].source == (
        "steps.manifest.tracking"
    )


def test_step_reference_to_unknown_step_is_rejected() -> None:
    bad = {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "only",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "x", "source": "steps.nope.tracking"}
                            ],
                        },
                    }
                ],
            }
        },
    }

    with pytest.raises(ValidationError, match="unknown step"):
        CarrierDefinition.model_validate(bad)


def test_auth_secret_source_is_validated_at_authoring() -> None:
    bad = {
        **MINIMAL,
        "auth": {
            "scheme": "query_key",
            "param": "key",
            "secret": "cofnig.api_key",
        },
    }

    with pytest.raises(ValidationError, match="unknown source root"):
        CarrierDefinition.model_validate(bad)


def test_success_when_without_a_path_is_rejected_at_authoring() -> None:
    import copy

    malformed = copy.deepcopy(MINIMAL)
    step = malformed["operations"]["book"]["steps"][0]  # type: ignore[index]
    step["response"]["success_when"] = {"equals": "OK"}

    with pytest.raises(ValidationError, match="path"):
        CarrierDefinition.model_validate(malformed)


def test_error_message_without_a_path_is_rejected_at_authoring() -> None:
    import copy

    malformed = copy.deepcopy(MINIMAL)
    step = malformed["operations"]["book"]["steps"][0]  # type: ignore[index]
    step["response"]["error_message"] = {"pat": "/response/error"}

    with pytest.raises(ValidationError, match="path"):
        CarrierDefinition.model_validate(malformed)


def test_step_output_names_are_validated_at_authoring() -> None:
    """A typo'd OUTPUT name (valid step, wrong extraction) must fail at
    authoring - at execution it would render a placeholder token and send
    it to a live carrier (refuter, PR #30)."""
    bad = {
        **MINIMAL,
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
                                {
                                    "target": "order",
                                    "source": "shipment.order_number",
                                }
                            ],
                        },
                        "response": {
                            "format": "json",
                            "extract": [{"name": "tracking", "path": "codes"}],
                        },
                    },
                    {
                        "name": "label",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "code",
                                    "source": "steps.manifest.trackign",
                                }
                            ],
                        },
                    },
                ],
            }
        },
    }

    with pytest.raises(ValidationError, match="unknown output"):
        CarrierDefinition.model_validate(bad)


def test_step_reference_to_a_step_with_no_extractions_is_rejected() -> None:
    bad = {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "fire",
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
                    },
                    {
                        "name": "second",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "x", "source": "steps.fire.anything"}
                            ],
                        },
                    },
                ],
            }
        },
    }

    with pytest.raises(ValidationError, match="unknown output"):
        CarrierDefinition.model_validate(bad)
