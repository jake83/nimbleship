"""The Shipping Area resolver: destination postcode + country -> area codes,
by longest-prefix matching over the postcode_areas table (a port of the old
system's getBlockedHauliersForPostcode single-query optimisation)."""

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from nimbleship.db import Base
from nimbleship.domain.geography import resolve_shipping_areas
from nimbleship.models import PostcodeArea, ShippingArea


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session
    engine.dispose()


def area(session: Session, code: str, prefixes: list[str], country: str = "GB") -> None:
    row = ShippingArea(code=code, name=code.title(), country=country)
    row.prefixes = [PostcodeArea(prefix=prefix) for prefix in prefixes]
    session.add(row)
    session.flush()


def test_postcode_matching_a_prefix_resolves_to_its_area(session: Session) -> None:
    area(session, "HIGHLANDS", ["IV", "KW"])

    assert resolve_shipping_areas(session, "IV1 2AB", "GB") == ["HIGHLANDS"]


def test_postcode_matching_nothing_resolves_to_no_areas(session: Session) -> None:
    area(session, "HIGHLANDS", ["IV"])

    assert resolve_shipping_areas(session, "SW1A 2AA", "GB") == []


def test_longest_matching_prefix_wins(session: Session) -> None:
    area(session, "HIGHLANDS", ["IV"])
    area(session, "INVERNESS-CITY", ["IV1"])

    assert resolve_shipping_areas(session, "IV1 2AB", "GB") == ["INVERNESS-CITY"]
    assert resolve_shipping_areas(session, "IV63 6TU", "GB") == ["HIGHLANDS"]


def test_areas_sharing_the_longest_prefix_are_all_returned(session: Session) -> None:
    area(session, "NORTHERN-IRELAND", ["BT"])
    area(session, "TWO-MAN-EXCLUSION", ["BT"])

    assert resolve_shipping_areas(session, "BT1 5GS", "GB") == [
        "NORTHERN-IRELAND",
        "TWO-MAN-EXCLUSION",
    ]


def test_areas_in_another_country_do_not_match(session: Session) -> None:
    area(session, "DUBLIN", ["D"], country="IE")

    assert resolve_shipping_areas(session, "D02 X285", "GB") == []


def test_postcode_matching_is_case_and_whitespace_insensitive(
    session: Session,
) -> None:
    area(session, "HIGHLANDS", ["IV"])

    assert resolve_shipping_areas(session, "  iv1 2ab ", "GB") == ["HIGHLANDS"]


def test_country_matching_is_case_insensitive(session: Session) -> None:
    area(session, "HIGHLANDS", ["IV"])

    assert resolve_shipping_areas(session, "IV1 2AB", "gb") == ["HIGHLANDS"]


def test_blank_postcode_resolves_to_no_areas(session: Session) -> None:
    area(session, "HIGHLANDS", ["IV"])

    assert resolve_shipping_areas(session, "   ", "GB") == []


def test_prefix_longer_than_the_postcode_does_not_match(session: Session) -> None:
    area(session, "INVERNESS-CITY", ["IV1 2ABX"])

    assert resolve_shipping_areas(session, "IV1 2AB", "GB") == []
