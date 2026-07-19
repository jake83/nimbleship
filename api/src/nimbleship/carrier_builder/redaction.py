"""Scrub known stored config values from the onboarding packet before it reaches the
model (ADR 0018): each occurrence is replaced with its config.* path. Defence in depth
for the doc text - the credentials intake is the primary channel and never puts a
value in the packet at all. Deliberately broader than credentials: every long-enough
config value is scrubbed (an endpoint drifting between docs and config is worth
catching too, and the model should reference config.X either way)."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.models import CarrierConfig

# Values shorter than this are skipped: redacting e.g. a port number or a two-letter
# country code would shred the documentation without protecting a real secret.
MIN_REDACT_LENGTH = 6


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


def known_config_values(session: Session) -> list[tuple[str, str]]:
    """(config path, value) for every stored carrier-config string leaf long enough
    to redact. All carriers, not just the one being onboarded: a stored secret must
    never reach the model regardless of which packet happens to contain it."""
    pairs: list[tuple[str, str]] = []
    for row in session.execute(select(CarrierConfig)).scalars():
        pairs.extend(_leaf_values(row.data, "config"))
    return [(path, value) for path, value in pairs if len(value) >= MIN_REDACT_LENGTH]


def redact_packet(session: Session, packet: str) -> str:
    """Replace every occurrence of a known stored config value in `packet` with its
    config.* path. Longest values first, so a secret that contains another (a URL
    embedding a token) is consumed whole before the shorter match can split it.
    Case-insensitive: forwarded emails routinely re-case text (an upper-cased header
    line, HTML-to-text conversion), and a re-cased secret is still the secret."""
    for path, value in sorted(
        known_config_values(session), key=lambda pair: len(pair[1]), reverse=True
    ):
        # Backslashes doubled so the substituted text is never interpreted for group
        # references, whatever characters a config path carries.
        replacement = f"[use {path}]".replace("\\", "\\\\")
        packet = re.sub(re.escape(value), replacement, packet, flags=re.IGNORECASE)
    return packet
