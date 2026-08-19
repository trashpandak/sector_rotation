"""Usage:
    python scripts/run_pipeline.py
    python scripts/run_pipeline.py --as-of-date 2026-08-16
    python scripts/run_pipeline.py --config-dir config --log-level DEBUG
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rrg.common.logging_config import get_logger, setup_logging  # noqa: E402
from rrg.config_manager.loader import ConfigManager  # noqa: E402
from rrg.pipeline.orchestrator import run_pipeline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the NSE Market Rotation Compass pipeline.")
    parser.add_argument("--as-of-date", default=None, help="ISO date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = get_logger("rrg.run_pipeline")

    try:
        config = ConfigManager(args.config_dir).load()
        summary = run_pipeline(config, as_of_date=args.as_of_date)
        logger.info("Run summary:\n%s", json.dumps(summary, indent=2, default=str))
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Pipeline run failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
