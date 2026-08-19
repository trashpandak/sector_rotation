"""Validation Framework.

Covers: missing prices, zero/negative prices, duplicate constituents, invalid
symbols, index discontinuities (corporate-action aware), missing timeframe
data, RRG coordinate sanity, and benchmark self-consistency.
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Callable, Dict, List, Tuple

from rrg.common.exceptions import ValidationCritical
from rrg.common.logging_config import get_logger

logger = get_logger(__name__)


def _log(conn: sqlite3.Connection, run_id: str, severity: str, module: str, message: str) -> None:
    conn.execute(
        "INSERT INTO validation_logs (run_id, severity, module, message) VALUES (?, ?, ?, ?)",
        (run_id, severity, module, message),
    )
    conn.commit()
    {"INFO": logger.info, "WARN": logger.warning, "ERROR": logger.error, "CRITICAL": logger.critical}[severity](
        "[%s] %s", module, message
    )


def check_missing_prices(conn: sqlite3.Connection, run_id: str, **kwargs) -> None:
    rows = conn.execute(
        """SELECT i.symbol FROM instruments i WHERE NOT EXISTS (
               SELECT 1 FROM instrument_price_history p WHERE p.instrument_id = i.instrument_id
           )"""
    ).fetchall()
    if rows:
        _log(conn, run_id, "WARN", "validation.missing_prices",
             f"{len(rows)} instrument(s) have no price history at all: {[r[0] for r in rows][:10]}")


def check_zero_or_negative_prices(conn: sqlite3.Connection, run_id: str, **kwargs) -> None:
    row = conn.execute("SELECT COUNT(*) FROM instrument_price_history WHERE close <= 0").fetchone()
    if row[0] > 0:
        _log(conn, run_id, "ERROR", "validation.invalid_prices", f"{row[0]} price row(s) have close <= 0")


def check_invalid_symbols(conn: sqlite3.Connection, run_id: str, **kwargs) -> None:
    """Symbols in the seed universe for which no price data could be fetched
    at all -- signals a bad/delisted/mistyped ticker rather than a transient
    data gap (that's check_missing_prices' milder cousin; this one is scoped
    to symbols with literally zero rows after a full pipeline run)."""
    rows = conn.execute(
        """SELECT i.symbol, i.name FROM instruments i WHERE NOT EXISTS (
               SELECT 1 FROM instrument_price_history p WHERE p.instrument_id = i.instrument_id
           )"""
    ).fetchall()
    for r in rows:
        _log(conn, run_id, "WARN", "validation.invalid_symbol",
             f"No price data ever fetched for {r['symbol']} ({r['name']}) -- check the ticker is valid and listed")


def check_duplicate_constituents(conn: sqlite3.Connection, run_id: str, **kwargs) -> None:
    """A given instrument should appear at most once in a given index's
    CURRENT constituent set. Duplicates would silently double-count that
    stock's contribution to the index's weighted return."""
    rows = conn.execute(
        """SELECT index_code, instrument_id, COUNT(*) as n FROM custom_index_constituent_history
           WHERE is_current = 1 GROUP BY index_code, instrument_id HAVING COUNT(*) > 1"""
    ).fetchall()
    if rows:
        _log(conn, run_id, "CRITICAL", "validation.duplicate_constituents",
             f"{len(rows)} (index, instrument) pair(s) have more than one current constituent row "
             f"(e.g. {rows[0]['index_code']}, instrument_id={rows[0]['instrument_id']})")


def check_zero_weight_constituents(conn: sqlite3.Connection, run_id: str, **kwargs) -> None:
    """Flags any current constituent with exactly zero weight -- almost
    always means its market cap couldn't be computed that day (a price fetch
    failure, see data_acquisition/sources.py's per-ticker retry fallback).
    Not itself an error (the weighting engine now handles this correctly,
    see custom_index/weighting.py), but worth surfacing for visibility: a
    constituent silently contributing 0% to its index is a real, if
    temporary, data quality issue worth knowing about, especially if it
    persists across multiple runs for the same symbol."""
    rows = conn.execute(
        """SELECT c.index_code, i.symbol FROM custom_index_constituent_history c
           JOIN instruments i ON i.instrument_id = c.instrument_id
           WHERE c.is_current = 1 AND c.weight = 0"""
    ).fetchall()
    if rows:
        symbols = sorted({r["symbol"] for r in rows})
        _log(conn, run_id, "WARN", "validation.zero_weight_constituent",
             f"{len(rows)} current constituent row(s) have exactly 0 weight (likely missing price/market-cap "
             f"data on the rebalance date), affecting symbol(s): {symbols}")


def check_weight_sums(conn: sqlite3.Connection, run_id: str, **kwargs) -> None:
    rows = conn.execute(
        """SELECT index_code, SUM(weight) FROM custom_index_constituent_history
           WHERE is_current = 1 GROUP BY index_code"""
    ).fetchall()
    for index_code, total in rows:
        if total is None:
            continue
        if abs(total - 1.0) > 0.01:
            _log(conn, run_id, "ERROR", "validation.weight_sum",
                 f"Index {index_code} constituent weights sum to {total:.4f}, expected ~1.0")


def check_index_discontinuities(
    conn: sqlite3.Connection, run_id: str, corporate_actions: List[Dict[str, Any]] | None = None,
    max_daily_move: float = 0.20, **kwargs,
) -> None:
    """Flags an index whose day-over-day close moved more than max_daily_move
    without an obvious cause. If a registered corporate action (see
    config/corporate_actions.yaml) falls on that date for a constituent of a
    CUSTOM index, the finding is downgraded to INFO with a note explaining the
    likely cause rather than raised as an unexplained ERROR -- the action is
    still logged so it remains auditable, it's just not treated as a bug."""
    corporate_actions = corporate_actions or []
    action_dates = {a["effective_date"] for a in corporate_actions}

    rows = conn.execute("SELECT DISTINCT index_code FROM index_ohlc_daily").fetchall()
    for (code,) in rows:
        series = conn.execute(
            "SELECT date, close FROM index_ohlc_daily WHERE index_code=? ORDER BY date", (code,)
        ).fetchall()
        for i in range(1, len(series)):
            prev_close, today_close = series[i - 1][1], series[i][1]
            if not prev_close or not today_close:
                continue
            move = (today_close - prev_close) / prev_close
            if abs(move) <= max_daily_move:
                continue
            today_date = series[i][0]
            if today_date in action_dates:
                _log(conn, run_id, "INFO", "validation.continuity",
                     f"Index {code} moved {move*100:+.1f}% on {today_date} -- matches a registered "
                     "corporate action date, not flagged as an error")
            else:
                _log(conn, run_id, "ERROR", "validation.continuity",
                     f"Index {code} moved {move*100:+.1f}% from {series[i-1][0]} to {today_date} "
                     f"-- exceeds {max_daily_move*100:.0f}% sanity threshold and no corporate action is registered for this date")


