"""Packet redaction (ADR 0018): known stored config values never reach the model -
each occurrence in the packet is replaced with its config.* path, so the model can
reference the secret's location without ever seeing its value."""

from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nimbleship.assistant import LlmReply
from nimbleship.carrier_builder.redaction import known_secrets, redact_packet
from nimbleship.models import CarrierConfig
from nimbleship.routers.carrier_builder import get_llm_client

Message = dict[str, object]


def _store(app: FastAPI, carrier: str, data: dict[str, object]) -> None:
    with app.state.session_factory() as session:
        session.add(CarrierConfig(carrier=carrier, data=data))
        session.commit()


def test_redact_replaces_a_stored_value_with_its_config_path(app: FastAPI) -> None:
    _store(app, "acme", {"api_key": "sk-hunter22secret", "port": "443"})
    packet = "Auth: send api key sk-hunter22secret as a header. Port 443."

    with app.state.session_factory() as session:
        redacted = redact_packet(session, packet)

    assert "sk-hunter22secret" not in redacted
    assert "[use config.api_key]" in redacted
    # Short values are not secrets; shredding them would ruin the documentation.
    assert "443" in redacted


def test_redact_is_case_insensitive(app: FastAPI) -> None:
    # Forwarded emails routinely re-case text (an upper-cased header line,
    # HTML-to-text conversion); a re-cased secret is still the secret.
    _store(app, "acme", {"api_key": "sk-hunter22secret"})
    packet = "AUTH KEY: SK-HUNTER22SECRET goes in the header."

    with app.state.session_factory() as session:
        redacted = redact_packet(session, packet)

    assert "SK-HUNTER22SECRET" not in redacted
    assert "hunter22" not in redacted.lower()
    assert "[use config.api_key]" in redacted


def test_redact_handles_nested_config_and_containment(app: FastAPI) -> None:
    # Longest-first: a URL embedding a token is consumed whole before the token
    # alone could split it, and the nested path names the leaf.
    _store(
        app,
        "acme",
        {
            "credentials": {"token": "tok_abcdef123"},
            "book_url": "https://api.acme.example/book?key=tok_abcdef123",
        },
    )
    packet = "POST https://api.acme.example/book?key=tok_abcdef123 with tok_abcdef123."

    with app.state.session_factory() as session:
        redacted = redact_packet(session, packet)

    assert "tok_abcdef123" not in redacted
    assert "[use config.book_url]" in redacted
    assert "[use config.credentials.token]" in redacted


def test_redact_covers_every_carrier_not_just_the_one_onboarding(
    app: FastAPI,
) -> None:
    # A stored secret must never reach the model regardless of whose packet contains
    # it - a forwarded email can mention another carrier's credentials.
    _store(app, "other", {"password": "s3cretpass"})
    with app.state.session_factory() as session:
        assert "s3cretpass" not in redact_packet(session, "pw is s3cretpass")
        assert ("config.password", "s3cretpass") in known_secrets(session)


class _CapturingLlm:
    def __init__(self) -> None:
        self.systems: list[str] = []

    def reply(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: Sequence[dict[str, object]],
    ) -> LlmReply:
        self.systems.append(system)
        return LlmReply(stop_reason="end_turn", text="Read the docs.", tool_uses=())


def test_messages_grounds_the_model_in_the_redacted_packet(
    app: FastAPI, client: TestClient
) -> None:
    # End to end through the route: the packet reaches the prompt, the stored secret
    # does not - its config path does.
    _store(app, "acme", {"api_key": "sk-hunter22secret"})
    llm = _CapturingLlm()
    app.dependency_overrides[get_llm_client] = lambda: llm

    response = client.post(
        "/api/carrier-builder/messages",
        json={
            "messages": [{"role": "user", "content": "onboard acme"}],
            "packet": "Acme API. Use key sk-hunter22secret on every call.",
        },
    )

    assert response.status_code == 200
    [system] = llm.systems
    assert "Acme API" in system
    assert "sk-hunter22secret" not in system
    assert "[use config.api_key]" in system


def test_messages_without_a_packet_adds_no_documentation_section(
    app: FastAPI, client: TestClient
) -> None:
    llm = _CapturingLlm()
    app.dependency_overrides[get_llm_client] = lambda: llm

    client.post(
        "/api/carrier-builder/messages",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert "Carrier documentation" not in llm.systems[0]
