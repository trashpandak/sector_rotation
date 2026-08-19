"""Configuration Manager.

Loads and lightly validates every config file, computes a content hash for
provenance/reproducibility (every computed RRG coordinate can be traced back
to the exact config version that produced it), and hands back one object the
rest of the pipeline reads from -- no other module reads YAML directly.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml

from rrg.common.exceptions import ConfigError
from rrg.common.logging_config import get_logger

logger = get_logger(__name__)

CONFIG_FILES = [
    "system.yaml", "official_indices.yaml", "custom_indices.yaml", "rrg_settings.yaml",
    "universes.yaml", "capitalization_bands.yaml", "comparison_pairs.yaml",
    "corporate_actions.yaml", "notifications.yaml",
]


@dataclass
class RRGConfig:
    system: Dict[str, Any]
    official_indices: Dict[str, Any]
    custom_indices: Dict[str, Any]
    rrg_settings: Dict[str, Any]
    universes: Dict[str, Any]
    capitalization_bands: Dict[str, Any]
    comparison_pairs: Dict[str, Any]
    corporate_actions: Dict[str, Any]
    notifications: Dict[str, Any]
    seed_universe_path: Path
    config_hash: str


class ConfigManager:
    def __init__(self, config_dir: str | Path = "config"):
        self.config_dir = Path(config_dir)

    def load(self) -> RRGConfig:
        raw: Dict[str, Any] = {}
        for fname in CONFIG_FILES:
            path = self.config_dir / fname
            if not path.exists():
                raise ConfigError(f"Required config file missing: {path}")
            with open(path, "r", encoding="utf-8") as fh:
                raw[fname] = yaml.safe_load(fh.read()) or {}

        self._validate(raw)
        config_hash = hashlib.sha256(
            json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

        seed_path = self.config_dir / "seed_universe.csv"
        if not seed_path.exists():
            raise ConfigError(f"seed_universe.csv not found at {seed_path}")

        logger.info("Configuration loaded. config_hash=%s", config_hash)
        return RRGConfig(
            system=raw["system.yaml"],
            official_indices=raw["official_indices.yaml"],
            custom_indices=raw["custom_indices.yaml"],
            rrg_settings=raw["rrg_settings.yaml"],
            universes=raw["universes.yaml"],
            capitalization_bands=raw["capitalization_bands.yaml"],
            comparison_pairs=raw["comparison_pairs.yaml"],
            corporate_actions=raw["corporate_actions.yaml"],
            notifications=raw["notifications.yaml"],
            seed_universe_path=seed_path,
            config_hash=config_hash,
        )

    @staticmethod
    def _validate(raw: Dict[str, Any]) -> None:
        benchmarks = raw["official_indices.yaml"].get("benchmarks", [])
        if not benchmarks:
            raise ConfigError("official_indices.yaml must define at least one benchmark")
        defaults = [b for b in benchmarks if b.get("is_default_benchmark")]
        if len(defaults) != 1:
            raise ConfigError(
                f"official_indices.yaml must have exactly one is_default_benchmark=true "
                f"benchmark, found {len(defaults)}"
            )

        method_codes = set(raw["custom_indices.yaml"].get("weighting_params", {}).keys())
        for idx in raw["custom_indices.yaml"].get("indices", []):
            if idx["weighting_method"] not in method_codes:
                raise ConfigError(
                    f"Custom index {idx['code']} references unknown weighting_method "
                    f"'{idx['weighting_method']}' (not in weighting_params)"
                )

        rrg = raw["rrg_settings.yaml"]["rrg"]
        for key in ("rs_ratio_window", "rs_momentum_roc_period", "rs_momentum_window", "min_history_bars", "scale_factor"):
            if key not in rrg or rrg[key] <= 0:
                raise ConfigError(f"rrg_settings.yaml rrg.{key} must be a positive number")

        universe_codes = {u["code"] for u in raw["universes.yaml"]["universes"]}
        for code in raw["universes.yaml"].get("active_universes", []):
            if code not in universe_codes:
                raise ConfigError(f"universes.yaml active_universes references unknown universe '{code}'")
        if not raw["universes.yaml"].get("active_universes"):
            raise ConfigError("universes.yaml must set at least one active_universes entry")

        band_codes = {b["code"] for b in raw["capitalization_bands.yaml"]["bands"]}
        if "LARGE" not in band_codes or "MID" not in band_codes or "SMALL" not in band_codes:
            raise ConfigError("capitalization_bands.yaml must define at least LARGE, MID, and SMALL bands")

        custom_codes = {idx["code"] for idx in raw["custom_indices.yaml"]["indices"]}
        official_codes = {idx["code"] for idx in raw["official_indices.yaml"]["indices"]}
        for pair in raw["comparison_pairs.yaml"].get("pairs", []):
            if pair["custom"] not in custom_codes:
                raise ConfigError(f"comparison_pairs.yaml references unknown custom index '{pair['custom']}'")
            if pair["official"] not in official_codes:
                raise ConfigError(f"comparison_pairs.yaml references unknown official index '{pair['official']}'")
