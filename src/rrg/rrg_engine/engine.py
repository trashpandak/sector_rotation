"""RRG Engine -- the core relative-rotation mathematics.

This is an ORIGINAL, fully transparent implementation. It is conceptually in
the same family as the well-known JdK RRG framework (relative strength vs. a
benchmark, normalized and plotted as a rotating quadrant chart) but uses its
own, simpler, explicitly documented formulas below -- it does not reproduce
any proprietary JdK formula or constants. Every number here can be
hand-verified from the formulas in this docstring; see
tests/unit/test_rrg_engine.py for worked examples.

================================================================================
STEP 1 -- Relative Strength (raw)
================================================================================
    relative_t = index_close_t / benchmark_close_t

The raw ratio of the index's price to the benchmark's price, on whichever
timeframe's bars are supplied (daily/weekly/monthly -- see timeframe/resample.py).

================================================================================
STEP 2 -- RS-Ratio (normalized relative strength)
================================================================================
    rs_ratio_t = 100 + S * (relative_t - rolling_mean(relative, W)) / rolling_std(relative, W)

`relative` is rescaled into a rolling z-score, then remapped so a reading of
exactly 100 means "relative strength is at its own W-bar rolling average" --
above 100 means the index is stronger than its own recent-average relative
strength vs. the benchmark, below 100 means weaker. W = rrg.rs_ratio_window
in config (bars of the ACTIVE timeframe -- 14 trading days on Daily, 14 weeks
on Weekly, 14 months on Monthly). S = rrg.scale_factor (default 10): scales
the z-score into a practically-readable range -- a routine z of +-1 becomes
+-10 (RS-Ratio ~90-110), rather than +-100 (RS-Ratio -100 to 300, unusably
wide and not how RRG charts are meant to read). See config/rrg_settings.yaml
for the full reasoning.

================================================================================
STEP 3 -- RS-Momentum (normalized rate of change of RS-Ratio)
================================================================================
    roc_t         = rs_ratio_t / rs_ratio_(t-M) - 1                         (M = rrg.rs_momentum_roc_period)
    rs_momentum_t = 100 + S * (roc_t - rolling_mean(roc, W2)) / rolling_std(roc, W2)

Same normalize-to-100 treatment (same scale factor S) applied to the M-bar
rate of change of RS-Ratio itself, so RS-Momentum answers "is relative
strength accelerating or decelerating," on a comparable numeric scale to
RS-Ratio (both cluster around 100), which is what makes the two work
together as (x, y) coordinates on one chart. W2 = rrg.rs_momentum_window.

================================================================================
STEP 4 -- Quadrant
================================================================================
Center = (100, 100) on both axes (a direct consequence of the normalization
above -- there is no separate "boundary" parameter to tune).

    LEADING:    rs_ratio >= 100  AND  rs_momentum >= 100
    WEAKENING:  rs_ratio >= 100  AND  rs_momentum <  100
    LAGGING:    rs_ratio <  100  AND  rs_momentum <  100
    IMPROVING:  rs_ratio <  100  AND  rs_momentum >= 100

================================================================================
STEP 5 -- Direction and Rotation Speed
================================================================================
    delta_ratio_t    = rs_ratio_t - rs_ratio_(t-1)
    delta_momentum_t = rs_momentum_t - rs_momentum_(t-1)
    direction_deg_t  = degrees(atan2(delta_momentum_t, delta_ratio_t))     [0deg = due East = pure RS-Ratio gain]
    rotation_speed_t = sqrt(delta_ratio_t^2 + delta_momentum_t^2)         [Euclidean distance moved in one bar]

Direction is the compass heading of the point's most recent one-bar move on
the RRG plane (0 deg/East = strengthening only, 90 deg/North = accelerating
only, 180 deg/West = weakening only, 270 deg/South = decelerating only).
Rotation speed is how far it moved -- a fast-rotating sector crosses
quadrants quickly; a slow one drifts.
"""
from __future__ import annotations

import math

import pandas as pd


