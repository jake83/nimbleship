"""Delivery Proposition membership (CONTEXT.md: sold at checkout, honoured
at dispatch). Services declare which propositions they fulfil; dispatch
selects only among services fulfilling the proposition the customer bought.
An empty declaration means unrestricted; a shipment without a proposition is
optimistically eligible (ADR 0007), following checks/dimension.py."""

from nimbleship.domain.model import Check, ServiceDeclaration, Shipment


class PropositionCheck:
    name = "proposition"

    def evaluate(self, service: ServiceDeclaration, shipment: Shipment) -> Check:
        if not service.propositions:
            return Check(
                name=self.name,
                ok=True,
                expected="no proposition restriction",
                actual="not restricted by this service",
            )
        expected = f"one of {', '.join(service.propositions)}"
        if shipment.proposition is None:
            return Check(
                name=self.name,
                ok=True,
                expected=expected,
                actual="unknown (optimistic)",
            )
        return Check(
            name=self.name,
            ok=shipment.proposition in service.propositions,
            expected=expected,
            actual=shipment.proposition,
        )
