import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import numpy as np
import pandas as pd

from rrg.rrg_engine.engine import (
    compute_direction_and_speed,
    compute_quadrant,
    compute_rrg_coordinates,
    compute_rs_momentum,
    compute_rs_ratio,
)


def test_quadrant_boundary_cases():
    assert compute_quadrant(101, 101) == "LEADING"
    assert compute_quadrant(101, 99) == "WEAKENING"
    assert compute_quadrant(99, 99) == "LAGGING"
    assert compute_quadrant(99, 101) == "IMPROVING"
    # Exactly on the center line: >=100 counts as the "strong" side by definition
    assert compute_quadrant(100, 100) == "LEADING"
    assert compute_quadrant(100, 99) == "WEAKENING"


def test_index_identical_to_benchmark_gives_constant_rs_ratio_100():
    """Regression guard for the zero-variance edge case: if an index IS its own
    benchmark, relative strength never deviates from its own average, so
    RS-Ratio should sit exactly at 100 (the "no variance" case), not NaN/inf
    from a division by zero."""
    close = pd.Series(np.linspace(100, 150, 40))  # identical trending series
    rs_ratio = compute_rs_ratio(close, close, window=10)
    valid = rs_ratio.dropna()
    assert len(valid) > 0
    assert (valid == 100).all(), f"expected constant 100, got {valid.unique()}"


def test_rs_ratio_hand_calculation():
    """Hand-verified worked example: a 5-bar window where the relative series
    is [1.0, 1.0, 1.0, 1.0, 1.5] -- mean=1.1, std (sample, ddof=1) computed by
    hand, and the z-score-derived RS-Ratio checked against that by hand (using
    the engine's default scale_factor=10)."""
    index_close = pd.Series([100.0, 100.0, 100.0, 100.0, 150.0])
    benchmark_close = pd.Series([100.0, 100.0, 100.0, 100.0, 100.0])
    relative = index_close / benchmark_close  # [1.0, 1.0, 1.0, 1.0, 1.5]

    rs_ratio = compute_rs_ratio(index_close, benchmark_close, window=5)
    last = rs_ratio.iloc[-1]

    mean = relative.mean()
    std = relative.std()  # pandas default ddof=1, matches implementation
    expected = 100 + 10 * (relative.iloc[-1] - mean) / std

    assert abs(last - expected) < 1e-9
    # Sanity: the last bar's relative value is above the window's own mean,
    # so RS-Ratio must be above 100.
    assert last > 100


def test_rs_momentum_hand_calculation():
    """RS-Momentum with a strictly increasing RS-Ratio series should read
    above 100 (accelerating) at the point of maximum recent acceleration, and
    the z-score formula is checked by hand for one point (default scale_factor=10)."""
    rs_ratio = pd.Series([100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 113.0, 116.0])
    rs_momentum = compute_rs_momentum(rs_ratio, roc_period=2, window=5)

    roc = rs_ratio / rs_ratio.shift(2) - 1
    mean = roc.rolling(5, min_periods=5).mean().iloc[-1]
    std = roc.rolling(5, min_periods=5).std().iloc[-1]
    expected_last = 100 + 10 * (roc.iloc[-1] - mean) / std

    assert abs(rs_momentum.iloc[-1] - expected_last) < 1e-9


def test_scale_factor_keeps_routine_zscores_in_a_realistic_band():
    """Regression test for a real calibration bug caught during development:
    scale_factor=100 turned a routine z-score of ~1.3 into RS-Ratio=-33 (miles
    outside how RRG charts are meant to read). With the corrected default
    scale_factor=10, a moderate z-score should read within a realistic band."""
    s = pd.Series([100.0] * 9 + [113.0])
    b = pd.Series([100.0] * 10)
    rs_ratio = compute_rs_ratio(s, b, window=10, scale_factor=10)
    last = rs_ratio.dropna().iloc[-1]
    assert 70 < last < 130, f"RS-Ratio {last} outside the realistic practical band"


def test_direction_and_speed_cardinal_directions():
    """Hand-verified: a pure +1 move on RS-Ratio with no RS-Momentum change is
    due East (0 deg); a pure +1 move on RS-Momentum with no RS-Ratio change is
    due North (90 deg). Speed is the Euclidean distance moved."""
    rs_ratio = pd.Series([100.0, 101.0, 101.0])
    rs_momentum = pd.Series([100.0, 100.0, 101.0])
    direction, speed = compute_direction_and_speed(rs_ratio, rs_momentum)

    assert abs(direction.iloc[1] - 0.0) < 1e-6      # East: ratio +1, momentum +0
    assert abs(direction.iloc[2] - 90.0) < 1e-6      # North: ratio +0, momentum +1
    assert abs(speed.iloc[1] - 1.0) < 1e-9
    assert abs(speed.iloc[2] - 1.0) < 1e-9


