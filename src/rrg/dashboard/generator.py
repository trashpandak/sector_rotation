"""Dashboard Generator.

Still produces exactly ONE standalone HTML file -- no server, no build step,
opens directly in a browser. Everything new in this version (multiple
benchmarks, multiple universes, official-vs-custom comparison) is handled by
embedding MORE data in the same JSON blob and switching it client-side, the
same pattern already used for timeframe/trail-length switching. This module
still does no calculation -- it only reads storage and shapes JSON.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from jinja2 import Environment, FileSystemLoader, select_autoescape

from rrg.common.logging_config import get_logger

logger = get_logger(__name__)
TEMPLATES_DIR = Path(__file__).parent / "templates"


def _index_trail(conn: sqlite3.Connection, index_code: str, benchmark_code: str, timeframe: str, max_points: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT date, rs_ratio, rs_momentum, quadrant, direction_deg, rotation_speed
           FROM rrg_coordinates WHERE index_code=? AND benchmark_code=? AND timeframe=?
           ORDER BY date DESC LIMIT ?""",
        (index_code, benchmark_code, timeframe, max_points),
    ).fetchall()
    rows = list(reversed(rows))
    return [
        {
            "date": r["date"], "rs_ratio": round(r["rs_ratio"], 3), "rs_momentum": round(r["rs_momentum"], 3),
            "quadrant": r["quadrant"],
            "direction_deg": round(r["direction_deg"], 1) if r["direction_deg"] is not None else None,
            "rotation_speed": round(r["rotation_speed"], 3) if r["rotation_speed"] is not None else None,
        }
        for r in rows
    ]


