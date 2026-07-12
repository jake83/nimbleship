"""Shipping Area blocking: a service declaring areas_blocked refuses any
shipment resolved into one of them. Empty shipment areas means no area
matched the destination postcode, which is treated optimistically per
ADR 0007 (checkout may not know the postcode's areas yet)."""

from nimbleship.domain.model import Check, ServiceDeclaration, Shipment


class AreaBlockedCheck:
    name = "area_blocked"

    def evaluate(self, service: ServiceDeclaration, shipment: Shipment) -> Check:
        if not service.areas_blocked:
            return Check(
                name=self.name,
                ok=True,
                expected="no blocked areas",
                actual="not limited by this service",
            )
        expected = f"not in {', '.join(service.areas_blocked)}"
        if not shipment.shipping_areas:
            return Check(
                name=self.name,
                ok=True,
                expected=expected,
                actual="no areas matched (optimistic)",
            )
        blocked = sorted(set(shipment.shipping_areas) & set(service.areas_blocked))
        return Check(
            name=self.name,
            ok=not blocked,
            expected=expected,
            actual=f"in {', '.join(shipment.shipping_areas)}",
        )
