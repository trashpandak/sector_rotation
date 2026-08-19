import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pandas as pd

from rrg.custom_index.weighting import EqualWeight, MarketCapWeight
from rrg.timeframe.resample import resample_ohlc


def test_equal_weight_sums_to_one():
    w = EqualWeight().compute_weights([1, 2, 3, 4], {}, {})
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_market_cap_weight_respects_cap():
    ids = [1, 2, 3, 4, 5]
    caps = {1: 10, 2: 10, 3: 10, 4: 10, 5: 960}
    w = MarketCapWeight().compute_weights(ids, caps, {"cap_single_constituent_pct": 30})
    assert all(v <= 0.30 + 1e-9 for v in w.values())
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_market_cap_weight_falls_back_to_uncapped_when_cap_is_infeasible():
    """Regression test for a real issue caught during pipeline testing: a
    2-constituent index with a 30% single-constituent cap is mathematically
    infeasible (2 * 0.30 = 0.60 < 1.0). Rather than leaving 40% of the index
    unallocated (weights summing to 0.6), the cap should be skipped and plain
    proportional market-cap weights used -- they already sum to 1.0, and a
    cap intended to prevent one stock dominating a LARGE pool isn't really
    applicable to a 2-stock index anyway. Caps chosen so the uncapped weight
    (90%) is clearly, unambiguously above the 30% cap -- proving the cap was
    genuinely skipped, not coincidentally satisfied."""
    ids = [1, 2]
    caps = {1: 900.0, 2: 100.0}
    w = MarketCapWeight().compute_weights(ids, caps, {"cap_single_constituent_pct": 30})
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert abs(w[1] - 0.9) < 1e-9
    assert abs(w[2] - 0.1) < 1e-9


def test_market_cap_weight_with_one_constituent_missing_data_still_sums_to_one():
    """Regression test for a REAL production bug (not a synthetic edge case):
    a live pipeline run had a 4-constituent IT_SERVICES index where one
    constituent's price fetch failed that day (a documented yfinance
    rate-limiting issue -- see data_acquisition/sources.py), giving it a
    market cap of exactly 0. The naive feasibility check counted all 4
    constituents (4 * 30% = 120% >= 100%, "feasible"), so it attempted normal
    capping+redistribution -- but the zero-cap constituent could never
    receive any of the redistributed excess (proportional-to-zero is zero),
    and the redistribution loop broke early, leaving weights summing to 0.9
    instead of 1.0. The fix must produce weights summing to exactly 1.0
    regardless of which constituent has zero data that day."""
    ids = [1, 2, 3, 4]
    # Three large, roughly-equal caps (each individually > 30% before
    # capping) and one with NO market cap data at all (simulating a failed
    # price fetch) -- the exact production shape that broke the old logic.
    caps = {1: 400.0, 2: 350.0, 3: 250.0, 4: 0.0}
    w = MarketCapWeight().compute_weights(ids, caps, {"cap_single_constituent_pct": 30})
    assert abs(sum(w.values()) - 1.0) < 1e-9, f"weights {w} must sum to 1.0, not silently lose the cap excess"
    assert w[4] == 0.0  # the missing-data constituent still correctly gets zero, not a fabricated share



def test_weekly_resample_ohlc_matches_hand_calculation():
    # Two full weeks of daily bars, Mon-Fri
    dates = pd.bdate_range("2025-01-06", periods=10)  # 2025-01-06 (Mon) .. 2025-01-17 (Fri)
    df = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open":  [10, 11, 12, 13, 14, 20, 21, 22, 23, 24],
        "high":  [15, 15, 15, 15, 15, 25, 25, 25, 25, 25],
        "low":   [ 5,  5,  5,  5,  5, 15, 15, 15, 15, 15],
        "close": [11, 12, 13, 14, 15, 21, 22, 23, 24, 25],
    })
    weekly = resample_ohlc(df, "WEEKLY")
    assert len(weekly) == 2
    # Week 1: open = Monday's open (10), close = Friday's close (15), high/low = week's max/min
    assert weekly.iloc[0]["open"] == 10
    assert weekly.iloc[0]["close"] == 15
    assert weekly.iloc[0]["high"] == 15
    assert weekly.iloc[0]["low"] == 5
    # Week 2
    assert weekly.iloc[1]["open"] == 20
    assert weekly.iloc[1]["close"] == 25


def test_monthly_resample_ohlc_matches_hand_calculation():
    dates = pd.bdate_range("2025-01-01", "2025-02-28")
    df = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": range(len(dates)), "high": range(len(dates)),
        "low": range(len(dates)), "close": range(len(dates)),
    })
    monthly = resample_ohlc(df, "MONTHLY")
    assert len(monthly) == 2  # January, February
    assert monthly.iloc[0]["open"] == 0  # first trading day of January
    assert monthly.iloc[-1]["date"].startswith("2025-02")


def test_daily_resample_is_a_passthrough():
    df = pd.DataFrame({"date": ["2025-01-01", "2025-01-02"], "open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1, 2]})
    out = resample_ohlc(df, "DAILY")
    pd.testing.assert_frame_equal(out.reset_index(drop=True), df.reset_index(drop=True))