def check_missing_timeframe_data(conn: sqlite3.Connection, run_id: str, timeframes: List[str] | None = None, **kwargs) -> None:
    """An index that has RRG coordinates on some timeframes but not others
    (when it clearly has enough underlying daily history) signals a
    resampling bug rather than genuinely insufficient data."""
    timeframes = timeframes or ["DAILY", "WEEKLY", "MONTHLY"]
    index_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT index_code FROM rrg_coordinates"
    ).fetchall()]
    for code in index_codes:
        present = {r[0] for r in conn.execute(
            "SELECT DISTINCT timeframe FROM rrg_coordinates WHERE index_code=?", (code,)
        ).fetchall()}
        missing = set(timeframes) - present
        # Only worth flagging if the index has enough daily bars that weekly/
        # monthly SHOULD also have cleared min_history_bars -- a brand new
        # index legitimately won't have monthly data yet, that's not a bug.
        daily_bars = conn.execute(
            "SELECT COUNT(*) FROM index_ohlc_daily WHERE index_code=?", (code,)
        ).fetchone()[0]
        if missing and daily_bars > 400:  # ~1.5+ years -- plenty for weekly/monthly too
            _log(conn, run_id, "WARN", "validation.missing_timeframe",
                 f"Index {code} has {daily_bars} daily bars but no RRG coordinates for: {sorted(missing)}")