def _zscore_rescale(series: pd.Series, window: int, scale_factor: float) -> pd.Series:
    mean = series.rolling(window=window, min_periods=window).mean()
    std = series.rolling(window=window, min_periods=window).std()
    z = (series - mean) / std
    # Zero (or near-zero) rolling variance means the series hasn't moved
    # relative to its own recent average -- e.g. an index compared to itself.
    # Without this guard, a std of 0 produces NaN (0/0) or +-inf, which would
    # silently corrupt every downstream RRG coordinate. Treat "no variance" as
    # "exactly at the mean" (z=0) rather than propagating NaN/inf.
    z = z.where(std > 1e-12, 0.0)
    return 100 + scale_factor * z


def compute_rs_ratio(index_close: pd.Series, benchmark_close: pd.Series, window: int, scale_factor: float = 10) -> pd.Series:
    relative = index_close / benchmark_close
    return _zscore_rescale(relative, window, scale_factor)


def compute_rs_momentum(rs_ratio: pd.Series, roc_period: int, window: int, scale_factor: float = 10) -> pd.Series:
    roc = rs_ratio / rs_ratio.shift(roc_period) - 1
    return _zscore_rescale(roc, window, scale_factor)


def compute_quadrant(rs_ratio: float, rs_momentum: float) -> str:
    if rs_ratio >= 100 and rs_momentum >= 100:
        return "LEADING"
    if rs_ratio >= 100 and rs_momentum < 100:
        return "WEAKENING"
    if rs_ratio < 100 and rs_momentum < 100:
        return "LAGGING"
    return "IMPROVING"


def compute_direction_and_speed(rs_ratio: pd.Series, rs_momentum: pd.Series) -> tuple[pd.Series, pd.Series]:
    delta_ratio = rs_ratio.diff()
    delta_momentum = rs_momentum.diff()
    direction = [
        math.degrees(math.atan2(dm, dr)) if pd.notna(dm) and pd.notna(dr) else None
        for dm, dr in zip(delta_momentum, delta_ratio)
    ]
    speed = (delta_ratio ** 2 + delta_momentum ** 2) ** 0.5
    return pd.Series(direction, index=rs_ratio.index), speed


def compute_rrg_coordinates(
    index_ohlc: pd.DataFrame,
    benchmark_ohlc: pd.DataFrame,
    rs_ratio_window: int,
    rs_momentum_roc_period: int,
    rs_momentum_window: int,
    min_history_bars: int,
    scale_factor: float = 10,
) -> pd.DataFrame:
    """index_ohlc / benchmark_ohlc: DataFrames with columns [date, close] (at
    minimum), already resampled to the desired timeframe. Returns a DataFrame
    of [date, rs_ratio, rs_momentum, quadrant, direction_deg, rotation_speed]
    -- rows before `min_history_bars` of valid history are dropped (insufficient
    history to trust the normalization windows yet, not silently returned as
    misleading partial numbers)."""
    merged = pd.merge(
        index_ohlc[["date", "close"]].rename(columns={"close": "index_close"}),
        benchmark_ohlc[["date", "close"]].rename(columns={"close": "benchmark_close"}),
        on="date", how="inner",
    ).sort_values("date").reset_index(drop=True)

    if len(merged) < min_history_bars:
        return pd.DataFrame(columns=["date", "rs_ratio", "rs_momentum", "quadrant", "direction_deg", "rotation_speed"])

    rs_ratio = compute_rs_ratio(merged["index_close"], merged["benchmark_close"], rs_ratio_window, scale_factor)
    rs_momentum = compute_rs_momentum(rs_ratio, rs_momentum_roc_period, rs_momentum_window, scale_factor)
    direction, speed = compute_direction_and_speed(rs_ratio, rs_momentum)

    out = pd.DataFrame({
        "date": merged["date"], "rs_ratio": rs_ratio, "rs_momentum": rs_momentum,
        "direction_deg": direction, "rotation_speed": speed,
    })
    out = out.dropna(subset=["rs_ratio", "rs_momentum"]).reset_index(drop=True)
    out["quadrant"] = [compute_quadrant(r, m) for r, m in zip(out["rs_ratio"], out["rs_momentum"])]
    return out[["date", "rs_ratio", "rs_momentum", "quadrant", "direction_deg", "rotation_speed"]]
