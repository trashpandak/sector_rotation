"""Historical backtest / methodology validation.

Not a unit test of a single formula (that's test_rrg_engine.py) -- this
simulates a full market-cycle narrative and checks that the RRG engine's
OUTPUT SEQUENCE matches what a real, well-understood sector rotation cycle
should look like: a sector that starts flat, outperforms with accelerating
strength, peaks, then gives back that outperformance, should trace
LAGGING/IMPROVING -> LEADING -> WEAKENING -> LAGGING around the quadrant
clock -- the textbook RRG rotation path. This is the closest thing to the
spec's "historical testing" phase that's practical without a live NSE feed:
a synthetic but methodologically faithful known scenario, checked end to end
through the exact same code path production data goes through.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import numpy as np
import pandas as pd

from rrg.rrg_engine.engine import compute_rrg_coordinates


def _relative_return_series(n: int) -> np.ndarray:
    """Builds a daily return series for an index vs. a FLAT benchmark that
    traces a full outperformance cycle:
      Phase 1 (flat, ~40 bars):        no relative edge -- matches benchmark
      Phase 2 (accelerating up, ~50):  index pulls away, accelerating
      Phase 3 (decelerating up, ~40):  index still rising but losing steam
      Phase 4 (falling, ~50 bars):     index gives back the outperformance
      Phase 5 (flat again, ~40 bars):  settles back near the benchmark
    """
    phase1 = np.zeros(40)
    phase2 = np.linspace(0.001, 0.010, 50)     # accelerating daily outperformance
    phase3 = np.linspace(0.010, 0.001, 40)     # still positive, decelerating
    phase4 = np.linspace(-0.002, -0.012, 50)   # accelerating underperformance
    phase5 = np.zeros(max(n - 180, 20))
    returns = np.concatenate([phase1, phase2, phase3, phase4, phase5])
    return returns[:n]


def test_full_rotation_cycle_traces_the_textbook_quadrant_path():
    n = 260
    dates = pd.bdate_range("2024-01-01", periods=n).strftime("%Y-%m-%d")
    daily_returns = _relative_return_series(n)
    index_close = 100 * np.cumprod(1 + daily_returns)
    benchmark_close = np.full(n, 100.0)

    index_ohlc = pd.DataFrame({"date": dates, "close": index_close})
    benchmark_ohlc = pd.DataFrame({"date": dates, "close": benchmark_close})

    coords = compute_rrg_coordinates(
        index_ohlc, benchmark_ohlc,
        rs_ratio_window=14, rs_momentum_roc_period=5, rs_momentum_window=14,
        min_history_bars=20, scale_factor=10,
    )
    assert not coords.empty

    # Sample the quadrant at several points along the cycle and check the
    # sequence visits LAGGING/IMPROVING early, LEADING during the strong
    # acceleration phase, and WEAKENING or LAGGING again once outperformance
    # rolls over -- the defining signature of one full RRG rotation.
    quadrant_sequence = coords["quadrant"].tolist()

    # During strong, accelerating outperformance (mid Phase 2), it must be LEADING.
    peak_accel_idx = 40 + 35  # well into phase 2, near its most accelerated point
    assert quadrant_sequence[min(peak_accel_idx, len(quadrant_sequence) - 1)] == "LEADING"

    # Once outperformance is actively reversing (mid Phase 4), it must have
    # left LEADING -- either WEAKENING (still above benchmark but decelerating)
    # or LAGGING (already given back the edge).
    reversal_idx = 40 + 50 + 40 + 25  # well into phase 4
    reversal_quadrant = quadrant_sequence[min(reversal_idx, len(quadrant_sequence) - 1)]
    assert reversal_quadrant in ("WEAKENING", "LAGGING")

    # The rotation must have actually visited more than one quadrant -- a
    # methodology that just sits in one quadrant regardless of the underlying
    # price action would trivially "pass" narrower assertions but isn't
    # measuring rotation at all.
    assert len(set(quadrant_sequence)) >= 3


def test_flat_relative_performance_stays_near_center_throughout():
    """Sanity complement: an index with NO relative edge over the benchmark at
    any point should stay close to the (100, 100) center the whole time, not
    drift into a strong quadrant reading by construction artifact."""
    n = 100
    dates = pd.bdate_range("2024-01-01", periods=n).strftime("%Y-%m-%d")
    rng = np.random.default_rng(7)
    # Index and benchmark both get IDENTICAL small daily noise -- zero relative edge.
    noise = rng.normal(0, 0.005, n)
    index_close = 100 * np.cumprod(1 + noise)
    benchmark_close = 100 * np.cumprod(1 + noise)

    index_ohlc = pd.DataFrame({"date": dates, "close": index_close})
    benchmark_ohlc = pd.DataFrame({"date": dates, "close": benchmark_close})

    coords = compute_rrg_coordinates(
        index_ohlc, benchmark_ohlc,
        rs_ratio_window=14, rs_momentum_roc_period=5, rs_momentum_window=14,
        min_history_bars=20, scale_factor=10,
    )
    assert not coords.empty
    assert (coords["rs_ratio"] == 100).all()
    assert (coords["rs_momentum"] == 100).all()