def check_rrg_coordinate_sanity(conn: sqlite3.Connection, run_id: str, scale_factor: float = 10, **kwargs) -> None:
    band = 8 * scale_factor
    lo, hi = 100 - band, 100 + band
    rows = conn.execute(
        "SELECT index_code, timeframe, date, rs_ratio, rs_momentum FROM rrg_coordinates "
        "WHERE rs_ratio < ? OR rs_ratio > ? OR rs_momentum < ? OR rs_momentum > ?",
        (lo, hi, lo, hi),
    ).fetchall()
    if rows:
        _log(conn, run_id, "ERROR", "validation.rrg_sanity",
             f"{len(rows)} RRG coordinate(s) fall outside the expected [{lo:.0f},{hi:.0f}] band "
             f"(e.g. {rows[0]['index_code']}/{rows[0]['timeframe']}/{rows[0]['date']}: "
             f"rs_ratio={rows[0]['rs_ratio']:.1f}, rs_momentum={rows[0]['rs_momentum']:.1f})")


def check_benchmark_self_consistency(conn: sqlite3.Connection, run_id: str, **kwargs) -> None:
    benchmark_rows = conn.execute("SELECT code FROM indices WHERE is_benchmark=1").fetchall()
    for (bench_code,) in benchmark_rows:
        self_rows = conn.execute(
            "SELECT rs_ratio, rs_momentum FROM rrg_coordinates WHERE index_code=? AND benchmark_code=?",
            (bench_code, bench_code),
        ).fetchall()
        for r in self_rows:
            if abs(r["rs_ratio"] - 100) > 1e-6 or abs(r["rs_momentum"] - 100) > 1e-6:
                _log(conn, run_id, "CRITICAL", "validation.benchmark_self_check",
                     f"Benchmark {bench_code} vs itself produced ({r['rs_ratio']}, {r['rs_momentum']}) "
                     "instead of (100, 100) -- RRG engine has a bug")


CHECKS: List[Tuple[str, Callable]] = [
    ("missing_prices", check_missing_prices),
    ("zero_or_negative_prices", check_zero_or_negative_prices),
    ("invalid_symbols", check_invalid_symbols),
    ("duplicate_constituents", check_duplicate_constituents),
    ("zero_weight_constituents", check_zero_weight_constituents),
    ("weight_sums", check_weight_sums),
    ("index_discontinuities", check_index_discontinuities),
    ("missing_timeframe_data", check_missing_timeframe_data),
    ("rrg_coordinate_sanity", check_rrg_coordinate_sanity),
    ("benchmark_self_consistency", check_benchmark_self_consistency),
]


def run_all_checks(
    conn: sqlite3.Connection, halt_on_critical: bool = True, scale_factor: float = 10,
    timeframes: List[str] | None = None, corporate_actions: List[Dict[str, Any]] | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    kwargs = {"scale_factor": scale_factor, "timeframes": timeframes, "corporate_actions": corporate_actions}
    for name, fn in CHECKS:
        try:
            fn(conn, run_id, **kwargs)
        except Exception as exc:  # noqa: BLE001
            _log(conn, run_id, "ERROR", f"validation.{name}", f"Check raised an exception: {exc}")

    critical_count = conn.execute(
        "SELECT COUNT(*) FROM validation_logs WHERE run_id=? AND severity='CRITICAL'", (run_id,)
    ).fetchone()[0]
    if critical_count and halt_on_critical:
        raise ValidationCritical(f"{critical_count} CRITICAL validation finding(s) in run {run_id}")
    return run_id
