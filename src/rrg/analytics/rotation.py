"""Analytics -- pure read/aggregation logic over rrg_coordinates, producing
the Rotation Table and Multi-Timeframe Matrix. No calculation happens here
that isn't already in rrg_coordinates -- this module only shapes it."""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List


def latest_coordinate(conn: sqlite3.Connection, index_code: str, benchmark_code: str, timeframe: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM rrg_coordinates WHERE index_code=? AND benchmark_code=? AND timeframe=?
           ORDER BY date DESC LIMIT 1""",
        (index_code, benchmark_code, timeframe),
    ).fetchone()


def build_rotation_table(conn: sqlite3.Connection, benchmark_code: str, timeframe: str) -> List[Dict[str, Any]]:
    indices = conn.execute("SELECT code, name, category FROM indices WHERE is_benchmark=0 ORDER BY code").fetchall()
    rows = []
    for idx in indices:
        coord = latest_coordinate(conn, idx["code"], benchmark_code, timeframe)
        if coord is None:
            continue
        constituent_count = conn.execute(
            "SELECT COUNT(*) FROM custom_index_constituent_history WHERE index_code=? AND is_current=1", (idx["code"],)
        ).fetchone()[0]
        rows.append({
            "code": idx["code"], "name": idx["name"], "category": idx["category"],
            "constituent_count": constituent_count if idx["category"] == "CUSTOM" else None,
            "rs_ratio": coord["rs_ratio"], "rs_momentum": coord["rs_momentum"],
            "quadrant": coord["quadrant"], "direction_deg": coord["direction_deg"],
            "rotation_speed": coord["rotation_speed"], "date": coord["date"],
        })
    # Relative rank: 1 = strongest RS-Ratio among indices with a valid reading that day
    ranked = sorted(rows, key=lambda r: r["rs_ratio"], reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["relative_rank"] = rank
    return sorted(ranked, key=lambda r: r["relative_rank"])


def build_multi_timeframe_matrix(conn: sqlite3.Connection, benchmark_code: str, timeframes: List[str]) -> List[Dict[str, Any]]:
    indices = conn.execute("SELECT code, name FROM indices WHERE is_benchmark=0 ORDER BY code").fetchall()
    matrix = []
    for idx in indices:
        row: Dict[str, Any] = {"code": idx["code"], "name": idx["name"]}
        any_data = False
        for tf in timeframes:
            coord = latest_coordinate(conn, idx["code"], benchmark_code, tf)
            row[tf.lower()] = coord["quadrant"] if coord else None
            any_data = any_data or coord is not None
        if any_data:
            matrix.append(row)
    return matrix
