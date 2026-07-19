"""Scrub known credentials from the onboarding packet before it reaches the model
(ADR 0018: secrets never reach the model; the AI is told only that a secret exists at
config.apiKey, never its value). The packet is a forwarded email or pasted docs, which
routinely embed the same credentials the operator entered into Carrier Config - those
stored values are KNOWN, so exact occurrences are replaced with their config.* path.
This is defence in depth for the doc text; the primary channel is the credentials
intake, which routes values straight to config and never into the packet at all."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.models import CarrierConfig

# Values shorter than this are skipped: redacting e.g. a port number or a two-letter
# country code would shred the documentation without protecting a real secret.
MIN_SECRET_LENGTH = 6


def _leaf_values(data: object, path: str) -> list[tuple[str, str]]:
    """(config path, value) for every string leaf of a config blob."""
    if isinstance(data, str):
        return [(path, data)]
    if isinstance(data, dict):
        return [
            leaf
            for key, value in data.items()
            for leaf in _leaf_values(value, f"{path}.{key}")
        ]
    if isinstance(data, list):
        return [
            leaf
            for index, item in enumerate(data)
            for leaf in _leaf_values(item, f"{path}.{index}")
        ]
    return []


def known_secrets(session: Session) -> list[tuple[str, str]]:
    """(config path, value) for every stored carrier-config string leaf long enough to
    be a credential. All carriers, not just the one being onboarded: a stored secret
    must never reach the model regardless of which packet happens to contain it."""
    pairs: list[tuple[str, str]] = []
    for row in session.execute(select(CarrierConfig)).scalars():
        pairs.extend(_leaf_values(row.data, "config"))
    return [(path, value) for path, value in pairs if len(value) >= MIN_SECRET_LENGTH]


def redact_packet(session: Session, packet: str) -> str:
    """Replace every occurrence of a known stored config value in `packet` with its
    config.* path, longest values first so a secret that contains another (a URL
    embedding a token) is consumed whole before the shorter match can split it."""
    for path, value in sorted(
        known_secrets(session), key=lambda pair: len(pair[1]), reverse=True
    ):
        packet = packet.replace(value, f"[use {path}]")
    return packet
