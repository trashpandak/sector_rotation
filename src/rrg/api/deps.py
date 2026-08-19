"""API dependencies. Read-only, opens a fresh SQLite connection per request
(same reasoning as the sibling Market Intelligence Engine project's API: the
DB file is written externally by the scheduled pipeline / a git pull, so a
fresh connection per request guarantees each request sees current data
rather than a stale file descriptor). This API is entirely optional -- the
dashboard remains a standalone HTML file with no dependency on this service
running; this exists purely for future consumers that want live queries
instead of parsing the HTML's embedded JSON."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException

from rrg.config_manager.loader import ConfigManager, RRGConfig

_config_cache: Optional[RRGConfig] = None


def get_config() -> RRGConfig:
    global _config_cache
    if _config_cache is None:
        _config_cache = ConfigManager("config").load()
    return _config_cache


def get_db() -> sqlite3.Connection:
    config = get_config()
    db_path = Path(config.system["db_path"])
    if not db_path.exists():
        raise HTTPException(status_code=503, detail=f"Database not found at {db_path} -- has the pipeline run yet?")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    config = get_config()
    allowed_keys = config.system.get("api", {}).get("api_keys", [])
    if not allowed_keys:
        return
    if x_api_key not in allowed_keys:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")
