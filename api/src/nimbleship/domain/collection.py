"""Collection-day arithmetic for a Warehouse calendar.

A Warehouse (CONTEXT.md: a logical dispatch identity) carries collection
days and holidays; dispatch dates must land on a day a carrier actually
collects. Pure functions only - the ORM rows are mapped into these shapes
at the edge. Cutoff times are out of scope until delivery dates
arrive."""

from collections.abc import Collection
from datetime import date, timedelta

from pydantic import BaseModel

# A holiday-saturated calendar must fail loudly, never spin or guess:
# no real warehouse closes for a whole year, and silently scheduling
# collection on a non-collection day is worse than an error.
_SCAN_LIMIT_DAYS = 366


class CollectionDays(BaseModel):
    """Weekday collection flags, one set per Warehouse; collection
    defaults to weekdays (Monday to Friday)."""

    monday: bool = True
    tuesday: bool = True
    wednesday: bool = True
    thursday: bool = True
    friday: bool = True
    saturday: bool = False
    sunday: bool = False

    def iso_weekdays(self) -> frozenset[int]:
        """The enabled days as ISO weekday numbers (1=Monday .. 7=Sunday)."""
        flags = (
            self.monday,
            self.tuesday,
            self.wednesday,
            self.thursday,
            self.friday,
            self.saturday,
            self.sunday,
        )
        return frozenset(iso for iso, flag in enumerate(flags, start=1) if flag)


def next_collection_day(
    days: CollectionDays, holidays: Collection[date], on_or_after: date
) -> date:
    """The first date on or after `on_or_after` that falls on an enabled
    collection weekday and is not a holiday.

    Raises ValueError when no day qualifies: a calendar with every weekday
    disabled, or holidays covering more than a year ahead - both are
    configuration mistakes that must surface, not schedule silently."""
    weekdays = days.iso_weekdays()
    if not weekdays:
        raise ValueError("warehouse has no collection days enabled")
    holiday_set = frozenset(holidays)
    candidate = on_or_after
    for _ in range(_SCAN_LIMIT_DAYS):
        if candidate.isoweekday() in weekdays and candidate not in holiday_set:
            return candidate
        candidate += timedelta(days=1)
    raise ValueError(f"no collection day within a year of {on_or_after.isoformat()}")
