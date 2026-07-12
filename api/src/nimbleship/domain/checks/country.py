from nimbleship.domain.model import Check, ServiceDeclaration, Shipment


class CountryCheck:
    name = "country"

    def evaluate(self, service: ServiceDeclaration, shipment: Shipment) -> Check:
        return Check(
            name=self.name,
            ok=shipment.destination_country in service.countries,
            expected=f"one of {', '.join(service.countries)}",
            actual=shipment.destination_country,
        )
