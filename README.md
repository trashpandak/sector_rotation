# NSE Market Rotation Compass

A transparent, config-driven NSE sector rotation and Relative Rotation Graph
(RRG) system. Identifies which sectors are Leading, Weakening, Lagging, or
Improving relative to a benchmark, across Daily/Weekly/Monthly timeframes,
across multiple NIFTY universes and capitalization bands, for both
**official NSE sector indices** (Nifty Bank, Nifty IT, Nifty Auto, ...) and
**custom synthetic sector indices** — with full historical trails.

The exact mathematics are documented in [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md)
— every number is hand-verifiable from the formulas there, and
`tests/unit/test_rrg_engine.py` contains worked, hand-calculated examples.
This is an original implementation, not a reproduction of any proprietary
indicator.

**This is not a stock scanner, buy/sell signal generator, or portfolio
optimizer.** It answers "where is relative strength rotating," nothing more.

**The dashboard is, and only ever will be, a single standalone HTML file.**
No server, no build step, no API dependency to view it — open it directly in
a browser. The optional API layer described below is a separate, additional
consumer-facing service for future integrations; it is never required to use
the dashboard.

## Quick start

```bash
pip install -r requirements.txt
PYTHONPATH=src python scripts/run_pipeline.py
```

Outputs: `data/rrg.db` (SQLite), and a standalone interactive HTML report at
`reports/market_rotation_<date>.html`. A stable copy is also written to
`docs/index.html` for GitHub Pages.

Run against a specific date:
```bash
PYTHONPATH=src python scripts/run_pipeline.py --as-of-date 2026-08-16
```

## Running in GitHub Actions

`.github/workflows/rotation_pipeline.yml` runs the pipeline daily on
weekdays (18:15 IST) and commits `data/`, `reports/`, and `docs/` back to the
repo — no external database or server, every day's state is a normal git
commit. Switch to weekly by editing the `cron` line. Trigger manually anytime
from the Actions tab, optionally overriding the as-of date.

To publish the live dashboard: Settings → Pages → Deploy from branch → `main`
/ `docs`.

To enable optional Slack/Telegram summaries: set `notifications.slack.enabled`
or `notifications.telegram.enabled` to `true` in `config/notifications.yaml`,
and add the corresponding secret(s) in repo Settings → Secrets → Actions
(`SLACK_WEBHOOK_URL`, or `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`). Both are
no-ops if left unset.

## What's in the dashboard

- **Relative Rotation Graph** — quadrant-shaded scatter chart, one point +
  trail per index, click to inspect.
- **Benchmark switcher** (Nifty 500 / Nifty 50) and **Universe switcher**
  (whichever NIFTY universes are active) — both fully client-side, all data
  precomputed and embedded.
- **Timeframe switch** (Daily / Weekly / Monthly) — genuinely independent
  calculations per timeframe (see methodology §8), not a resampled daily
  chart.
- **Trail length** (5/10/20/30 periods), **Quadrant filter**, **Category
  filter** (Official / Custom), **search**, **index visibility toggles**.
- **Official vs Custom comparison panel** — every configured sector pair
  (`config/comparison_pairs.yaml`) shown side by side with the RS-Ratio delta.
- **Rotation Table** — sortable, every column from the spec (RS-Ratio,
  RS-Momentum, Quadrant, Direction, Rotation Speed, Relative Rank, Cap Band, ...).
- **Multi-Timeframe Matrix** — Daily/Weekly/Monthly quadrant side by side per
  index.
- **Index Detail** — category, universe, cap band, weighting method,
  constituents, current stats, recent trail table.
- **Data Quality panel** — surfaces whatever the Validation Framework found.

## Sector coverage

103 constituents across **26 sectors**, feeding **28 custom sector indices**
(built independently per active universe, plus capitalization-band variants
where the sector has real Large/Mid/Small depth):

Banks, Private Banks, PSU Banks, NBFC, Insurance, Capital Markets, IT
Services, Defence, Capital Goods, Railways, Infrastructure, Pharmaceuticals,
Hospitals, Diagnostics, FMCG, Retail, Consumer Durables, Textiles, Media,
Auto (OEM), Auto Ancillary, Energy (Oil & Gas), Power, Renewable Energy,
Metals & Mining, Cement, Specialty Chemicals, Agro Chemicals — plus 17
**official** NSE sector indices (Bank, IT, Auto, Pharma, Metal, FMCG,
Energy, Realty, PSU Bank, Private Bank, Financial Services, Healthcare, Oil
& Gas, Consumption, Infrastructure, alongside the Nifty 50/500 benchmarks).

