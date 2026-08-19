# RRG Methodology — NSE Market Rotation Compass

This document is the single source of truth for every calculation in the
system. Every number the dashboard shows can be reproduced by hand from the
formulas below (see `tests/unit/test_rrg_engine.py` for worked examples with
hand-checked arithmetic).

This is an **original, transparent implementation**. It is conceptually in
the same family as the well-known JdK RRG framework (plot relative strength
vs. a benchmark, normalized, as a rotating quadrant chart) but does not
reproduce any proprietary JdK formula, constant, or smoothing technique. Where
this implementation differs from JdK's published approach, it's noted below.

---

## 1. Custom sector index construction

For each entry in `config/custom_indices.yaml`, built independently **per
active NIFTY universe** (`config/universes.yaml` → `active_universes`, e.g.
one full set of sector indices for NIFTY50, another for NIFTY500 — index
codes are suffixed accordingly, e.g. `CUSTOM_BANKS__NIFTY50`):

1. Resolve constituents: every instrument in `config/seed_universe.csv` whose
   `sector` (and optionally `industry`) column matches the index's filter
   **and** whose `nifty_universes` tag includes the active universe.
2. Compute weights via the configured method (Equal or capped Market-Cap —
   see §1.1 below).
3. Aggregate constituent daily returns into an index level via
   divisor-free weighted-return-chaining:
   `level_t = level_{t-1} * (1 + Σ w_i · r_i,t)`, starting from `base_value = 1000`.
4. Approximate index Open/High/Low as the value-weighted ratio of each
   constituent's own Open/High/Low to its Close, applied to the computed
   index Close (the standard approach when only daily OHLC, not intraday
   ticks, is available per constituent).

**Constituent membership and weights are point-in-time versioned (SCD Type
2)**, not a static snapshot: `custom_index_constituent_history` records every
rebalance as a new batch (`effective_from`/`effective_to`/`is_current`), and
`compute_index_ohlc` uses whichever weight snapshot was actually in effect on
each historical date — a rebalance changes the index's trajectory going
forward without ever rewriting its past levels. This mirrors the already-
tested pattern from the sibling Market Intelligence Engine project.

### 1.0 Rebalancing — quarterly-lock, not daily re-weighting

Indices do **not** silently re-weight themselves every pipeline run.
`is_rebalance_due()` compares the calendar quarter (or whichever
`rebalance_frequency` is configured) of the current run against the index's
last rebalance date; weights stay locked between quarters even under a large
intra-quarter price move, exactly like real institutional indices. See
`tests/unit/test_custom_index.py::test_rebalance_lock_across_universe_suffixed_index`
for a regression test that forces a 10x relative price move mid-quarter and
confirms weights don't budge until the quarter actually rolls over.

### 1.1 Weighting methodology — why capped Market-Cap is the default

Pure market-cap weighting is standard for *broad-market* indices, but a
sector index built from only 2–8 constituents is far more exposed to one
mega-cap distorting the whole sector's reading (e.g. Reliance would dominate
an "Energy" sector index on its own). The default here is **Market-Cap
weight with a 30% single-constituent cap**, with the excess redistributed
proportionally among uncapped constituents — this keeps the index
representative of *sector rotation*, not single-stock movement, while still
preferring larger, more liquid constituents over pure Equal Weight (which
would let illiquid small constituents add disproportionate noise).

**Infeasible-cap fallback, a deliberate design choice**: if a sector (or a
capitalization-band sub-index — see §1.2) has too few constituents for the
cap to be mathematically satisfiable (e.g. 2 constituents can't both be
capped below 50% and still sum to 100%), the cap is skipped entirely and
plain proportional market-cap weights are used instead — they already sum to
1.0, and a cap meant to prevent one stock dominating a *large* pool isn't
really a meaningful concern for a 2-stock index anyway. An earlier version
instead capped everyone and left the remainder unallocated (weights summing
to e.g. 0.6) — the Validation Framework's weight-sum check caught this
immediately once capitalization-band sub-indices started producing small
constituent pools in practice. See
`tests/unit/test_weighting_and_timeframe.py::test_market_cap_weight_falls_back_to_uncapped_when_cap_is_infeasible`.

