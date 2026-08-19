import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from rrg.config_manager.loader import ConfigManager
from rrg.pipeline.orchestrator import run_pipeline


@pytest.fixture(scope="module")
def populated_db(tmp_path_factory):
    """Runs a tiny real pipeline (synthetic data source) against a temp config
    dir so the API has something real to serve, rather than mocking storage."""
    base_config_dir = Path(__file__).resolve().parents[2] / "config"
    work_dir = tmp_path_factory.mktemp("api_test_workdir")
    config_dir = work_dir / "config"
    config_dir.mkdir()
    for f in base_config_dir.iterdir():
        if f.is_file():
            (config_dir / f.name).write_text(f.read_text())

    system_path = config_dir / "system.yaml"
    text = system_path.read_text()
    text = text.replace('source: "yfinance"', 'source: "synthetic"')
    db_path = work_dir / "data" / "rrg.db"
    report_dir = work_dir / "reports"
    docs_dir = work_dir / "docs"
    text = text.replace('db_path: "data/rrg.db"', f'db_path: "{db_path}"')
    text = text.replace('report_output_dir: "reports"', f'report_output_dir: "{report_dir}"')
    text = text.replace('docs_output_dir: "docs"', f'docs_output_dir: "{docs_dir}"')
    system_path.write_text(text)

    config = ConfigManager(str(config_dir)).load()
    run_pipeline(config, as_of_date="2026-08-14")  # a Friday -- avoids the weekend market-cap edge case

    os.chdir(work_dir)  # api.deps.ConfigManager("config") resolves "config" relative to cwd
    yield db_path


@pytest.fixture()
def client(populated_db):
    from rrg.api import deps
    deps._config_cache = None  # reset the module-level cache between test modules
    from rrg.api.main import app
    return TestClient(app)


def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_list_indices_returns_both_categories(client):
    resp = client.get("/api/v1/indices")
    assert resp.status_code == 200
    body = resp.json()
    categories = {row["category"] for row in body["data"]}
    assert "OFFICIAL" in categories
    assert "CUSTOM" in categories


def test_get_index_404_for_unknown_code(client):
    resp = client.get("/api/v1/indices/NOT_A_REAL_INDEX")
    assert resp.status_code == 404


def test_index_ohlc_returns_rows(client):
    resp = client.get("/api/v1/indices")
    code = next(r["code"] for r in resp.json()["data"] if r["category"] == "OFFICIAL")
    ohlc_resp = client.get(f"/api/v1/indices/{code}/ohlc")
    assert ohlc_resp.status_code == 200
    assert len(ohlc_resp.json()["data"]) > 0


def test_rrg_endpoint_requires_benchmark_param(client):
    resp = client.get("/api/v1/rrg/OFFICIAL_BANK")
    assert resp.status_code == 422  # FastAPI validation error: missing required query param


def test_rotation_table_returns_ranked_rows(client):
    resp = client.get("/api/v1/rotation-table", params={"benchmark": "NIFTY500", "timeframe": "DAILY"})
    assert resp.status_code == 200
    rows = resp.json()["data"]
    if rows:
        ranks = [r["relative_rank"] for r in rows]
        assert ranks == sorted(ranks)


def test_api_key_enforcement_when_configured(populated_db, monkeypatch):
    """When api_keys is non-empty, requests without a valid X-API-Key must be rejected."""
    from rrg.api import deps
    deps._config_cache = None
    config = deps.get_config()
    config.system.setdefault("api", {})["api_keys"] = ["secret123"]
    deps._config_cache = config

    from rrg.api.main import app
    local_client = TestClient(app)

    resp_no_key = local_client.get("/api/v1/indices")
    assert resp_no_key.status_code == 401

    resp_with_key = local_client.get("/api/v1/indices", headers={"X-API-Key": "secret123"})
    assert resp_with_key.status_code == 200

    deps._config_cache = None  # reset for subsequent tests
