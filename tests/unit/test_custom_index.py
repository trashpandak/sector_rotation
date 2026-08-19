import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import sqlite3

import pytest

from rrg.storage.db import get_connection
from rrg.custom_index.construction import (
    get_cap_band,
    resolve_constituents,
    construct_custom_index,
    compute_index_ohlc,
    is_rebalance_due,
    _market_caps,
)

BANDS = [
    {"code": "LARGE", "min_mcap_cr": 20000},
    {"code": "MID", "min_mcap_cr": 5000, "max_mcap_cr": 20000},
    {"code": "SMALL", "min_mcap_cr": 500, "max_mcap_cr": 5000},
    {"code": "MICRO", "max_mcap_cr": 500},
]


def test_cap_band_boundaries():
    assert get_cap_band(50000, BANDS) == "LARGE"
    assert get_cap_band(20000, BANDS) == "LARGE"       # inclusive lower bound
    assert get_cap_band(19999, BANDS) == "MID"
    assert get_cap_band(5000, BANDS) == "MID"
    assert get_cap_band(4999, BANDS) == "SMALL"
    assert get_cap_band(500, BANDS) == "SMALL"
    assert get_cap_band(499, BANDS) == "MICRO"
    assert get_cap_band(0, BANDS) == "MICRO"


@pytest.fixture()
def conn(tmp_path):
    return get_connection(tmp_path / "test.db")


def _insert_instrument(conn: sqlite3.Connection, symbol: str, sector: str, universes: str, shares: float) -> int:
    conn.execute(
        "INSERT INTO instruments (symbol, name, sector, shares_outstanding_cr, nifty_universes) VALUES (?, ?, ?, ?, ?)",
        (symbol, symbol, sector, shares, universes),
    )
    conn.commit()
    return conn.execute("SELECT instrument_id FROM instruments WHERE symbol=?", (symbol,)).fetchone()[0]


def _insert_price(conn: sqlite3.Connection, iid: int, date: str, close: float) -> None:
    conn.execute(
        "INSERT INTO instrument_price_history (instrument_id, date, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, 1000)",
        (iid, date, close, close, close, close),
    )
    conn.commit()


def test_resolve_constituents_respects_universe_filter(conn):
    """Also a regression test for a real substring-matching bug caught during
    development: an earlier version used `LIKE '%NIFTY50%'`, which also
    matches 'NIFTY500' (NIFTY50 is a literal substring of NIFTY500) --
    silently pulling Nifty-500-only stocks into a Nifty 50 index. BBB below
    is deliberately NIFTY500-only and must NOT appear in the NIFTY50 result."""
    id_a = _insert_instrument(conn, "AAA", "BANKS", "NIFTY50|NIFTY500", 100)
    id_b = _insert_instrument(conn, "BBB", "BANKS", "NIFTY500", 50)  # not in NIFTY50

    nifty50_members = resolve_constituents(conn, "BANKS", None, "NIFTY50")
    nifty500_members = resolve_constituents(conn, "BANKS", None, "NIFTY500")

    assert nifty50_members == [id_a]
    assert sorted(nifty500_members) == sorted([id_a, id_b])


