from fastapi.testclient import TestClient

from nimbleship.main import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_docs_served_under_api_prefix() -> None:
    client = TestClient(create_app())

    assert client.get("/api/docs").status_code == 200
    assert client.get("/api/openapi.json").status_code == 200
