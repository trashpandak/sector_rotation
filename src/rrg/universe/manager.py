"""Universe Manager -- loads the seed universe (config/seed_universe.csv,
shared with the sibling Market Intelligence Engine project) and keeps
constituent stock price history up to date. Classification (sector/industry)
is used only to resolve which stocks feed which custom index -- this project
does not need the full SCD2 historical-membership machinery the Intelligence
Engine has, since custom index membership here is a static, current-snapshot
config-driven grouping (documented v1 simplification -- see README)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List

import pandas as pd

from rrg.common.logging_config import get_logger
from rrg.data_acquisition.sources import PriceSource

logger = get_logger(__name__)


def load_seed_universe(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    return df


def sync_instruments(conn: sqlite3.Connection, seed_df: pd.DataFrame) -> List[str]:
    symbols = []
    for _, row in seed_df.iterrows():
        conn.execute(
            """INSERT INTO instruments (symbol, name, sector, industry, shares_outstanding_cr, nifty_universes)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                    name=excluded.name, sector=excluded.sector, industry=excluded.industry,
                    shares_outstanding_cr=excluded.shares_outstanding_cr,
                    nifty_universes=excluded.nifty_universes""",
            (row["symbol"], row["name"], row["sector"],
             row["industry"] if isinstance(row.get("industry"), str) else None,
             row.get("shares_outstanding_cr"),
             row.get("nifty_universes", "")),
        )
        symbols.append(row["symbol"])
    conn.commit()
    logger.info("Synced %d constituent instruments", len(symbols))
    return symbols


def update_constituent_prices(
    conn: sqlite3.Connection, source: PriceSource, symbols: List[str],
    symbol_suffix: str, start: str, end: str,
) -> int:
    tickers = {s: f"{s}{symbol_suffix}" for s in symbols}
    price_frames = source.fetch(tickers, start, end)
    rows = 0
    for code, df in price_frames.items():
        row = conn.execute("SELECT instrument_id FROM instruments WHERE symbol=?", (code,)).fetchone()
        if row is None:
            continue
        iid = row[0]
        for _, r in df.iterrows():
            conn.execute(
                """INSERT INTO instrument_price_history (instrument_id, date, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(instrument_id, date) DO UPDATE SET
                        open=excluded.open, high=excluded.high, low=excluded.low,
                        close=excluded.close, volume=excluded.volume""",
                (iid, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]),
            )
            rows += 1
    conn.commit()
    logger.info("Updated constituent prices: %d rows across %d symbols", rows, len(price_frames))
    return rows
