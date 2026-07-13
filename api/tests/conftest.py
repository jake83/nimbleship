from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from nimbleship.db import Base, get_session
from nimbleship.labels.store import LabelStore, get_label_store
from nimbleship.main import create_app


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    # The lifespan prune reads settings directly (it runs outside request
    # dependency injection), so point it at the test directory too.
    monkeypatch.setenv("NIMBLESHIP_LABELS_DIR", str(tmp_path / "labels"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def session_override() -> Iterator[Session]:
        with factory() as session:
            yield session
            session.commit()

    application = create_app()
    # Tests that assert on rows the API does not expose (e.g. recorded
    # carrier traffic) open their own sessions from here.
    application.state.session_factory = factory
    application.dependency_overrides[get_session] = session_override
    application.dependency_overrides[get_label_store] = lambda: LabelStore(
        tmp_path / "labels"
    )
    yield application
    engine.dispose()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
