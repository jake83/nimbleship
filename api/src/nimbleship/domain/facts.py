"""The named facts Carrier Definitions map from (ADR 0009): one builder
per fact root, shared by live booking, the publish render gate, and Golden
Replay - a definition must render from identical facts in all three, or
replay would diff phantom differences."""

from datetime import UTC
from zoneinfo import ZoneInfo

from nimbleship.models import Consignment, Manifest, Warehouse


def shipment_facts(consignment: Consignment) -> dict[str, object]:
    return {
        "order_number": consignment.order_number,
        "recipient_name": consignment.recipient_name,
        "address_lines": consignment.address_lines,
        "postcode": consignment.postcode,
        "destination_country": consignment.destination_country,
        "parcels": [
            {
                "weight_kg": parcel.weight_kg,
                "barcode": parcel.barcode,
                # The carrier-facing barcode (a Dachser SSCC, minted at
                # booking): a fan-out manifest declares it per parcel from
                # shipment facts, so it must travel here as manifest_facts
                # already carries it.
                "carrier_barcode": parcel.carrier_barcode,
            }
            for parcel in consignment.parcels
        ],
    }


def manifest_facts(
    manifest: Manifest, consignments: list[Consignment], timezone: str
) -> dict[str, object]:
    # The manifest date is the warehouse's LOCAL dispatch day, not the UTC date:
    # a near-midnight scan-out must declare the day the warehouse observes.
    # created_at is UTC (naive when SQLite round-trips it, so pin the zone).
    created = manifest.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    local_date = created.astimezone(ZoneInfo(timezone)).date().isoformat()
    return {
        "carrier": manifest.carrier,
        "date": local_date,
        "consignment_count": len(consignments),
        "consignments": [
            {
                "order_number": consignment.order_number,
                "tracking_reference": consignment.tracking_reference,
                "recipient_name": consignment.recipient_name,
                "postcode": consignment.postcode,
                "destination_country": consignment.destination_country,
                "parcel_count": len(consignment.parcels),
                "parcels": [
                    {
                        "weight_kg": parcel.weight_kg,
                        "barcode": parcel.barcode,
                        "carrier_barcode": parcel.carrier_barcode,
                    }
                    for parcel in consignment.parcels
                ],
            }
            for consignment in consignments
        ],
    }


def warehouse_facts(warehouse: Warehouse) -> dict[str, object]:
    return {
        "code": warehouse.code,
        "name": warehouse.name,
        "company_name": warehouse.company_name,
        "phone": warehouse.phone,
        "email": warehouse.email,
        "address_lines": warehouse.address_lines,
        "postcode": warehouse.postcode,
        "country": warehouse.country,
    }
