from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from nimbleship.config import get_settings

# Postgres advisory-lock keys in use, each serialising one write concern.
# Claim the next free key here before taking a new lock anywhere:
#   815_003  rulebook writes           (domain/rulebook.py)
#   815_004  proposition seeding       (domain/propositions.py)
#   815_005  definition writes         (domain/definitions.py)
#   815_006  carrier number sequences  (engine/plugins/number_range.py)


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


def open_session() -> Session:
    """A session outside request scope - queue jobs open and commit their
    own. FastAPI routes use the get_session dependency instead."""
    return Session(_engine(get_settings().database_url))
