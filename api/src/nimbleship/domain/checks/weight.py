from nimbleship.domain.model import Check, ServiceDeclaration, Shipment


class WeightBandCheck:
    name = "weight"

    def evaluate(self, service: ServiceDeclaration, shipment: Shipment) -> Check:
        return Check(
            name=self.name,
            ok=service.weight_min_kg
            <= shipment.total_weight_kg
            <= service.weight_max_kg,
            expected=f"{service.weight_min_kg}kg to {service.weight_max_kg}kg",
            actual=f"{shipment.total_weight_kg}kg",
        )
