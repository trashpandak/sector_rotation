"""Storage layer -- SQLite, committed back to the repo by GitHub Actions, same
operational pattern as the sibling Market Intelligence Engine project: no
external DB server needed, every day's state is a normal git commit.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from rrg.common.logging_config import get_logger

logger = get_logger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    sector TEXT,
    industry TEXT,
    shares_outstanding_cr REAL,
    nifty_universes TEXT   -- pipe-delimited list, e.g. "NIFTY50|NIFTY100|NIFTY500"
);

CREATE TABLE IF NOT EXISTS instrument_price_history (
    instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (instrument_id, date)
);

-- Every index this system tracks, OFFICIAL or CUSTOM, in one table so RRG
-- computation and the dashboard never special-case one vs the other.
CREATE TABLE IF NOT EXISTS indices (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,       -- OFFICIAL | CUSTOM
    weighting_method TEXT,        -- NULL for OFFICIAL (not constructed, fetched directly)
    is_benchmark INTEGER NOT NULL DEFAULT 0,
    universe_code TEXT,           -- NULL for OFFICIAL; e.g. NIFTY50, NIFTY500 for CUSTOM
    cap_band TEXT,                -- NULL for a sector's headline index; LARGE/MID/SMALL for a _LARGE/_MID/_SMALL variant
    parent_index_code TEXT,       -- for a cap-band variant, the headline index it was split from
    rebalance_frequency TEXT      -- QUARTERLY etc. (CUSTOM only)
);

-- SCD Type 2: custom index constituent weights over time. A rebalance closes
-- the ENTIRE current batch and opens a fresh one sharing one effective_from,
-- so "all rows with the same effective_from" is a coherent weight vector
-- that was in effect for a well-defined date range (same pattern used by the
-- sibling Market Intelligence Engine project, already tested there).
CREATE TABLE IF NOT EXISTS custom_index_constituent_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    index_code TEXT NOT NULL REFERENCES indices(code),
    instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),
    weight REAL NOT NULL DEFAULT 0,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    is_current INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_custom_constituent_current
    ON custom_index_constituent_history (index_code, is_current);

-- Daily OHLC for every index (OFFICIAL: fetched directly. CUSTOM: aggregated
-- from constituents using whichever weight snapshot was in effect on each
-- date). Weekly/Monthly are DERIVED on the fly by the Timeframe Engine from
-- this table -- not separately stored -- so there is exactly one source of
-- truth for price history per index.
CREATE TABLE IF NOT EXISTS index_ohlc_daily (
    index_code TEXT NOT NULL REFERENCES indices(code),
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    PRIMARY KEY (index_code, date)
);

-- RRG coordinates: one row per (index, benchmark, timeframe, date). This is
-- the core analytical output -- everything the dashboard renders (RRG chart,
-- trails, rotation table, multi-timeframe matrix) reads from this table.
CREATE TABLE IF NOT EXISTS rrg_coordinates (
    index_code TEXT NOT NULL,
    benchmark_code TEXT NOT NULL,
    timeframe TEXT NOT NULL,          -- DAILY | WEEKLY | MONTHLY
    date TEXT NOT NULL,
    rs_ratio REAL,
    rs_momentum REAL,
    quadrant TEXT,                    -- LEADING | WEAKENING | LAGGING | IMPROVING
    direction_deg REAL,
    rotation_speed REAL,
    PRIMARY KEY (index_code, benchmark_code, timeframe, date)
);

CREATE TABLE IF NOT EXISTS validation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    severity TEXT NOT NULL,
    module TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT,
    status TEXT,
    as_of_date TEXT NOT NULL,
    config_hash TEXT NOT NULL
);
"""


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    logger.info("Connected to SQLite store at %s", path)
    return conn