## Sectors covered

28 custom sector indices (each built per active universe), matching nearly
the full sector list from the original spec: Banks, Private Banks, PSU
Banks, NBFC, Insurance, Capital Markets, IT Services, Defence, Capital
Goods, Railways, Infrastructure, Pharmaceuticals, Hospitals, Diagnostics,
FMCG, Retail, Consumer Durables, Textiles, Media, Auto OEM, Auto Ancillary,
Energy (Oil & Gas), Power, Renewable Energy, Metals & Mining, Cement,
Specialty Chemicals, Agro Chemicals. 17 official NSE indices including 4
newly added (Healthcare, Oil & Gas, Consumption, Infrastructure).

## Configuration

| File | Controls |
|---|---|
| `config/system.yaml` | paths, data source, backfill window, optional API auth |
| `config/official_indices.yaml` | benchmarks + official NSE index tickers |
| `config/custom_indices.yaml` | custom sector index definitions, weighting, cap-split, rebalance frequency |
| `config/rrg_settings.yaml` | RRG math parameters, timeframes, trail lengths |
| `config/universes.yaml` | NIFTY universes + which are actively built |
| `config/capitalization_bands.yaml` | Large/Mid/Small/Micro market-cap thresholds |
| `config/comparison_pairs.yaml` | Official-vs-Custom sector pairings shown in the dashboard |
| `config/corporate_actions.yaml` | manual registry cross-referenced by the discontinuity check |
| `config/notifications.yaml` | optional Slack/Telegram summary after each run |
| `config/seed_universe.csv` | constituent stocks, sector/industry classification, universe tags |

Add a new official index, custom sector, universe, or comparison pair purely
by editing the relevant config file — no code changes required.

## Data sources

- **Official indices**: Yahoo Finance, real NSE index tickers (`^NSEBANK`,
  `^CNXIT`, `^CNXAUTO`, `^CNXPHARMA`, `^CNXMETAL`, `^CNXFMCG`, `^CNXENERGY`,
  `^CNXREALTY`, `^CNXPSUBANK`, `NIFTY_PVT_BANK.NS`, `^CNXFIN`,
  `NIFTY_HEALTHCARE.NS`, `NIFTY_OIL_AND_GAS.NS`, `^CNXCONSUM`, `^CNXINFRA`;
  benchmarks `^CRSLDX` = Nifty 500, `^NSEI` = Nifty 50) — every ticker
  individually confirmed against a live Yahoo Finance quote page, not a
  secondary source.
- **Custom index constituents**: Yahoo Finance, individual NSE stock tickers
  (`.NS` suffix), for the curated seed universe (103 instruments spanning
  large/mid/small caps across 26 sectors).
- A `SyntheticSource` (deterministic fake data, seeded per ticker) is also
  included for local development, unit tests, and CI dry-runs without
  network access — used to build and test this entire repo end to end.

## Tests

```bash
python -m pytest tests/unit/ -q
```

30 tests: hand-calculated RRG formula checks, weekly/monthly OHLC resampling,
capitalization-band boundaries, universe filtering, SCD2 quarterly-lock
rebalancing, point-in-time OHLC reconstruction, a full synthetic-cycle
methodology backtest, and API smoke tests against a real pipeline-populated
database.

## Notable issues caught during development (all fixed, all regression-tested)

1. **Scale factor miscalibration** — `RS-Ratio = 100 + 100×zscore` turned a
   routine z-score of 1.3 into a reading of -33. Fixed by calibrating to a
   scale of 10 (realistic ~70-130 range).
2. **Universe substring-matching bug** — `LIKE '%NIFTY50%'` also matches
   `'NIFTY500'`, silently pulling Nifty-500-only stocks into a Nifty 50
   index. Fixed with exact pipe-delimited token matching.
3. **Non-trading-day market cap lookup** — an exact-date price join silently
   returned no market cap at all whenever the as-of date fell on a weekend,
   which both broke capitalization-band splitting and quietly degraded
   Market-Cap weighting to Equal Weight project-wide. Fixed with a
   latest-price-on-or-before lookup.
4. **Infeasible-cap design flaw** — a 2-constituent index under a 30% single-
   constituent cap is mathematically infeasible (2×30% = 60% < 100%); an
   earlier version left 40% of the index unallocated rather than falling
   back to uncapped weights. Fixed.
