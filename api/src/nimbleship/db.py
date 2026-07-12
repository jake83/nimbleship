from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from nimbleship.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache(maxsize=4)
def _engine(database_url: str) -> Engine:
    # Schema is owned by Alembic (uv run alembic upgrade head); the engine
    # never creates tables. Tests build their own engines with create_all.
    return create_engine(database_url)


def get_session() -> Iterator[Session]:
    with Session(_engine(get_settings().database_url)) as session:
        yield session
        session.commit()
