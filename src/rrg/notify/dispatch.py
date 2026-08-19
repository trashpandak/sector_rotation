"""Notification Dispatcher.

Optional, no-op by default. Reads webhook URL / bot token from environment
variables (never from config directly, and never committed to the repo --
set as a GitHub Actions secret). A failure here is logged and swallowed, not
raised -- a notification failing must never fail the pipeline run itself.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List

from rrg.common.logging_config import get_logger
from rrg.config_manager.loader import RRGConfig

logger = get_logger(__name__)


def _top_movers(conn: sqlite3.Connection, benchmark_code: str, timeframe: str, n: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT rc.index_code, i.name, rc.rs_ratio, rc.rs_momentum, rc.quadrant
           FROM rrg_coordinates rc JOIN indices i ON i.code = rc.index_code
           WHERE rc.benchmark_code=? AND rc.timeframe=? AND i.is_benchmark=0
             AND rc.date = (SELECT MAX(date) FROM rrg_coordinates WHERE benchmark_code=? AND timeframe=?)
           ORDER BY rc.rs_ratio DESC LIMIT ?""",
        (benchmark_code, timeframe, benchmark_code, timeframe, n),
    ).fetchall()
    return [dict(r) for r in rows]


def _build_summary_text(conn: sqlite3.Connection, config: RRGConfig, result: Dict[str, Any]) -> str:
    benchmark_code = next(b["code"] for b in config.official_indices["benchmarks"] if b.get("is_default_benchmark"))
    top_n = config.notifications.get("top_n_movers", 5)
    leaders = _top_movers(conn, benchmark_code, "DAILY", top_n)
    lines = [f"*NSE Market Rotation Compass* — {result['as_of_date']}", ""]
    lines.append("Top by RS-Ratio (Daily):")
    for r in leaders:
        lines.append(f"  {r['name']}: {r['rs_ratio']:.1f} ({r['quadrant']})")
    lines.append("")
    lines.append(f"Report: {result.get('report_path', 'n/a')}")
    if result.get("validation_run_id"):
        lines.append(f"Validation run: {result['validation_run_id'][:8]}")
    return "\n".join(lines)


def _send_slack(webhook_url: str, text: str) -> None:
    import urllib.request
    import json as _json

    payload = _json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


def _send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    import urllib.request
    import urllib.parse

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload)
    urllib.request.urlopen(req, timeout=10)


def send_notifications(conn: sqlite3.Connection, config: RRGConfig, result: Dict[str, Any]) -> None:
    notif_cfg = config.notifications.get("notifications", {})

    slack_cfg = notif_cfg.get("slack", {})
    if slack_cfg.get("enabled"):
        webhook_url = os.environ.get(slack_cfg.get("webhook_url_env", "SLACK_WEBHOOK_URL"))
        if webhook_url:
            try:
                _send_slack(webhook_url, _build_summary_text(conn, config, result))
                logger.info("Slack notification sent")
            except Exception as exc:  # noqa: BLE001 - notification failure must not break the pipeline
                logger.warning("Slack notification failed: %s", exc)
        else:
            logger.info("Slack notifications enabled but %s is not set -- skipping", slack_cfg.get("webhook_url_env"))

    tg_cfg = notif_cfg.get("telegram", {})
    if tg_cfg.get("enabled"):
        token = os.environ.get(tg_cfg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
        chat_id = os.environ.get(tg_cfg.get("chat_id_env", "TELEGRAM_CHAT_ID"))
        if token and chat_id:
            try:
                _send_telegram(token, chat_id, _build_summary_text(conn, config, result))
                logger.info("Telegram notification sent")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telegram notification failed: %s", exc)
        else:
            logger.info("Telegram notifications enabled but token/chat_id env vars are not set -- skipping")
