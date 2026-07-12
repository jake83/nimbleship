from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from nimbleship.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache(maxsize=4)
def _engine(database_url: str) -> Engine:
    engine = create_engine(database_url)
    # Schema management via Alembic arrives with Phase 2's first real
    # migration; the walking skeleton creates its schema directly.
    Base.metadata.create_all(engine)
    return engine


def get_session() -> Iterator[Session]:
    with Session(_engine(get_settings().database_url)) as session:
        yield session
        session.commit()
