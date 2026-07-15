"""Hand-rolled translation for the MetaPack SOAP 1.1 dialect (ADR 0011).

defusedxml parses the untrusted WMS input (blocking entity-expansion and
external-entity attacks a raw parser would run); stdlib ElementTree builds the
replies, as the render engine already does. The dialect uses multiref encoding:
a complex value is an `href="#id"` pointing at an id-tagged sibling under the
Body, so parsing resolves those references before the caller navigates."""

import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
SERVICES = "urn:DeliveryManager/services"
_BODY = f"{{{SOAP_ENV}}}Body"

ET.register_namespace("soap", SOAP_ENV)
ET.register_namespace("tns", SERVICES)


class SoapFault(Exception):
    """A malformed or unsupported request; surfaced to the WMS as a SOAP fault."""


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class SoapRequest:
    method: str
    operation: ET.Element
    _refs: dict[str, ET.Element]

    def follow(self, element: ET.Element) -> ET.Element:
        # A multiref value is an href="#id" pointing at an id-tagged sibling; a
        # plain element is returned as-is.
        href = element.get("href")
        if href is None:
            return element
        if not href.startswith("#"):
            raise SoapFault(f"unsupported href '{href}'")
        target = self._refs.get(href[1:])
        if target is None:
            raise SoapFault(f"unresolved href '{href}'")
        return target

    def follow_child(self, parent: ET.Element, name: str) -> ET.Element | None:
        child = parent.find(name)
        return None if child is None else self.follow(child)


def parse_request(body: bytes) -> SoapRequest:
    try:
        root: ET.Element = DET.fromstring(body)
    except ET.ParseError as error:
        # The message is not echoed: a parse error can carry a fragment of the
        # untrusted input.
        raise SoapFault("malformed XML") from error
    except DefusedXmlException as error:
        # defusedxml raises this (not ParseError) for a DOCTYPE, entity, or
        # external reference - the attacks it blocks. Turn it into a fault too,
        # so the security path returns the dialect's error shape, not a 500.
        raise SoapFault("forbidden XML construct") from error
    body_el = root.find(_BODY)
    if body_el is None:
        raise SoapFault("no SOAP Body")
    children = list(body_el)
    if not children:
        raise SoapFault("empty SOAP Body")
    # Multiref targets are id-tagged Body children; collect ids only from direct
    # children, so a nested id cannot shadow a top-level target on hostile input.
    refs: dict[str, ET.Element] = {}
    for element in children:
        ref_id = element.get("id")
        if ref_id is not None:
            refs[ref_id] = element
    # The operation is the Body child in the services namespace; the value
    # elements are in the types/encoding namespaces. Selecting by namespace is
    # robust to an encoder that ids the operation wrapper or reorders the Body.
    operation = next(
        (child for child in children if child.tag.startswith(f"{{{SERVICES}}}")),
        None,
    )
    if operation is None:
        raise SoapFault("no operation element in the SOAP Body")
    return SoapRequest(
        method=_localname(operation.tag), operation=operation, _refs=refs
    )


def response(operation: str, build: Callable[[ET.Element], None]) -> bytes:
    """Wrap an operation-return element in the SOAP envelope; `build` populates
    the operation element the WMS reads its result from."""
    envelope = ET.Element(f"{{{SOAP_ENV}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP_ENV}}}Body")
    operation_element = ET.SubElement(body, f"{{{SERVICES}}}{operation}")
    build(operation_element)
    result: bytes = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
    return result


def text_child(parent: ET.Element, name: str, value: str) -> ET.Element:
    child = ET.SubElement(parent, name)
    child.text = value
    return child


def fault(message: str) -> bytes:
    """A SOAP 1.1 Fault - the dialect's error shape, returned with HTTP 500."""
    envelope = ET.Element(f"{{{SOAP_ENV}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP_ENV}}}Body")
    fault_element = ET.SubElement(body, f"{{{SOAP_ENV}}}Fault")
    text_child(fault_element, "faultcode", "soap:Client")
    text_child(fault_element, "faultstring", message)
    result: bytes = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
    return result
