"""Timeframe Engine.

Per the spec: "Do not simply calculate Daily RRG and visually resample it...
the selected timeframe must affect the underlying calculations." This module
is what makes that true -- it resamples the DAILY OHLC bars themselves into
independent WEEKLY and MONTHLY OHLC bar series first, and the RRG Engine then
runs its normalization windows (rolling means/std devs) over whichever bar
series it's given. A 14-bar rolling window means 14 TRADING DAYS on the daily
timeframe but 14 WEEKS (~3.5 months) on the weekly timeframe -- a materially
different, independently-computed statistic, not a resampled version of the
same underlying daily number.

Weekly bars close Friday (W-FRI); monthly bars close on the last calendar day
of the month (ME). Both are standard equity-market bar conventions.
"""
from __future__ import annotations

import pandas as pd

TIMEFRAME_RULES = {"DAILY": None, "WEEKLY": "W-FRI", "MONTHLY": "ME"}


def resample_ohlc(daily_df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """daily_df: columns [date, open, high, low, close], date as ISO strings."""
    if timeframe == "DAILY":
        return daily_df.copy()

    rule = TIMEFRAME_RULES.get(timeframe)
    if rule is None:
        raise ValueError(f"Unknown timeframe '{timeframe}'")

    df = daily_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    resampled = df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    resampled = resampled.dropna(how="all")
    resampled = resampled.reset_index()
    resampled["date"] = resampled["date"].dt.strftime("%Y-%m-%d")
    return resampled