### 1.2 Capitalization-band sub-indices

Any custom index flagged `cap_split: true` in `config/custom_indices.yaml`
additionally generates `{code}_LARGE` / `_MID` / `_SMALL` sub-indices,
partitioning the same constituent pool by market-cap band
(`config/capitalization_bands.yaml`) — but **only** for a band that clears
`min_constituents_for_cap_split` (default 2), so a sector with just one
small-cap name doesn't get a meaningless 1-stock "sub-index". `CUSTOM_DEFENCE`
is configured this way and, with the current seed universe, produces the
full `CUSTOM_DEFENCE_LARGE` / `_MID` / `_SMALL` trio — the exact example
given in the original spec.

Market cap for band assignment uses the **latest available close price on or
before the as-of date**, not an exact-date match — a real bug caught during
testing: an exact-date join silently returned no price (and therefore no
market cap for anyone) whenever the pipeline ran for a non-trading day
(e.g. the as-of date fell on a weekend), which both broke capitalization
banding entirely and quietly degraded Market-Cap weighting to Equal Weight
project-wide. See
`tests/unit/test_custom_index.py::test_market_caps_use_latest_price_on_or_before_as_of_date`.



---

## 2. Official NSE index incorporation

Official indices (Nifty Bank, Nifty IT, Nifty Auto, etc.) are fetched
**directly** from Yahoo Finance using their real NSE index tickers (e.g.
`^NSEBANK`, `^CNXIT`) — they are never constructed from constituents, and
they coexist with (never replace) the custom synthetic indices. Every index
in the system carries a `category` of `OFFICIAL` or `CUSTOM` so the dashboard
can filter/compare them directly. Official indices are universe-independent
(they don't get rebuilt per NIFTY universe the way custom indices do).

---

## 3. Benchmark

The default benchmark is **Nifty 500** (`^CRSLDX`), configurable in
`config/official_indices.yaml` (`benchmarks:` list, exactly one entry must
have `is_default_benchmark: true`). Nifty 50 is also computed as a second
benchmark in the underlying data (stored in `rrg_coordinates` per
`benchmark_code`) even though the v1 dashboard only surfaces the default —
adding a benchmark switcher to the dashboard is a data-already-there, UI-only
follow-up.

---

## 4. RS-Ratio — how it's calculated

```
relative_t = index_close_t / benchmark_close_t

rs_ratio_t = 100 + S * (relative_t - rolling_mean(relative, W)) / rolling_std(relative, W)
```

`relative` is turned into a rolling z-score (mean/std over a trailing window
of `W` bars), then rescaled around a center of 100: **100 means relative
strength is exactly at its own W-bar rolling average**; above 100 means the
index is currently stronger than its own recent-average relative strength vs.
the benchmark; below 100 means weaker.

- `W` = `rrg.rs_ratio_window` in `config/rrg_settings.yaml` (default 14 bars
  of whichever timeframe is active — 14 trading days on Daily, 14 weeks on
  Weekly, 14 months on Monthly).
- `S` = `rrg.scale_factor` (default **10**). This scales the z-score into a
  practically readable range. **A real calibration bug was caught during
  development here**: an initial version used `S = 100`, which turns a
  perfectly routine z-score of ±1.3 (unremarkable for a return series) into
  RS-Ratio values of -33 or +233 — miles outside how RRG charts are meant to
  read (published RRG charts typically cluster in the 85–115 range). `S = 10`
  keeps a routine ±1 z-score in the ±10 range, giving RS-Ratio values that
  typically fall in the 70–130 band. See `tests/unit/test_rrg_engine.py::test_scale_factor_keeps_routine_zscores_in_a_realistic_band`
  for the regression test that locks this in, and
  `validation/framework.py::check_rrg_coordinate_sanity` for the runtime
  guard against this class of bug recurring.
- **Zero-variance edge case**: if an index's relative strength hasn't moved
  at all within the window (or an index is compared to itself), `std = 0`
  would otherwise produce `NaN` (0/0) or `±inf`. This is explicitly guarded:
  zero variance is treated as "exactly at the mean" (z = 0, RS-Ratio = 100)
  rather than propagating a corrupt value downstream.

---

## 5. RS-Momentum — how it's calculated

```
roc_t         = rs_ratio_t / rs_ratio_(t-M) - 1
rs_momentum_t = 100 + S * (roc_t - rolling_mean(roc, W2)) / rolling_std(roc, W2)
```

The same normalize-to-100 treatment (same scale factor `S`) is applied to the
`M`-bar rate of change of RS-Ratio itself — RS-Momentum answers "is relative
strength *accelerating or decelerating*," not merely "is the index
outperforming." This is an important distinction verified explicitly in
tests: a **constant** (non-accelerating) rate of outperformance correctly
shows RS-Ratio > 100 but RS-Momentum settling at ≈100 (no bug — that's the
mathematically correct behavior of momentum-as-acceleration). An
**accelerating** outperformer shows both RS-Ratio > 100 and RS-Momentum > 100.

- `M` = `rrg.rs_momentum_roc_period` (default 5 bars).
- `W2` = `rrg.rs_momentum_window` (default 14 bars).

Using the same scale factor `S` for both axes is what makes RS-Ratio and
RS-Momentum comparable as (x, y) coordinates on one chart — both cluster
around 100 with a similar practical spread.

---

## 6. Normalization

Both axes use the same rolling z-score-to-100 technique (§4/§5) — there is
no separate "normalization" step beyond what's already described. This
keeps the methodology to one idea applied twice, rather than two different
statistical techniques that would need separately justifying.

---

## 7. Quadrant boundaries

Center = **(100, 100)** on both axes — a direct, parameter-free consequence
of the normalization above (there is no separate tunable "boundary" setting).

| Quadrant | RS-Ratio | RS-Momentum |
|---|---|---|
| LEADING | ≥ 100 | ≥ 100 |
| WEAKENING | ≥ 100 | < 100 |
| LAGGING | < 100 | < 100 |
| IMPROVING | < 100 | ≥ 100 |

---

## 8. Daily / Weekly / Monthly — how they actually differ

The Timeframe Engine (`timeframe/resample.py`) resamples the **daily OHLC
bars themselves** into independent Weekly (`W-FRI`, i.e. bars close Friday)
and Monthly (calendar month-end) OHLC series *before* any RS-Ratio/RS-Momentum
calculation happens. The RRG Engine then runs its rolling windows (`W`, `W2`)
over whichever bar series it's given — a 14-bar window means 14 trading days
on Daily, but 14 *weeks* (~3.5 months) on Weekly and 14 *months* (>1 year) on
Monthly. This is a materially different, independently-computed statistic on
each timeframe, not a resampled/interpolated version of the same daily
number — verified in `tests/unit/test_weighting_and_timeframe.py` with
hand-calculated weekly/monthly OHLC aggregation.

---

## 9. Rotation trail — direction and speed

```
delta_ratio_t    = rs_ratio_t - rs_ratio_(t-1)
delta_momentum_t = rs_momentum_t - rs_momentum_(t-1)
direction_deg_t  = degrees(atan2(delta_momentum_t, delta_ratio_t))   [0° = East = pure RS-Ratio gain]
rotation_speed_t = sqrt(delta_ratio_t² + delta_momentum_t²)          [Euclidean distance moved in one bar]
```

The trail itself is simply the last N `(rs_ratio, rs_momentum)` coordinates
for an index/timeframe, drawn oldest→newest, with the most recent point
rendered larger and outlined so it's visually distinguishable. N is
user-selectable (5/10/20/30) client-side in the dashboard — the underlying
data stores up to `max(trail.allowed_periods)` points per index/timeframe so
switching N never requires recomputation.

---

## 10. Validation — how correctness is checked

`validation/framework.py` runs, every pipeline run:

- **Missing prices**: any instrument with zero price history at all.
- **Zero/negative prices**: any price row with `close <= 0`.
- **Index discontinuities**: any index whose day-over-day close moved more
  than 20% without an obvious cause (catches index-construction bugs, not
  real market moves — this exact check class caught a real duplicate-column
  aggregation bug in the sibling Market Intelligence Engine project).
- **RRG coordinate sanity**: any `rs_ratio`/`rs_momentum` outside a
  scale-factor-aware band (`100 ± 8·S`) — generous enough to never trip on
  real market data, but this is precisely the check that caught the
  `scale_factor` miscalibration described in §4 during development.
- **Benchmark self-consistency**: if a benchmark's own price series were ever
  run through the RRG engine against itself, the result must be *exactly*
  (100, 100) — a structural invariant of the math, checked wherever computed.

CRITICAL findings halt the pipeline before the (potentially bad) data is
published to the dashboard (`system.yaml` → `pipeline.halt_on_critical_validation`).

Two additional checks beyond the original set:

- **Duplicate constituents**: an instrument appearing more than once in a
  given index's current constituent set (would silently double-count that
  stock's contribution to the weighted return) — CRITICAL.
- **Invalid symbols**: any seed-universe symbol for which no price data could
  ever be fetched (bad ticker, delisted, mistyped) — WARN.
- **Missing timeframe data**: an index with a healthy amount of daily history
  but no RRG coordinates on Weekly or Monthly signals a resampling bug, not
  genuinely insufficient data — WARN.
- **Corporate-action-aware discontinuity checks**: a >20%/day index move that
  falls on a date registered in `config/corporate_actions.yaml` is logged as
  INFO (explained, auditable) rather than raised as an unexplained ERROR.

---

## 11. NIFTY universe selection

`config/universes.yaml` defines the seven universes from the spec (NIFTY50,
NIFTY100, NIFTY200, NIFTY500, MIDCAP150, SMALLCAP250, SMALLCAP500) as tags;
each seed-universe instrument carries a pipe-delimited `nifty_universes`
column (e.g. `"NIFTY50|NIFTY100|NIFTY200|NIFTY500"`). `active_universes`
controls which universes custom indices are actually built for on a given
run — each gets a full, independent set of custom sector indices
(`CUSTOM_BANKS__NIFTY50` vs. `CUSTOM_BANKS__NIFTY500`, etc.), switchable
client-side in the dashboard exactly like the timeframe control.

**Stated plainly**: universe membership tags in `seed_universe.csv` are a
documented approximation for this project's small, hand-curated universe —
not sourced from an authoritative live NSE index-constituent feed. The
architecture fully supports real membership data; that column is the only
thing that would need replacing with an official feed.

Membership filtering matches the universe code as an **exact token** in the
pipe-delimited list, not a raw substring — a real bug caught during testing:
an initial version used SQL `LIKE '%NIFTY50%'`, which also matches
`'NIFTY500'` (`NIFTY50` is a literal substring of `NIFTY500`), silently
pulling Nifty-500-only stocks into a Nifty 50 index. See
`tests/unit/test_custom_index.py::test_resolve_constituents_respects_universe_filter`.

---

## 12. Historical / methodology backtest

`tests/unit/test_methodology_backtest.py` simulates a full synthetic market
cycle (flat → accelerating outperformance → decelerating → reversal → flat)
and asserts the RRG engine's output traces the textbook rotation path:
LEADING during peak accelerating outperformance, WEAKENING or LAGGING once
that outperformance is actively reversing, and at least 3 distinct quadrants
visited over the cycle (ruling out a methodology that trivially sits in one
quadrant regardless of price action). A complementary test confirms a
zero-relative-edge scenario stays locked exactly at (100, 100) throughout.
This is the closest practical substitute for backtesting against a real
known historical sector rally without a live NSE feed — the same code path
production data runs through, exercised against a scenario whose "correct
answer" is knowable by construction.

---

## 13. Optional notifications and API

Two purely optional additions, neither of which the dashboard depends on:

- **Notifications** (`config/notifications.yaml`): an optional Slack webhook
  or Telegram message summarizing top movers after each run, no-op unless
  the corresponding secret env var is set (see `.github/workflows/rotation_pipeline.yml`).
  A notification failure is logged and swallowed, never allowed to fail the
  pipeline run itself.
- **API layer** (`src/rrg/api/`): a read-only FastAPI service over the same
  SQLite store, for future consumers that want live queries instead of
  parsing the dashboard's embedded JSON. The dashboard remains, and always
  will remain, a standalone HTML file — running this API is never required
  to view or use it.
