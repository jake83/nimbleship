"""The exemplar for optimistic unknown-fact handling (ADR 0007): a shipment
that has not stated its longest dimension is eligible until dispatch-time
facts say otherwise. New checks should follow this shape."""

from nimbleship.domain.model import Check, ServiceDeclaration, Shipment


class DimensionCheck:
    name = "dimension"

    def evaluate(self, service: ServiceDeclaration, shipment: Shipment) -> Check:
        if service.max_dimension_cm is None:
            return Check(
                name=self.name,
                ok=True,
                expected="no dimension limit",
                actual="not limited by this service",
            )
        if shipment.max_dimension_cm is None:
            return Check(
                name=self.name,
                ok=True,
                expected=f"at most {service.max_dimension_cm}cm",
                actual="unknown (optimistic)",
            )
        return Check(
            name=self.name,
            ok=shipment.max_dimension_cm <= service.max_dimension_cm,
            expected=f"at most {service.max_dimension_cm}cm",
            actual=f"{shipment.max_dimension_cm}cm",
        )
