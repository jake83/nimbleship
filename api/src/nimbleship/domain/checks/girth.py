"""Girth (CONTEXT.md: 2h + 2w + length, in cm) against a service's declared
maximum. Follows checks/dimension.py: an unknown shipment girth is
optimistically eligible (ADR 0007) until dispatch-time facts say otherwise."""

from nimbleship.domain.model import Check, ServiceDeclaration, Shipment


class GirthCheck:
    name = "girth"

    def evaluate(self, service: ServiceDeclaration, shipment: Shipment) -> Check:
        if service.max_girth_cm is None:
            return Check(
                name=self.name,
                ok=True,
                expected="no girth limit",
                actual="not limited by this service",
            )
        if shipment.max_girth_cm is None:
            return Check(
                name=self.name,
                ok=True,
                expected=f"at most {service.max_girth_cm}cm",
                actual="unknown (optimistic)",
            )
        return Check(
            name=self.name,
            ok=shipment.max_girth_cm <= service.max_girth_cm,
            expected=f"at most {service.max_girth_cm}cm",
            actual=f"{shipment.max_girth_cm}cm",
        )
