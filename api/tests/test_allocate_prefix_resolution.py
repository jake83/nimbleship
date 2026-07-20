"""An allocate prefix is a config.* source like any other: its path resolves by
nested traversal, exactly as missing_config_keys and the render engine read it -
one path semantics everywhere, so a config the completeness check calls complete
cannot fail the mint at dispatch."""

import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session

from nimbleship.domain.carrier_definition import AllocationSpec
from nimbleship.domain.consignments import ConsignmentError, _mint_parcel_allocations
from nimbleship.models import Consignment, Parcel

# A 13-digit GS1 company prefix leaves a 4-digit serial.
PREFIX = "9500000000000"


def _persisted_consignment(session: Session) -> Consignment:
    consignment = Consignment(
        order_number="ORD-2001",
        recipient_name="Test Recipient",
        address_lines=["1 Test Street"],
        postcode="TE1 1ST",
        destination_country="GB",
        status="allocated",
        carrier="ssccarrier",
        allocation={},
        parcels=[Parcel(sequence=1, weight_kg="1.0", barcode="ORD-2001-1")],
    )
    session.add(consignment)
    session.flush()
    return consignment


def test_a_nested_allocate_prefix_resolves_like_every_other_config_path(
    app: FastAPI,
) -> None:
    spec = AllocationSpec(kind="sscc", per="parcel", prefix="config.depot.code")
    with app.state.session_factory() as session:
        consignment = _persisted_consignment(session)
        _mint_parcel_allocations(
            session, consignment, [spec], {"depot": {"code": PREFIX}}
        )
        [parcel] = consignment.parcels
        assert parcel.carrier_barcode is not None
        assert parcel.carrier_barcode.startswith(PREFIX)


def test_an_unresolvable_allocate_prefix_still_fails_loudly(app: FastAPI) -> None:
    spec = AllocationSpec(kind="sscc", per="parcel", prefix="config.depot.code")
    with app.state.session_factory() as session:
        consignment = _persisted_consignment(session)
        with pytest.raises(ConsignmentError) as caught:
            _mint_parcel_allocations(session, consignment, [spec], {"depot": {}})
        assert "not configured" in str(caught.value.detail)
