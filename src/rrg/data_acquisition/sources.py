"""Price data sources.

Two distinct sources, matching the OFFICIAL vs CUSTOM distinction that runs
through this whole system:

- `fetch_official_indices`: pulls REAL NSE sector index OHLC directly from
  Yahoo Finance (tickers verified against Yahoo Finance's own NSE index
  listings, e.g. ^NSEBANK for Nifty Bank, ^CNXIT for Nifty IT). These are
  NOT constructed from constituents -- they ARE the official index.
- `fetch_constituent_prices`: pulls individual stock OHLC for the seed
  universe, used only to build the CUSTOM synthetic indices.

A `SyntheticSource` generates deterministic fake data for both cases, used in
local development, unit tests, and CI dry-runs where outbound network access
to Yahoo Finance isn't available -- this lets the whole pipeline (including
the RRG math) be exercised end-to-end without depending on external data
availability. It is NEVER used silently: the pipeline logs loudly which
source is active on every run.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Dict, List

import pandas as pd

from rrg.common.exceptions import DataSourceError
from rrg.common.logging_config import get_logger

logger = get_logger(__name__)


class PriceSource(ABC):
    @abstractmethod
    def fetch(self, tickers: Dict[str, str], start: str, end: str) -> Dict[str, pd.DataFrame]:
        """tickers: {internal_code: actual_ticker_symbol}. Returns
        {internal_code: DataFrame[date, open, high, low, close, volume]}."""


class YFinanceSource(PriceSource):
    def __init__(self, retries: int = 3, pause_s: float = 1.5, per_ticker_pause_s: float = 0.5):
        self.retries = retries
        self.pause_s = pause_s
        self.per_ticker_pause_s = per_ticker_pause_s

    def fetch(self, tickers: Dict[str, str], start: str, end: str) -> Dict[str, pd.DataFrame]:
        import yfinance as yf

        symbol_list = list(tickers.values())
        result: Dict[str, pd.DataFrame] = {}

        last_exc = None
        raw = None
        for attempt in range(1, self.retries + 1):
            try:
                # threads=False (sequential, not concurrent requests) is
                # deliberate: Yahoo Finance rate-limits aggressively from
                # shared/cloud IPs (GitHub Actions runners in particular --
                # this is a widely documented yfinance issue that affects
                # even blue-chip tickers like AAPL, not just obscure ones).
                # A burst of concurrent requests is far more likely to trip
                # that limit than the same requests spaced out sequentially.
                raw = yf.download(
                    symbol_list, start=start, end=end, group_by="ticker",
                    auto_adjust=True, threads=False, progress=False,
                )
                break
            except Exception as exc:  # noqa: BLE001 - network flakiness expected
                last_exc = exc
                logger.warning("yfinance batch fetch attempt %d/%d failed: %s", attempt, self.retries, exc)
                time.sleep(self.pause_s * attempt)
        if raw is None:
            raise DataSourceError(f"yfinance failed after {self.retries} attempts: {last_exc}")

        missing_codes: Dict[str, str] = {}
        for code, symbol in tickers.items():
            try:
                df = raw if len(symbol_list) == 1 else raw[symbol]
                df = df.dropna(how="all")
                if df.empty:
                    missing_codes[code] = symbol
                    continue
                df = df.rename(columns={
                    "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
                })
                df.index.name = "date"
                out = df[["open", "high", "low", "close", "volume"]].reset_index()
                out["date"] = out["date"].dt.strftime("%Y-%m-%d")
                result[code] = out
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not parse batch price frame for %s (%s): %s", code, symbol, exc)
                missing_codes[code] = symbol

        if missing_codes:
            # A batch download commonly fails PARTIALLY under rate-limiting
            # even when most tickers succeed -- retrying the missing ones
            # individually (one request at a time, with a pause between)
            # frequently recovers data that the batch call dropped. This is
            # the concrete fix for the "possibly delisted; no price data
            # found" errors seen in production for tickers that are
            # confirmed live and valid on Yahoo Finance.
            logger.info("Retrying %d ticker(s) individually after batch fetch gaps: %s",
                        len(missing_codes), list(missing_codes.values()))
            for code, symbol in missing_codes.items():
                recovered = self._fetch_single_with_retry(yf, code, symbol, start, end)
                if recovered is not None:
                    result[code] = recovered
                else:
                    logger.warning("No data returned for %s (%s) after batch + individual retries", code, symbol)

        return result

    def _fetch_single_with_retry(self, yf, code: str, symbol: str, start: str, end: str) -> pd.DataFrame | None:
        for attempt in range(1, self.retries + 1):
            try:
                time.sleep(self.per_ticker_pause_s)
                df = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=True)
                df = df.dropna(how="all")
                if df.empty:
                    continue
                df = df.rename(columns={
                    "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
                })
                df.index.name = "date"
                out = df[["open", "high", "low", "close", "volume"]].reset_index()
                out["date"] = out["date"].dt.strftime("%Y-%m-%d")
                return out
            except Exception as exc:  # noqa: BLE001
                logger.warning("Individual retry %d/%d for %s (%s) failed: %s",
                               attempt, self.retries, code, symbol, exc)
                time.sleep(self.pause_s * attempt)
        return None


class SyntheticSource(PriceSource):
    """Deterministic synthetic OHLCV, seeded per ticker so results are
    reproducible run-to-run. Each series gets its own drift/volatility (seeded
    off a hash of the ticker) so different sectors plausibly diverge -- this
    matters for RRG math testing, since a degenerate case where every series
    is identical would trivially collapse every index onto the benchmark."""

    def fetch(self, tickers: Dict[str, str], start: str, end: str) -> Dict[str, pd.DataFrame]:
        import numpy as np

        dates = pd.bdate_range(start=start, end=end)
        n = len(dates)
        result: Dict[str, pd.DataFrame] = {}
        for code, symbol in tickers.items():
            seed = abs(hash(symbol)) % (2**31)
            rng = np.random.default_rng(seed)
            drift = rng.uniform(-0.0003, 0.0007)
            vol = rng.uniform(0.010, 0.020)
            returns = rng.normal(loc=drift, scale=vol, size=n)
            close = 1000 * (1 + returns).cumprod()
            open_ = close * (1 + rng.normal(0, 0.002, n))
            high = np.maximum(open_, close) * (1 + abs(rng.normal(0, 0.004, n)))
            low = np.minimum(open_, close) * (1 - abs(rng.normal(0, 0.004, n)))
            volume = rng.integers(100_000, 5_000_000, n)
            result[code] = pd.DataFrame({
                "date": dates.strftime("%Y-%m-%d"), "open": open_, "high": high,
                "low": low, "close": close, "volume": volume,
            })
        return result


def get_price_source(source_name: str) -> PriceSource:
    if source_name == "yfinance":
        return YFinanceSource()
    if source_name == "synthetic":
        return SyntheticSource()
    raise DataSourceError(f"Unknown data.source '{source_name}'")