def test_rebalance_lock_across_universe_suffixed_index(conn):
    """Same regression scenario as the sibling Market Intelligence Engine
    project, ported here: a market-cap-weighted custom index must NOT
    silently re-weight itself mid-quarter even under an extreme price move."""
    conn.execute(
        "INSERT INTO indices (code, name, category, weighting_method, universe_code, rebalance_frequency) "
        "VALUES ('CUSTOM_TEST__NIFTY50', 'Custom Test', 'CUSTOM', 'MCAP', 'NIFTY50', 'QUARTERLY')"
    )
    conn.commit()
    id_a = _insert_instrument(conn, "AAA", "TESTSECTOR", "NIFTY50", 10.0)
    id_b = _insert_instrument(conn, "BBB", "TESTSECTOR", "NIFTY50", 10.0)
    _insert_price(conn, id_a, "2026-01-05", 100)
    _insert_price(conn, id_b, "2026-01-05", 100)

    index_def = {
        "code": "CUSTOM_TEST__NIFTY50", "sector_filter": "TESTSECTOR", "industry_filter": None,
        "universe_code": "NIFTY50", "weighting_method": "MCAP", "rebalance_frequency": "QUARTERLY",
    }
    result1 = construct_custom_index(conn, index_def, {}, "2026-01-05")
    assert result1["rebalanced"] is True
    w1 = dict(conn.execute(
        "SELECT instrument_id, weight FROM custom_index_constituent_history WHERE index_code=? AND is_current=1",
        ("CUSTOM_TEST__NIFTY50",),
    ).fetchall())
    assert abs(w1[id_a] - 0.5) < 1e-9

    # Mid-quarter (still Q1), A's price explodes 10x
    _insert_price(conn, id_a, "2026-02-10", 1000)
    _insert_price(conn, id_b, "2026-02-10", 100)
    result2 = construct_custom_index(conn, index_def, {}, "2026-02-10")
    assert result2["rebalanced"] is False
    w2 = dict(conn.execute(
        "SELECT instrument_id, weight FROM custom_index_constituent_history WHERE index_code=? AND is_current=1",
        ("CUSTOM_TEST__NIFTY50",),
    ).fetchall())
    assert w2 == w1, "weights must stay locked within the same quarter"

    # Q2: rebalance should now pick up the new prices
    _insert_price(conn, id_a, "2026-04-01", 1000)
    _insert_price(conn, id_b, "2026-04-01", 100)
    result3 = construct_custom_index(conn, index_def, {}, "2026-04-01")
    assert result3["rebalanced"] is True
    w3 = dict(conn.execute(
        "SELECT instrument_id, weight FROM custom_index_constituent_history WHERE index_code=? AND is_current=1",
        ("CUSTOM_TEST__NIFTY50",),
    ).fetchall())
    assert w3[id_a] > w3[id_b]


def test_market_caps_use_latest_price_on_or_before_as_of_date(conn):
    """Regression test for a real bug caught during development: a non-trading
    day (e.g. weekend) as_of_date used to return NO market cap at all (exact
    date match only), silently degrading MCAP weighting to Equal Weight and
    starving capitalization-band sub-indices of data. A price on the last
    trading day before as_of_date must still be found."""
    id_a = _insert_instrument(conn, "AAA", "TESTSECTOR", "NIFTY50", 10.0)
    _insert_price(conn, id_a, "2026-08-14", 500.0)  # last trading day (Friday)
    # 2026-08-16 (Sunday) has no price row at all
    caps = _market_caps(conn, [id_a], "2026-08-16")
    assert caps[id_a] == 10.0 * 500.0


def test_ohlc_uses_weight_segment_in_effect_on_each_historical_date(conn):
    conn.execute(
        "INSERT INTO indices (code, name, category, weighting_method) VALUES ('CUSTOM_SEG', 'Seg Test', 'CUSTOM', 'MCAP')"
    )
    conn.commit()
    id_a = _insert_instrument(conn, "AAA", "TESTSECTOR", "NIFTY50", 10.0)
    id_b = _insert_instrument(conn, "BBB", "TESTSECTOR", "NIFTY50", 10.0)

    a_closes = [100.0, 102.0, 104.04, 106.1208]
    b_closes = [100.0, 100.0, 100.0, 100.0]
    for i, d in enumerate(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]):
        _insert_price(conn, id_a, d, a_closes[i])
        _insert_price(conn, id_b, d, b_closes[i])

    # Segment 1 (days 1-2): 100% weight on B (flat)
    conn.execute(
        "INSERT INTO custom_index_constituent_history (index_code, instrument_id, weight, effective_from, is_current) "
        "VALUES ('CUSTOM_SEG', ?, 1.0, '2026-01-01', 0)", (id_b,),
    )
    conn.execute("UPDATE custom_index_constituent_history SET effective_to='2026-01-02' WHERE index_code='CUSTOM_SEG'")
    # Segment 2 (days 3-4): 100% weight on A (rising)
    conn.execute(
        "INSERT INTO custom_index_constituent_history (index_code, instrument_id, weight, effective_from, is_current) "
        "VALUES ('CUSTOM_SEG', ?, 1.0, '2026-01-03', 1)", (id_a,),
    )
    conn.commit()

    ohlc = compute_index_ohlc(conn, "CUSTOM_SEG", base_value=1000.0).sort_values("date")
    closes = ohlc["close"].tolist()
    assert abs(closes[0] - 1000.0) < 0.01
    assert abs(closes[1] - 1000.0) < 0.01
    assert closes[2] > closes[1]
    assert closes[3] > closes[2]