5. **Test assumption error (not a code bug)** — an early test asserted a
   *constant*-rate outperformer should show rising momentum. Momentum
   measures acceleration; a constant rate has zero acceleration by
   definition, so RS-Momentum correctly settling at ≈100 is right. The test
   was corrected, not the implementation.
6. **My own test's phase-length arithmetic** — the historical backtest test
   itself had a miscounted synthetic-cycle segment length, caught by pandas
   refusing to build a DataFrame from mismatched array lengths.
7. **Cascading Chart.js failure (the "just structure, no data" bug)** —
   confirmed by actually executing the dashboard's JS in a real DOM
   environment (jsdom) with the Chart.js CDN blocked. `buildChart()` ran
   first and threw an uncaught error when Chart.js failed to load, which
   halted the entire script -- every panel stayed empty. Fixed by isolating
   every render step in its own try/catch.
8. **Real production yfinance failures, diagnosed from an actual GitHub
   Actions run log** (not synthetic testing this time):
   - Eight official index tickers logged `"possibly delisted; no price data
     found"`. Verified each one directly against a live Yahoo Finance quote
     page (not a secondary source) — every ticker is genuinely valid and
     trading. This is a well-documented yfinance issue: Yahoo Finance
     rate-limits/blocks shared and cloud IPs (GitHub Actions runners in
     particular), which is known to intermittently hit even blue-chip
     tickers like AAPL. Fixed by making `YFinanceSource` resilient: batch
     fetches now run sequentially (`threads=False`, gentler on rate limits)
     and any ticker that comes back empty is retried individually via
     `yf.Ticker().history()` with its own backoff — this recovers data the
     batch call dropped.
   - `TATAMOTORS.NS: possibly delisted; no timezone found` was a **genuine
     corporate action**, not a bug: Tata Motors demerged in Oct–Nov 2025
     into Tata Motors Passenger Vehicles Ltd (`TMPV`, confirmed live) and a
     renamed Tata Motors Ltd for the commercial-vehicle business (`TMCV`,
     confirmed live). The old symbol no longer exists. Fixed by replacing
     `TATAMOTORS` with both `TMPV` and `TMCV` in the seed universe, and
     registering the demerger in `config/corporate_actions.yaml` — exactly
     the scenario that registry was built for.
   - **The real bug underneath the weight-sum validation errors**
     (`CUSTOM_IT_SERVICES` summing to 0.9, `CUSTOM_METALS_MINING` to 0.6):
     when a constituent's price fetch failed (per the yfinance issue above),
     it correctly got 0 weight — but the infeasible-cap feasibility check
     used the *raw* constituent count instead of the count of constituents
     that could actually receive redistributed weight. A 4-constituent index
     with one zero-weight constituent is effectively a 3-constituent index
     for cap-feasibility purposes (3×30% = 90% < 100%, genuinely infeasible),
     but the old check said "4×30% = 120%, feasible" and attempted normal
     redistribution — which had nowhere to put the excess once the only
     "under-cap" constituent turned out to have zero weight, silently
     dropping it. Fixed by basing feasibility on the count of *nonzero*-
     weight constituents; regression-tested with the exact production shape
     (`test_market_cap_weight_with_one_constituent_missing_data_still_sums_to_one`).

## Scope

Built: 28 custom sector indices (up from 11) spanning Financials, Technology,
Industrials, Healthcare, Consumer, Automobiles, Energy, and Materials —
Banks, Private/PSU Banks, NBFC, Insurance, Capital Markets, IT Services,
Defence, Capital Goods, Railways, Infrastructure, Pharma, Hospitals,
Diagnostics, FMCG, Retail, Consumer Durables, Textiles, Media, Auto, Auto
Ancillary, Energy, Power, Renewable Energy, Metals & Mining, Cement,
Specialty Chemicals, Agro Chemicals — plus 16 official NSE indices;
configurable NIFTY universes, capitalization-band sub-indices, SCD2
point-in-time membership with quarterly-lock rebalancing, multi-benchmark and
multi-universe dashboard switching, Official-vs-Custom comparison, expanded
data-quality validation, an optional read-only API layer, optional
Slack/Telegram notifications, a methodology backtest, and resilient data
fetching (per-ticker retry fallback for yfinance's well-documented
cloud-IP rate-limiting).

Not built: a live/authoritative NSE index-constituent feed (universe
membership is a documented hand-curated approximation), automatic corporate
action detection (the registry is manual-entry, though it now has a real
worked example — the Tata Motors 2025 demerger), and scheduled PDF/Excel
report generation (only the HTML report + raw SQLite/CSV-via-SQL export).
