"""Suggest a one-line rationale for a rulebook change (ADR 0017): diff the working
copy against the live rulebook and have the model phrase what changed, for the draft
`description` operations staff read. Distinct from the builder's conversational reply
- this is the durable one-liner the version history shows. The AI only suggests; the
operator edits before saving."""

from collections.abc import Sequence

from nimbleship.assistant.llm import LlmClient
from nimbleship.domain.allocation import ServiceDeclaration

RATIONALE_PROMPT = """\
You write a single-line change note for a carrier rulebook version, read by \
operations staff in the version history. Given the change below, reply with ONE \
concise line naming what changed in operational terms - no preamble, no quotes, no \
trailing period, at most about 140 characters. Example: "Added Saturday FedEx for GB \
orders over 20kg." If several things changed, summarise them in one line."""


def _service_summary(service: ServiceDeclaration) -> str:
    bits = [
        f"{service.code} ({service.carrier}, {service.name})",
        f"{service.weight_min_kg}-{service.weight_max_kg}kg",
        f"to {', '.join(service.countries)}",
        f"cost {service.cost}",
    ]
    if service.propositions:
        bits.append(f"propositions {', '.join(service.propositions)}")
    if service.service_groups:
        bits.append(f"groups {', '.join(service.service_groups)}")
    return ", ".join(bits)


def _changed_fields(old: ServiceDeclaration, new: ServiceDeclaration) -> str:
    before = old.model_dump(mode="json")
    after = new.model_dump(mode="json")
    changes = [
        f"{field} {before[field]!r} -> {after[field]!r}"
        for field in after
        if before.get(field) != after[field]
    ]
    return f"{new.code}: {', '.join(changes)}"


def _describe_change(
    active: Sequence[ServiceDeclaration], draft: Sequence[ServiceDeclaration]
) -> str | None:
    """A plain-text description of how `draft` differs from `active`, or None if they
    match. Feeds the model the exact diff so its one-liner is grounded, not guessed."""
    active_by = {s.code: s for s in active}
    draft_by = {s.code: s for s in draft}
    added = [draft_by[c] for c in draft_by if c not in active_by]
    removed = [active_by[c] for c in active_by if c not in draft_by]
    changed = [
        (active_by[c], draft_by[c])
        for c in draft_by
        if c in active_by and draft_by[c] != active_by[c]
    ]
    if not (added or removed or changed):
        return None
    lines: list[str] = []
    if added:
        lines.append("Added services:")
        lines += [f"- {_service_summary(s)}" for s in added]
    if removed:
        lines.append("Removed services:")
        lines += [f"- {s.code} ({s.carrier})" for s in removed]
    if changed:
        lines.append("Changed services:")
        lines += [f"- {_changed_fields(old, new)}" for old, new in changed]
    return "\n".join(lines)


def suggest_rationale(
    active: Sequence[ServiceDeclaration],
    draft: Sequence[ServiceDeclaration],
    *,
    llm: LlmClient,
) -> str | None:
    """A one-line rationale for how the working copy `draft` changes the live rulebook
    `active`, or None if nothing changed. The model phrases the computed diff."""
    change = _describe_change(active, draft)
    if change is None:
        return None
    reply = llm.reply(
        system=RATIONALE_PROMPT,
        messages=[{"role": "user", "content": change}],
        tools=[],
    )
    text = reply.text.strip()
    return text or None
