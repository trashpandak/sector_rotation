"""FastAPI application.

Optional, additional read layer over the same SQLite store the dashboard
reads from directly. The dashboard is, and remains, a standalone HTML file --
running this API is never required to view or use the dashboard. It exists
for future consumers (a Telegram bot, another internal tool, an alerting
service) that want live queries instead of parsing the HTML's embedded JSON.

Run locally:
    PYTHONPATH=src uvicorn rrg.api.main:app --reload
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from rrg.analytics.rotation import build_multi_timeframe_matrix, build_rotation_table
from rrg.api.deps import get_config, get_db, require_api_key

app = FastAPI(title="NSE Market Rotation Compass API", version="1.0")

_cors_origins = get_config().system.get("api", {}).get("cors_allow_origins", ["*"])
app.add_middleware(
    CORSMiddleware, allow_origins=_cors_origins, allow_methods=["GET"], allow_headers=["*"],
)


def _envelope(data, conn: sqlite3.Connection):
    meta_row = conn.execute("SELECT as_of_date, config_hash FROM pipeline_runs ORDER BY start_time DESC LIMIT 1").fetchone()
    meta = {"as_of_date": meta_row[0], "config_hash": meta_row[1]} if meta_row else {}
    return {"data": data, "meta": meta, "errors": []}


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/indices", dependencies=[Depends(require_api_key)])
def list_indices(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT code, name, category, weighting_method, universe_code, cap_band, is_benchmark FROM indices ORDER BY code"
    ).fetchall()
    return _envelope([dict(r) for r in rows], conn)


@app.get("/api/v1/indices/{code}", dependencies=[Depends(require_api_key)])
def get_index(code: str, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM indices WHERE code=?", (code,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Index '{code}' not found")
    return _envelope(dict(row), conn)


@app.get("/api/v1/indices/{code}/ohlc", dependencies=[Depends(require_api_key)])
def get_index_ohlc(code: str, start: Optional[str] = None, end: Optional[str] = None, conn: sqlite3.Connection = Depends(get_db)):
    query = "SELECT date, open, high, low, close FROM index_ohlc_daily WHERE index_code=?"
    params: list = [code]
    if start:
        query += " AND date >= ?"; params.append(start)
    if end:
        query += " AND date <= ?"; params.append(end)
    query += " ORDER BY date"
    rows = conn.execute(query, params).fetchall()
    return _envelope([dict(r) for r in rows], conn)


@app.get("/api/v1/indices/{code}/constituents", dependencies=[Depends(require_api_key)])
def get_index_constituents(code: str, as_of: Optional[str] = None, conn: sqlite3.Connection = Depends(get_db)):
    if as_of:
        rows = conn.execute(
            """SELECT i.symbol, i.name, c.weight FROM custom_index_constituent_history c
               JOIN instruments i ON i.instrument_id = c.instrument_id
               WHERE c.index_code=? AND c.effective_from <= ?
                 AND (c.effective_to IS NULL OR c.effective_to >= ?)
               ORDER BY c.weight DESC""",
            (code, as_of, as_of),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT i.symbol, i.name, c.weight FROM custom_index_constituent_history c
               JOIN instruments i ON i.instrument_id = c.instrument_id
               WHERE c.index_code=? AND c.is_current=1 ORDER BY c.weight DESC""",
            (code,),
        ).fetchall()
    return _envelope([dict(r) for r in rows], conn)


@app.get("/api/v1/rrg/{code}", dependencies=[Depends(require_api_key)])
def get_rrg_coordinates(
    code: str, benchmark: str, timeframe: str = "DAILY",
    start: Optional[str] = None, end: Optional[str] = None, conn: sqlite3.Connection = Depends(get_db),
):
    query = "SELECT * FROM rrg_coordinates WHERE index_code=? AND benchmark_code=? AND timeframe=?"
    params: list = [code, benchmark, timeframe]
    if start:
        query += " AND date >= ?"; params.append(start)
    if end:
        query += " AND date <= ?"; params.append(end)
    query += " ORDER BY date"
    rows = conn.execute(query, params).fetchall()
    return _envelope([dict(r) for r in rows], conn)


@app.get("/api/v1/rotation-table", dependencies=[Depends(require_api_key)])
def get_rotation_table(benchmark: str, timeframe: str = "DAILY", conn: sqlite3.Connection = Depends(get_db)):
    return _envelope(build_rotation_table(conn, benchmark, timeframe), conn)


@app.get("/api/v1/rotation-matrix", dependencies=[Depends(require_api_key)])
def get_rotation_matrix(benchmark: str, conn: sqlite3.Connection = Depends(get_db)):
    timeframes = get_config().rrg_settings["timeframes"]
    return _envelope(build_multi_timeframe_matrix(conn, benchmark, timeframes), conn)


@app.get("/api/v1/benchmarks", dependencies=[Depends(require_api_key)])
def list_benchmarks(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute("SELECT code, name FROM indices WHERE is_benchmark=1").fetchall()
    return _envelope([dict(r) for r in rows], conn)