def _index_constituents(conn: sqlite3.Connection, index_code: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT i.symbol, i.name, c.weight FROM custom_index_constituent_history c
           JOIN instruments i ON i.instrument_id = c.instrument_id
           WHERE c.index_code=? AND c.is_current=1 ORDER BY c.weight DESC""",
        (index_code,),
    ).fetchall()
    return [{"symbol": r["symbol"], "name": r["name"], "weight": round(r["weight"] * 100, 1)} for r in rows]


def _validation_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    latest_run = conn.execute("SELECT run_id FROM validation_logs ORDER BY ts DESC LIMIT 1").fetchone()
    if latest_run is None:
        return {"run_id": "n/a", "counts": {}, "messages": []}
    run_id = latest_run[0]
    counts_rows = conn.execute(
        "SELECT severity, COUNT(*) FROM validation_logs WHERE run_id=? GROUP BY severity", (run_id,)
    ).fetchall()
    messages = conn.execute(
        "SELECT severity, module, message FROM validation_logs WHERE run_id=? AND severity != 'INFO' ORDER BY ts",
        (run_id,),
    ).fetchall()
    return {
        "run_id": run_id[:8],
        "counts": {r[0]: r[1] for r in counts_rows},
        "messages": [{"severity": m[0], "module": m[1], "message": m[2]} for m in messages],
    }


def build_report_payload(
    conn: sqlite3.Connection, as_of_date: str, config_hash: str,
    default_benchmark_code: str, benchmark_codes: List[str], timeframes: List[str],
    trail_settings: Dict[str, Any], active_universes: List[str],
    comparison_pairs_config: List[Dict[str, str]],
) -> Dict[str, Any]:
    max_trail = max(trail_settings["allowed_periods"])

    benchmark_rows = conn.execute(
        f"SELECT code, name FROM indices WHERE code IN ({','.join('?' for _ in benchmark_codes)})",
        benchmark_codes,
    ).fetchall()
    benchmarks_payload = [{"code": r["code"], "name": r["name"]} for r in benchmark_rows]

    index_rows = conn.execute(
        """SELECT code, name, category, weighting_method, universe_code, cap_band, parent_index_code
           FROM indices WHERE is_benchmark=0 ORDER BY code"""
    ).fetchall()

    indices_payload = []
    all_ratios: List[float] = []
    all_momentums: List[float] = []
    for idx in index_rows:
        trails: Dict[str, Dict[str, Any]] = {}
        has_any_data = False
        for bench_code in benchmark_codes:
            per_tf = {tf: _index_trail(conn, idx["code"], bench_code, tf, max_trail) for tf in timeframes}
            trails[bench_code] = per_tf
            for tf in timeframes:
                if per_tf[tf]:
                    has_any_data = True
                if bench_code == default_benchmark_code:
                    all_ratios.extend(p["rs_ratio"] for p in per_tf[tf])
                    all_momentums.extend(p["rs_momentum"] for p in per_tf[tf])
        if not has_any_data:
            continue

        base_code = idx["code"].split("__")[0].split("_LARGE")[0].split("_MID")[0].split("_SMALL")[0] \
            if idx["category"] == "CUSTOM" else idx["code"]

        indices_payload.append({
            "code": idx["code"], "name": idx["name"], "category": idx["category"],
            "weighting_method": idx["weighting_method"],
            "universe_code": idx["universe_code"],
            "cap_band": idx["cap_band"],
            "parent_index_code": idx["parent_index_code"],
            "base_code": base_code,
            "constituents": _index_constituents(conn, idx["code"]) if idx["category"] == "CUSTOM" else [],
            "trails": trails,
        })

    if all_ratios and all_momentums:
        pad = 5
        axis_bounds = {
            "x_min": round(min(min(all_ratios), 100) - pad, 1),
            "x_max": round(max(max(all_ratios), 100) + pad, 1),
            "y_min": round(min(min(all_momentums), 100) - pad, 1),
            "y_max": round(max(max(all_momentums), 100) + pad, 1),
        }
    else:
        axis_bounds = {"x_min": 70, "x_max": 130, "y_min": 70, "y_max": 130}

    # Comparison pairs, resolved to concrete (universe-suffixed) custom index
    # codes for every active universe -- so "Official vs Custom" works no
    # matter which universe is currently selected in the dashboard.
    index_codes_present = {i["code"] for i in indices_payload}
    resolved_pairs = []
    for pair in comparison_pairs_config:
        for universe_code in active_universes:
            concrete_custom = f"{pair['custom']}__{universe_code}"
            if concrete_custom in index_codes_present and pair["official"] in index_codes_present:
                resolved_pairs.append({
                    "label": pair["label"], "custom_code": concrete_custom,
                    "official_code": pair["official"], "universe_code": universe_code,
                })

    return {
        "as_of_date": as_of_date,
        "config_hash": config_hash,
        "benchmarks": benchmarks_payload,
        "default_benchmark_code": default_benchmark_code,
        "universes": active_universes,
        "timeframes": timeframes,
        "trail_allowed_periods": trail_settings["allowed_periods"],
        "trail_default_periods": trail_settings["default_periods"],
        "axis_bounds": axis_bounds,
        "indices": indices_payload,
        "comparison_pairs": resolved_pairs,
        "validation": _validation_summary(conn),
    }


def generate_report(
    conn: sqlite3.Connection, report_dir: str | Path, docs_dir: str | Path,
    as_of_date: str, config_hash: str, default_benchmark_code: str, benchmark_codes: List[str],
    timeframes: List[str], trail_settings: Dict[str, Any], active_universes: List[str],
    comparison_pairs_config: List[Dict[str, str]],
) -> str:
    report_dir = Path(report_dir)
    docs_dir = Path(docs_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    payload = build_report_payload(
        conn, as_of_date, config_hash, default_benchmark_code, benchmark_codes, timeframes,
        trail_settings, active_universes, comparison_pairs_config,
    )

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html", "j2"]))
    html = env.get_template("report.html.j2").render(payload=payload, data_json=payload)

    report_path = report_dir / f"market_rotation_{as_of_date}.html"
    report_path.write_text(html, encoding="utf-8")
    (docs_dir / "index.html").write_text(html, encoding="utf-8")

    logger.info("Report generated: %s (%d indices)", report_path, len(payload["indices"]))
    return str(report_path)
