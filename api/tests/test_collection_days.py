"""The next-collection-day helper: pure calendar arithmetic over a
Warehouse's collection days and holidays (CONTEXT.md: a Warehouse carries
collection days and holidays)."""

from datetime import date

import pytest

from nimbleship.domain.collection import CollectionDays, next_collection_day

# Anchors (checked against a real calendar):
# 2026-07-13 is a Monday; 2026-12-31 is a Thursday; 2027-01-04 is a Monday.
MONDAY = date(2026, 7, 13)

WEEKDAYS_ONLY = CollectionDays()


def test_defaults_are_monday_to_friday() -> None:
    days = CollectionDays()

    assert days.iso_weekdays() == frozenset({1, 2, 3, 4, 5})


def test_a_collection_day_with_no_holiday_is_returned_unchanged() -> None:
    assert next_collection_day(WEEKDAYS_ONLY, [], MONDAY) == MONDAY


def test_a_non_collection_day_rolls_to_the_next_enabled_weekday() -> None:
    saturday = date(2026, 7, 18)

    assert next_collection_day(WEEKDAYS_ONLY, [], saturday) == date(2026, 7, 20)


def test_a_holiday_rolls_to_the_next_working_day() -> None:
    assert next_collection_day(WEEKDAYS_ONLY, [MONDAY], MONDAY) == date(2026, 7, 14)


def test_consecutive_holidays_before_a_weekend_roll_past_both() -> None:
    thursday = date(2026, 7, 16)
    friday = date(2026, 7, 17)

    result = next_collection_day(WEEKDAYS_ONLY, [thursday, friday], thursday)

    assert result == date(2026, 7, 20)  # the following Monday


def test_sparse_collection_days_skip_disabled_weekdays() -> None:
    tuesdays_only = CollectionDays(
        monday=False, wednesday=False, thursday=False, friday=False
    )

    result = next_collection_day(tuesdays_only, [], MONDAY)

    assert result == date(2026, 7, 14)


def test_year_boundary_holidays_roll_into_the_new_year() -> None:
    new_years_eve = date(2026, 12, 31)  # a Thursday
    new_years_day = date(2027, 1, 1)  # a Friday

    result = next_collection_day(
        WEEKDAYS_ONLY, [new_years_eve, new_years_day], new_years_eve
    )

    assert result == date(2027, 1, 4)  # the first Monday of 2027


def test_a_saturday_collection_warehouse_can_collect_on_saturday() -> None:
    six_days = CollectionDays(saturday=True)
    saturday = date(2026, 7, 18)

    assert next_collection_day(six_days, [], saturday) == saturday


def test_no_enabled_collection_days_is_loud() -> None:
    never = CollectionDays(
        monday=False,
        tuesday=False,
        wednesday=False,
        thursday=False,
        friday=False,
    )

    with pytest.raises(ValueError, match="no collection days"):
        next_collection_day(never, [], MONDAY)


def test_a_solid_year_of_holidays_is_loud_not_an_infinite_loop() -> None:
    a_year_of_holidays = [
        date.fromordinal(MONDAY.toordinal() + offset) for offset in range(400)
    ]

    with pytest.raises(ValueError, match="within a year"):
        next_collection_day(WEEKDAYS_ONLY, a_year_of_holidays, MONDAY)


def test_holidays_outside_the_scan_path_are_ignored() -> None:
    result = next_collection_day(WEEKDAYS_ONLY, [date(2026, 12, 25)], MONDAY)

    assert result == MONDAY
