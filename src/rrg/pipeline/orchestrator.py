"""Pipeline Orchestrator.

Sequence: ingest official index prices -> ingest constituent stock prices ->
for each active universe, construct custom indices (quarterly-lock rebalance,
SCD2 weight history) + their capitalization-band sub-indices -> for each
timeframe x benchmark, resample every index and compute RRG coordinates ->
validate -> generate the dashboard -> optionally notify.

Idempotent: every write is an upsert keyed on natural keys, so re-running the
same date range never duplicates data.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

import pandas as pd

from rrg.analytics.rotation import build_multi_timeframe_matrix, build_rotation_table
from rrg.common.exceptions import ValidationCritical
from rrg.common.logging_config import get_logger
from rrg.config_manager.loader import RRGConfig
from rrg.custom_index.construction import (
    build_cap_split_indices,
    compute_index_ohlc,
    construct_custom_index,
    persist_index_ohlc,
    register_indices,
)
from rrg.dashboard.generator import generate_report
from rrg.data_acquisition.sources import get_price_source
from rrg.notify.dispatch import send_notifications
from rrg.rrg_engine.engine import compute_rrg_coordinates
from rrg.storage.db import get_connection
from rrg.timeframe.resample import resample_ohlc
from rrg.universe.manager import load_seed_universe, sync_instruments, update_constituent_prices
from rrg.validation.framework import run_all_checks

logger = get_logger(__name__)


def run_pipeline(config: RRGConfig, as_of_date: str | None = None) -> Dict[str, Any]:
    as_of_date = as_of_date or date.today().isoformat()
    run_id = str(uuid.uuid4())
    start_time = datetime.utcnow().isoformat()
    conn = get_connection(config.system["db_path"])

    conn.execute(
        "INSERT INTO pipeline_runs (run_id, start_time, status, as_of_date, config_hash) VALUES (?, ?, 'RUNNING', ?, ?)",
        (run_id, start_time, as_of_date, config.config_hash),
    )
    conn.commit()

    try:
        summary = _run_stages(conn, config, as_of_date)
        conn.execute("UPDATE pipeline_runs SET status='SUCCESS', end_time=? WHERE run_id=?",
                      (datetime.utcnow().isoformat(), run_id))
        conn.commit()
        logger.info("Pipeline run %s completed successfully.", run_id)
        result = {"run_id": run_id, "status": "SUCCESS", **summary}
        send_notifications(conn, config, result)
        return result
    except ValidationCritical as exc:
        conn.execute("UPDATE pipeline_runs SET status='FAILED', end_time=? WHERE run_id=?",
                      (datetime.utcnow().isoformat(), run_id))
        conn.commit()
        logger.error("Pipeline run %s halted: %s", run_id, exc)
        raise
    finally:
        conn.close()


def _run_stages(conn: sqlite3.Connection, config: RRGConfig, as_of_date: str) -> Dict[str, Any]:
    data_cfg = config.system["data"]
    end_date = as_of_date
    start_date = (date.fromisoformat(as_of_date) - timedelta(days=data_cfg["initial_backfill_days"])).isoformat()
    source = get_price_source(data_cfg["source"])

    # 1. Official indices + benchmarks (fetched directly, not constructed)
    benchmarks = config.official_indices["benchmarks"]
    official_defs = config.official_indices["indices"]
    official_tickers = {d["code"]: d["ticker"] for d in benchmarks + official_defs}
    official_frames = source.fetch(official_tickers, start_date, end_date)

    custom_defs = config.custom_indices["indices"]
    active_universes = config.universes["active_universes"]
    expanded_custom = register_indices(conn, official_defs, custom_defs, benchmarks, active_universes)

    official_rows_written = 0
    for code, df in official_frames.items():
        official_rows_written += persist_index_ohlc(conn, code, df)

    # 2. Constituent stocks
    seed_df = load_seed_universe(config.seed_universe_path)
    symbols = sync_instruments(conn, seed_df)
    update_constituent_prices(conn, source, symbols, data_cfg["symbol_suffix"], start_date, end_date)

    # 3. Custom index construction (per universe) + capitalization-band sub-indices
    method_params = config.custom_indices.get("weighting_params", {})
    bands_config = config.capitalization_bands["bands"]
    min_cap_split = config.capitalization_bands["min_constituents_for_cap_split"]

    custom_summaries = []
    all_custom_index_codes: List[str] = []
    for idx in expanded_custom:
        params = method_params.get(idx["weighting_method"], {})
        result = construct_custom_index(conn, idx, params, as_of_date)
        custom_summaries.append(result)
        all_custom_index_codes.append(idx["code"])

        sub_results = build_cap_split_indices(conn, idx, bands_config, min_cap_split, params, as_of_date)
        custom_summaries.extend(sub_results)
        all_custom_index_codes.extend(r["index"] for r in sub_results)

        ohlc_df = compute_index_ohlc(conn, idx["code"])
        if not ohlc_df.empty:
            persist_index_ohlc(conn, idx["code"], ohlc_df)
        for sub in sub_results:
            sub_ohlc = compute_index_ohlc(conn, sub["index"])
            if not sub_ohlc.empty:
                persist_index_ohlc(conn, sub["index"], sub_ohlc)

    # 4. RRG coordinates: every non-benchmark index x every benchmark x every timeframe
    rrg_cfg = config.rrg_settings["rrg"]
    timeframes = config.rrg_settings["timeframes"]
    all_index_codes = [d["code"] for d in official_defs] + all_custom_index_codes
    coords_written = 0
    daily_cache: Dict[str, pd.DataFrame] = {}

    def get_daily_ohlc(code: str) -> pd.DataFrame:
        if code not in daily_cache:
            daily_cache[code] = pd.read_sql_query(
                "SELECT date, open, high, low, close FROM index_ohlc_daily WHERE index_code=? ORDER BY date",
                conn, params=(code,),
            )
        return daily_cache[code]

    for benchmark in benchmarks:
        bench_code = benchmark["code"]
        bench_daily = get_daily_ohlc(bench_code)
        if bench_daily.empty:
            logger.warning("Benchmark %s has no price data, skipping RRG for it", bench_code)
            continue
        for timeframe in timeframes:
            bench_tf = resample_ohlc(bench_daily, timeframe)
            for index_code in all_index_codes:
                index_daily = get_daily_ohlc(index_code)
                if index_daily.empty:
                    continue
                index_tf = resample_ohlc(index_daily, timeframe)
                coords = compute_rrg_coordinates(
                    index_tf, bench_tf,
                    rrg_cfg["rs_ratio_window"], rrg_cfg["rs_momentum_roc_period"],
                    rrg_cfg["rs_momentum_window"], rrg_cfg["min_history_bars"], rrg_cfg["scale_factor"],
                )
                for _, r in coords.iterrows():
                    conn.execute(
                        """INSERT INTO rrg_coordinates
                                (index_code, benchmark_code, timeframe, date, rs_ratio, rs_momentum,
                                 quadrant, direction_deg, rotation_speed)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(index_code, benchmark_code, timeframe, date) DO UPDATE SET
                                rs_ratio=excluded.rs_ratio, rs_momentum=excluded.rs_momentum,
                                quadrant=excluded.quadrant, direction_deg=excluded.direction_deg,
                                rotation_speed=excluded.rotation_speed""",
                        (index_code, bench_code, timeframe, r["date"], r["rs_ratio"], r["rs_momentum"],
                         r["quadrant"], r["direction_deg"], r["rotation_speed"]),
                    )
                    coords_written += 1
    conn.commit()

    # 5. Validation
    validation_run_id = run_all_checks(
        conn, halt_on_critical=config.system["pipeline"]["halt_on_critical_validation"],
        scale_factor=rrg_cfg["scale_factor"], timeframes=timeframes,
        corporate_actions=config.corporate_actions.get("actions", []),
    )

    # 6. Dashboard (pure static HTML, embeds all universes/benchmarks/timeframes
    # for fully client-side switching -- no server involved in viewing it)
    default_benchmark = next(b["code"] for b in benchmarks if b.get("is_default_benchmark"))
    all_benchmark_codes = [b["code"] for b in benchmarks]
    report_path = generate_report(
        conn, config.system["report_output_dir"], config.system["docs_output_dir"],
        as_of_date, config.config_hash, default_benchmark, all_benchmark_codes, timeframes,
        config.rrg_settings["trail"], active_universes, config.comparison_pairs.get("pairs", []),
    )

    return {
        "as_of_date": as_of_date,
        "symbols_synced": len(symbols),
        "active_universes": active_universes,
        "official_indices_rows": official_rows_written,
        "custom_index_summaries": custom_summaries,
        "rrg_coordinates_written": coords_written,
        "validation_run_id": validation_run_id,
        "report_path": report_path,
    }
