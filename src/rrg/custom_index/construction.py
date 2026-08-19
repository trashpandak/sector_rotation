"""Custom Index Construction Engine.

v2 additions over the original: universe-filtered constituents (a given
sector index is built separately per active NIFTY universe, e.g.
CUSTOM_BANKS__NIFTY50 vs CUSTOM_BANKS__NIFTY500), auto-generated
Large/Mid/Small capitalization-band sub-indices where a sector has enough
depth in each band, and proper quarterly-lock rebalancing with SCD2
constituent history (ported from the sibling Market Intelligence Engine
project's already-tested pattern: a rebalance closes the ENTIRE current
weight batch and opens a fresh one sharing one effective_from, and OHLC
computation uses whichever weight snapshot was actually in effect on each
historical date -- a rebalance changes the trajectory going forward without
rewriting the past).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import pandas as pd

from rrg.common.logging_config import get_logger
from rrg.common.time_utils import day_before, period_key
from rrg.custom_index.weighting import get_weighting_method
from rrg.storage.scd2 import current_members

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Capitalization bands
# ---------------------------------------------------------------------------

def get_cap_band(mcap_cr: float, bands_config: List[Dict[str, Any]]) -> Optional[str]:
    for band in bands_config:
        lo = band.get("min_mcap_cr", 0)
        hi = band.get("max_mcap_cr", float("inf"))
        if lo <= mcap_cr < hi:
            return band["code"]
    return None


# ---------------------------------------------------------------------------
# Constituent resolution
# ---------------------------------------------------------------------------

def resolve_constituents(
    conn: sqlite3.Connection, sector_filter: str, industry_filter: Optional[str], universe_code: str,
) -> List[int]:
    """Filters by exact universe-tag membership, NOT a raw SQL LIKE substring
    match -- a real bug caught during testing: `LIKE '%NIFTY50%'` also matches
    'NIFTY500' (NIFTY50 is a literal substring of NIFTY500), silently pulling
    Nifty 500-only stocks into a Nifty 50 index. Membership is pipe-delimited
    per instrument (e.g. "NIFTY50|NIFTY100|NIFTY500"), so filtering is done in
    Python against the exact split token list instead."""
    query = "SELECT instrument_id, nifty_universes FROM instruments WHERE sector=?"
    params: list = [sector_filter]
    if industry_filter:
        query += " AND industry=?"
        params.append(industry_filter)
    rows = conn.execute(query, params).fetchall()
    return [
        r[0] for r in rows
        if r[1] and universe_code in str(r[1]).split("|")
    ]


def _market_caps(conn: sqlite3.Connection, instrument_ids: List[int], as_of_date: str) -> Dict[int, float]:
    """Uses the latest available close price ON OR BEFORE as_of_date, not an
    exact-date match. A real bug caught during testing: an exact-date join
    silently returned NO price (and therefore NO market cap) whenever
    as_of_date fell on a non-trading day (weekend/holiday) -- MarketCapWeight
    then quietly fell back to Equal Weight (its documented behavior for "no
    market cap data available"), and capitalization-band sub-indices got zero
    constituents in every band, for a reason that had nothing to do with
    actual data availability."""
    caps = {}
    for iid in instrument_ids:
        row = conn.execute(
            """SELECT i.shares_outstanding_cr,
                      (SELECT p.close FROM instrument_price_history p
                       WHERE p.instrument_id = i.instrument_id AND p.date <= ?
                       ORDER BY p.date DESC LIMIT 1) AS close
               FROM instruments i WHERE i.instrument_id = ?""",
            (as_of_date, iid),
        ).fetchone()
        if row and row[0] is not None and row[1] is not None:
            caps[iid] = float(row[0]) * float(row[1])
    return caps


# ---------------------------------------------------------------------------
# Index registration
# ---------------------------------------------------------------------------

def register_indices(
    conn: sqlite3.Connection, official_defs: List[Dict], custom_defs: List[Dict],
    benchmarks: List[Dict], active_universes: List[str],
) -> List[Dict[str, Any]]:
    """Registers OFFICIAL indices/benchmarks once, and CUSTOM indices once per
    active universe (code suffixed __{universe}). Returns the expanded list of
    concrete custom index definitions (one per universe) for the pipeline to
    iterate over."""
    for b in benchmarks:
        conn.execute(
            """INSERT INTO indices (code, name, category, weighting_method, is_benchmark)
               VALUES (?, ?, 'OFFICIAL', NULL, 1)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name, is_benchmark=1""",
            (b["code"], b["name"]),
        )
    for idx in official_defs:
        conn.execute(
            """INSERT INTO indices (code, name, category, weighting_method, is_benchmark)
               VALUES (?, ?, 'OFFICIAL', NULL, 0)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name""",
            (idx["code"], idx["name"]),
        )

    expanded_custom = []
    for idx in custom_defs:
        for universe_code in active_universes:
            concrete_code = f"{idx['code']}__{universe_code}"
            concrete_name = f"{idx['name']} ({universe_code})"
            conn.execute(
                """INSERT INTO indices (code, name, category, weighting_method, is_benchmark,
                                         universe_code, rebalance_frequency)
                   VALUES (?, ?, 'CUSTOM', ?, 0, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                        name=excluded.name, weighting_method=excluded.weighting_method,
                        rebalance_frequency=excluded.rebalance_frequency""",
                (concrete_code, concrete_name, idx["weighting_method"], universe_code,
                 idx.get("rebalance_frequency", "QUARTERLY")),
            )
            concrete_def = dict(idx)
            concrete_def["code"] = concrete_code
            concrete_def["base_code"] = idx["code"]
            concrete_def["name"] = concrete_name
            concrete_def["universe_code"] = universe_code
            expanded_custom.append(concrete_def)
    conn.commit()
    return expanded_custom


def register_cap_split_index(
    conn: sqlite3.Connection, base_def: Dict[str, Any], band_code: str,
) -> str:
    code = f"{base_def['code']}_{band_code}"
    name = f"{base_def['name']} - {band_code.title()} Cap"
    conn.execute(
        """INSERT INTO indices (code, name, category, weighting_method, is_benchmark,
                                 universe_code, cap_band, parent_index_code, rebalance_frequency)
           VALUES (?, ?, 'CUSTOM', ?, 0, ?, ?, ?, ?)
           ON CONFLICT(code) DO UPDATE SET name=excluded.name""",
        (code, name, base_def["weighting_method"], base_def["universe_code"],
         band_code, base_def["code"], base_def.get("rebalance_frequency", "QUARTERLY")),
    )
    conn.commit()
    return code


# ---------------------------------------------------------------------------
# Rebalance scheduling (quarterly-lock)
# ---------------------------------------------------------------------------

def is_rebalance_due(conn: sqlite3.Connection, index_code: str, as_of_date: str, frequency: str) -> bool:
    row = conn.execute(
        "SELECT MAX(effective_from) FROM custom_index_constituent_history WHERE index_code=?",
        (index_code,),
    ).fetchone()
    last_rebalance = row[0] if row else None
    if last_rebalance is None:
        return True
    return period_key(as_of_date, frequency) != period_key(last_rebalance, frequency)


def _write_constituent_batch(conn: sqlite3.Connection, index_code: str, weights: Dict[int, float], as_of_date: str) -> None:
    """Closes the ENTIRE current batch for this index and opens a fresh one --
    every new row shares effective_from=as_of_date, giving compute_index_ohlc
    a well-defined weight-vector segment for point-in-time reconstruction."""
    conn.execute(
        """UPDATE custom_index_constituent_history SET effective_to=?, is_current=0
           WHERE index_code=? AND is_current=1""",
        (day_before(as_of_date), index_code),
    )
    for iid, w in weights.items():
        conn.execute(
            """INSERT INTO custom_index_constituent_history (index_code, instrument_id, weight, effective_from, is_current)
               VALUES (?, ?, ?, ?, 1)""",
            (index_code, iid, w, as_of_date),
        )
    conn.commit()


def construct_custom_index(
    conn: sqlite3.Connection, index_def: Dict[str, Any], weighting_params: Dict[str, Any], as_of_date: str,
    instrument_ids_override: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Builds/refreshes one concrete (already universe-suffixed) custom index.
    If instrument_ids_override is given, constituents are NOT re-resolved from
    sector/industry/universe filters -- used for cap-band sub-indices, whose
    constituent pool is the parent index's pool filtered by cap band."""
    code = index_def["code"]
    frequency = index_def.get("rebalance_frequency", "QUARTERLY")

    if not is_rebalance_due(conn, code, as_of_date, frequency):
        locked_count = len(
            current_members(conn, "custom_index_constituent_history", "instrument_id", "index_code", code)
        )
        logger.info("Index %s: no rebalance due on %s (frequency=%s) -- keeping %d locked constituent(s)",
                     code, as_of_date, frequency, locked_count)
        return {"index": code, "constituents": locked_count, "rebalanced": False}

    if instrument_ids_override is not None:
        instrument_ids = instrument_ids_override
    else:
        instrument_ids = resolve_constituents(
            conn, index_def["sector_filter"], index_def.get("industry_filter"), index_def["universe_code"]
        )

    if not instrument_ids:
        logger.warning("Custom index %s resolved to zero constituents on %s", code, as_of_date)
        return {"index": code, "constituents": 0, "rebalanced": False}

    market_caps = _market_caps(conn, instrument_ids, as_of_date)
    method = get_weighting_method(index_def["weighting_method"])
    weights = method.compute_weights(instrument_ids, market_caps, weighting_params)

    _write_constituent_batch(conn, code, weights, as_of_date)
    logger.info("Index %s: rebalanced on %s with %d constituents", code, as_of_date, len(weights))
    return {"index": code, "constituents": len(instrument_ids), "rebalanced": True}


def build_cap_split_indices(
    conn: sqlite3.Connection, base_def: Dict[str, Any], bands_config: List[Dict[str, Any]],
    min_constituents: int, weighting_params: Dict[str, Any], as_of_date: str,
) -> List[Dict[str, Any]]:
    """For a base custom index flagged cap_split=true, partitions its resolved
    constituent pool by capitalization band and constructs a sub-index for
    every band that clears min_constituents. Returns per-sub-index summaries."""
    if not base_def.get("cap_split"):
        return []

    all_ids = resolve_constituents(
        conn, base_def["sector_filter"], base_def.get("industry_filter"), base_def["universe_code"]
    )
    if not all_ids:
        return []
    market_caps = _market_caps(conn, all_ids, as_of_date)

    by_band: Dict[str, List[int]] = {}
    for iid in all_ids:
        mcap = market_caps.get(iid)
        if mcap is None:
            continue
        band = get_cap_band(mcap, bands_config)
        if band:
            by_band.setdefault(band, []).append(iid)

    summaries = []
    for band_code, ids in by_band.items():
        if len(ids) < min_constituents:
            logger.info("Index %s: cap band %s has only %d constituent(s) (< %d), skipping sub-index",
                         base_def["code"], band_code, len(ids), min_constituents)
            continue
        sub_code = register_cap_split_index(conn, base_def, band_code)
        sub_def = dict(base_def)
        sub_def["code"] = sub_code
        result = construct_custom_index(conn, sub_def, weighting_params, as_of_date, instrument_ids_override=ids)
        summaries.append(result)
    return summaries


# ---------------------------------------------------------------------------
# OHLC aggregation -- segment-aware (uses the weight snapshot in effect on
# each historical date, not just today's weights retroactively)
# ---------------------------------------------------------------------------

def _load_weight_segments(conn: sqlite3.Connection, index_code: str) -> Dict[str, Dict[int, float]]:
    rows = conn.execute(
        "SELECT instrument_id, weight, effective_from FROM custom_index_constituent_history WHERE index_code=?",
        (index_code,),
    ).fetchall()
    segments: Dict[str, Dict[int, float]] = {}
    for iid, w, eff_from in rows:
        segments.setdefault(eff_from, {})[iid] = w
    return segments


def compute_index_ohlc(conn: sqlite3.Connection, index_code: str, base_value: float = 1000.0) -> pd.DataFrame:
    """Divisor-free weighted-return-chaining: level_t = level_{t-1} * (1 + sum(w_i * r_i)),
    where the weight vector used on any given date is whichever SCD2 batch was
    in effect on that date. Contributions are accumulated with an explicit
    per-constituent loop (reindex-and-add), never by concatenating
    same-named columns and selecting by duplicate label -- a known footgun
    (duplicate labels select the cross-product of matches, not each once)
    documented and fixed in the sibling Intelligence Engine project; avoided
    here from the start.
    """
    segments = _load_weight_segments(conn, index_code)
    if not segments:
        return pd.DataFrame()
    segment_starts = sorted(segments.keys())

    all_ids: set[int] = set()
    for seg in segments.values():
        all_ids.update(seg.keys())

    price_frames = {}
    for iid in all_ids:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close FROM instrument_price_history WHERE instrument_id=? ORDER BY date",
            conn, params=(iid,),
        )
        if df.empty:
            continue
        df["ret"] = df["close"].pct_change().fillna(0.0)
        df["o_ratio"] = df["open"] / df["close"]
        df["h_ratio"] = df["high"] / df["close"]
        df["l_ratio"] = df["low"] / df["close"]
        price_frames[iid] = df.set_index("date")

    if not price_frames:
        return pd.DataFrame()

    all_dates = sorted(set().union(*(f.index for f in price_frames.values())))

    def segment_for(d: str) -> Dict[int, float]:
        chosen = segment_starts[0]
        for s in segment_starts:
            if s <= d:
                chosen = s
            else:
                break
        return segments[chosen]

    dates_out, opens, highs, lows, closes = [], [], [], [], []
    level = base_value
    for d in all_dates:
        weights = segment_for(d)
        ret = o_r = h_r = l_r = 0.0
        active = 0
        for iid, w in weights.items():
            frame = price_frames.get(iid)
            if frame is None or d not in frame.index:
                continue
            row = frame.loc[d]
            ret += row["ret"] * w
            o_r += row["o_ratio"] * w
            h_r += row["h_ratio"] * w
            l_r += row["l_ratio"] * w
            active += 1
        if active == 0:
            continue
        level = level * (1 + ret)
        dates_out.append(d)
        closes.append(level)
        opens.append(level * (o_r if o_r else 1.0))
        highs.append(level * (h_r if h_r else 1.0))
        lows.append(level * (l_r if l_r else 1.0))

    return pd.DataFrame({"date": dates_out, "open": opens, "high": highs, "low": lows, "close": closes})


def persist_index_ohlc(conn: sqlite3.Connection, index_code: str, df: pd.DataFrame) -> int:
    rows = 0
    for _, r in df.iterrows():
        conn.execute(
            """INSERT INTO index_ohlc_daily (index_code, date, open, high, low, close)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(index_code, date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close""",
            (index_code, r["date"], r["open"], r["high"], r["low"], r["close"]),
        )
        rows += 1
    conn.commit()
    return rows