def test_compute_rrg_coordinates_end_to_end_outperformer_lands_in_leading():
    """An index whose outperformance is ACCELERATING (not just constant) should
    settle into the LEADING quadrant: RS-Ratio > 100 from sustained
    outperformance, and RS-Momentum > 100 because the rate of outperformance
    is itself still increasing. (A constant, non-accelerating compounding rate
    would correctly show RS-Momentum ~= 100 -- momentum measures acceleration,
    not the mere presence of outperformance -- so the daily return here is
    deliberately ramped up over time rather than held constant.)"""
    n = 60
    dates = pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    daily_returns = 0.002 + 0.00025 * np.arange(n)  # accelerating daily return
    index_close = 100 * np.cumprod(1 + daily_returns)
    benchmark_close = np.full(n, 100.0)

    index_ohlc = pd.DataFrame({"date": dates, "close": index_close})
    benchmark_ohlc = pd.DataFrame({"date": dates, "close": benchmark_close})

    coords = compute_rrg_coordinates(
        index_ohlc, benchmark_ohlc,
        rs_ratio_window=14, rs_momentum_roc_period=5, rs_momentum_window=14, min_history_bars=20,
    )
    assert not coords.empty
    last = coords.iloc[-1]
    assert last["quadrant"] == "LEADING"
    assert last["rs_ratio"] > 100
    assert last["rs_momentum"] > 100


def test_compute_rrg_coordinates_constant_rate_outperformer_has_flat_momentum():
    """Complementary case to the above: a CONSTANT (non-accelerating) rate of
    outperformance should show RS-Ratio > 100 (it IS outperforming) but
    RS-Momentum settling at ~100 (that outperformance isn't accelerating or
    decelerating, once past the initial transient) -- this is the correct
    mathematical behavior of momentum-as-acceleration, not a bug."""
    n = 60
    dates = pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    index_close = 100 * (1.005 ** np.arange(n))
    benchmark_close = np.full(n, 100.0)

    index_ohlc = pd.DataFrame({"date": dates, "close": index_close})
    benchmark_ohlc = pd.DataFrame({"date": dates, "close": benchmark_close})

    coords = compute_rrg_coordinates(
        index_ohlc, benchmark_ohlc,
        rs_ratio_window=14, rs_momentum_roc_period=5, rs_momentum_window=14, min_history_bars=20,
    )
    last = coords.iloc[-1]
    assert last["rs_ratio"] > 100
    assert abs(last["rs_momentum"] - 100) < 1.0


def test_compute_rrg_coordinates_insufficient_history_returns_empty():
    dates = pd.bdate_range("2025-01-01", periods=5).strftime("%Y-%m-%d")
    index_ohlc = pd.DataFrame({"date": dates, "close": [100, 101, 102, 103, 104]})
    benchmark_ohlc = pd.DataFrame({"date": dates, "close": [100, 100, 100, 100, 100]})
    coords = compute_rrg_coordinates(
        index_ohlc, benchmark_ohlc,
        rs_ratio_window=14, rs_momentum_roc_period=5, rs_momentum_window=14, min_history_bars=20,
    )
    assert coords.empty


def test_compute_rrg_coordinates_misaligned_dates_only_uses_common_dates():
    """If the index and benchmark have different date coverage (e.g. one has a
    missing day), only genuinely overlapping dates should be used -- silently
    forward-filling or misaligning dates would corrupt the ratio."""
    index_dates = pd.bdate_range("2025-01-01", periods=30).strftime("%Y-%m-%d").tolist()
    benchmark_dates = index_dates[:-3]  # benchmark missing the last 3 days
    index_ohlc = pd.DataFrame({"date": index_dates, "close": np.linspace(100, 130, 30)})
    benchmark_ohlc = pd.DataFrame({"date": benchmark_dates, "close": np.full(27, 100.0)})

    coords = compute_rrg_coordinates(
        index_ohlc, benchmark_ohlc,
        rs_ratio_window=10, rs_momentum_roc_period=3, rs_momentum_window=10, min_history_bars=15,
    )
    assert coords["date"].max() == benchmark_dates[-1]
    assert coords["date"].max() != index_dates[-1]
