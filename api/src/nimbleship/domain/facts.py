"""The named facts Carrier Definitions map from (ADR 0009): one builder
per fact root, shared by live booking, the publish render gate, and Golden
Replay - a definition must render from identical facts in all three, or
replay would diff phantom differences."""

from nimbleship.models import Consignment, Warehouse


def shipment_facts(consignment: Consignment) -> dict[str, object]:
    return {
        "order_number": consignment.order_number,
        "recipient_name": consignment.recipient_name,
        "address_lines": consignment.address_lines,
        "postcode": consignment.postcode,
        "destination_country": consignment.destination_country,
        "parcels": [
            {"weight_kg": parcel.weight_kg, "barcode": parcel.barcode}
            for parcel in consignment.parcels
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
