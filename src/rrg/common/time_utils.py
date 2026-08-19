"""Small date utilities shared across modules that write SCD2 history (closing
a row 'the day before' a new one opens) and rebalance-schedule logic."""
from __future__ import annotations

from datetime import date, timedelta


def day_before(iso_date: str) -> str:
    d = date.fromisoformat(iso_date)
    return (d - timedelta(days=1)).isoformat()


def period_key(iso_date: str, frequency: str) -> tuple:
    """Maps a date to a comparable "rebalance period" key for a given frequency.
    Two dates sharing the same key belong to the same rebalance period (e.g. the
    same calendar quarter for QUARTERLY) -- used to detect period boundaries
    without hardcoding actual rebalance calendar dates."""
    d = date.fromisoformat(iso_date)
    frequency = (frequency or "QUARTERLY").upper()
    if frequency == "DAILY":
        return (d.year, d.month, d.day)
    if frequency == "MONTHLY":
        return (d.year, d.month)
    if frequency == "QUARTERLY":
        return (d.year, (d.month - 1) // 3)
    if frequency == "ANNUALLY":
        return (d.year,)
    # Unknown frequency: fail safe to quarterly rather than raising, since an
    # index misconfiguration here shouldn't halt the whole pipeline -- the
    # Validation Framework's config checks are the right place to catch a typo
    # in rebalance_frequency, not this scheduling helper.
    return (d.year, (d.month - 1) // 3)
