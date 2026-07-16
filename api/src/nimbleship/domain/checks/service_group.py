"""Service Group membership (ADR 0012). A service group is an allow-list of
carrier services; a service is eligible only if it is a member of one of the
groups the order accepts. Deliberately unlike PropositionCheck: a service that
declares no group is NOT a wildcard - it is unreachable when a filter is active.
An empty accepted set does not restrict (the JSON path never sends groups; a
shipment without one is optimistically eligible, ADR 0007)."""

from nimbleship.domain.model import Check, ServiceDeclaration, Shipment


class ServiceGroupCheck:
    name = "service_group"

    def evaluate(self, service: ServiceDeclaration, shipment: Shipment) -> Check:
        if not shipment.accepted_service_groups:
            return Check(
                name=self.name,
                ok=True,
                expected="member of an accepted group",
                actual="unknown (optimistic)",
            )
        accepted = set(shipment.accepted_service_groups)
        expected = f"member of one of {', '.join(sorted(accepted))}"
        return Check(
            name=self.name,
            ok=bool(accepted.intersection(service.service_groups)),
            expected=expected,
            actual=(
                f"member of {', '.join(service.service_groups)}"
                if service.service_groups
                else "member of no group"
            ),
        )
