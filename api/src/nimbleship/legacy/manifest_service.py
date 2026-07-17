"""ManifestService operations (ADR 0013). createManifest closes a Manifest over
the consignments a mark-ready call readied for a carrier and warehouse, returns
the NS-native manifest code minted for the call, and defers the carrier send.
The WMS treats the code as fire-and-forget; it does not validate or reuse it."""

import xml.etree.ElementTree as ET

from sqlalchemy.orm import Session

from nimbleship.domain.manifests import (
    create_manifests,
    mint_manifest_code,
    ready_to_manifest,
)
from nimbleship.legacy import soap
from nimbleship.queue import defer_manifest_send


def handle(body: bytes, session: Session) -> bytes:
    request = soap.parse_request(body)
    if request.method == "createManifest":
        return _create_manifest(request, session)
    raise soap.SoapFault(f"unsupported ManifestService operation '{request.method}'")


def _create_manifest(request: soap.SoapRequest, session: Session) -> bytes:
    carrier = _required(request, "carrierCode")
    warehouse = _required(request, "warehouseCode")
    # A code is minted for every call, even an empty sweep: the WMS expects one
    # back regardless, and the sequence is independent of whether a Manifest row
    # results (ADR 0013).
    ready = ready_to_manifest(session, carrier, warehouse)
    manifests = create_manifests(session, ready)
    code = mint_manifest_code(session)
    for manifest in manifests:
        manifest.code = code
        defer_manifest_send(session, manifest.id)
    session.flush()

    def build(operation_element: ET.Element) -> None:
        # A single-element string array: the return wrapper holds one Item, the
        # array-member convention this edge uses on every response and parses on
        # every request (soap.string_array).
        array = ET.SubElement(operation_element, "createManifestReturn")
        soap.text_child(array, "Item", code)

    return soap.response("createManifestResponse", build)


def _required(request: soap.SoapRequest, name: str) -> str:
    child = request.follow_child(request.operation, name)
    value = (child.text or "").strip() if child is not None else ""
    if not value:
        raise soap.SoapFault(f"createManifest: missing {name}")
    return value
