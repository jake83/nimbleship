"""Shipping Area serving: a service declaring areas_served delivers only to
shipments resolved into at least one of them; areas_served None means
anywhere within the allowed countries. Empty shipment areas means no area
matched the destination postcode, treated optimistically per ADR 0007."""

from nimbleship.domain.model import Check, ServiceDeclaration, Shipment


class AreaServedCheck:
    name = "area_served"

    def evaluate(self, service: ServiceDeclaration, shipment: Shipment) -> Check:
        if service.areas_served is None:
            return Check(
                name=self.name,
                ok=True,
                expected="no served-area restriction",
                actual="not limited by this service",
            )
        expected = f"in one of {', '.join(service.areas_served)}"
        if not shipment.shipping_areas:
            return Check(
                name=self.name,
                ok=True,
                expected=expected,
                actual="no areas matched (optimistic)",
            )
        overlap = set(shipment.shipping_areas) & set(service.areas_served)
        return Check(
            name=self.name,
            ok=bool(overlap),
            expected=expected,
            actual=f"in {', '.join(shipment.shipping_areas)}",
        )
