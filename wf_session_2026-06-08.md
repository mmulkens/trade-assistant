# Walk-Forward Session — 2026-06-08
## Signal Engine Refactor + Strategy B Analysis

---

## 1. Signal Engine Refactor

### What changed

Four files were refactored as specified in `signal_engine/signal_engine_refactor_brief.md`.
Two additional performance optimisations were applied on top of the brief.

#### `indicators.py`
Added `REGISTRY` — a dict of 12 named indicator callables — and a `compute(name, df)` function.
All existing functions (ema, macd, atr, rs_line) are unchanged. MACD is exposed as three
separate entries (`macd_line`, `macd_signal`, `macd_hist`) so callers never need to unpack tuples.

#### `strategy_a.py` and `strategy_b.py`
Each strategy now declares `required_indicators: list[str]` — the names it needs from the
pre-computed dict. The `evaluate()` signature changed from positional series parameters to
`(df, indicators: dict[str, pd.Series])`. No indicator computation happens inside `evaluate()`.

Strategy B note: `high_50d` uses `rolling(50).max()`. `iloc[-2]` gives the 50-day high as of
yesterday (excluding today), which is the correct breakout level. `iloc[-3]` is used for the
freshness check. This is equivalent to the prior direct-slice approach when `breakout_period=50`.

#### `engine.py` — full refactor
- **Strategy registry**: `_STRATEGY_MODULES` dict replaces direct instantiation. Active strategies
  are read from `config.yaml: signal_engine.active_strategies`. Unknown names log a warning and
  are skipped.
- **`prepare(tickers, benchmark_df)`**: reads from the real Parquet cache (`cache_store`, not the
  walker). Computes the union of `required_indicators` across all active strategies. Stores
  indicators as extra columns on the OHLCV DataFrame (see performance note below).
- **`scan(tickers_or_prepared, as_of_date)`**: two-path dispatch. List input → live mode (calls
  `prepare()` internally, backward-compatible). Dict input → WF mode (slices pre-computed data to
  `as_of_date`, no recomputation).
- **`_scan_prepared(prepared, as_of_date, regime, verbose)`**: single index-comparison slice per
  ticker (OHLCV + indicators merged), then O(1) column extraction for the indicators dict.
  `verbose=False` suppresses per-ticker skip debug logs in the WF path.
- **`_scan_one(ticker, df, indicators, regime, verbose)`**: receives pre-sliced data. All indicator
  values read from the `indicators` dict. No `ind.ema()` / `ind.macd()` calls (except in
  `_market_regime()` which operates on the benchmark, not prepared data).

#### `runner.py`
- Imports `cache as real_cache`.
- Calls `engine.prepare(eligible, benchmark_df=bench_full_df)` once before the day loop.
- Inside the loop: `signal_engine.scan(prepared_data, as_of_date=prev_date_str)`.
  `prev_date_str` is D-1 — the last completed trading day, not the execution day — preserving
  the scan-on-D1 → advance-to-D → enter → exit order established in design doc §16.D.

#### `config.yaml`
Added `signal_engine.active_strategies: [strategy_a, strategy_b]`.

#### `walk_forward/__main__.py`
Added `--strategies` flag: `python -m walk_forward run --strategies strategy_b`
or comma-separated: `--strategies strategy_a,strategy_b`. Overrides `active_strategies` for that
run only.

---

### Performance — before vs after

All numbers from a 1,174-day simulation over 514 tickers.

| Phase | Before | After |
|---|---|---|
| `prepare()` | n/a (per-day) | 3–4 s (once) |
| Loop per day | 682 ms | 225 ms |
| Full sim (1,174 days) | 801 s | 265 s |
| Extrapolated 250 days | ~145 s | ~48 s |
| Log file size | 175 MB / 842k DEBUG lines | ~1 MB |

**3× loop improvement.** Two changes drove this:

1. **Merged-df slicing**: indicators stored as columns on the OHLCV DataFrame. One
   `df[df.index <= cutoff]` slice per ticker covers all series simultaneously, instead of
   one per indicator series (~11× fewer O(n) index comparisons per ticker per day).

2. **WF debug suppression**: `verbose=False` in the WF scan path eliminates ~717 JSON
   serialisations per day (strategy skip events). Live `engine.scan(tickers)` is unaffected.

The remaining per-day cost (~225 ms) is dominated by SQLite round-trips:
`get_open_positions`, `record_equity`, `clear_trading_pause` on every iteration, plus
`walker.load()` for open positions feeding SimPM. These are below the SE scope.

---

## 2. Strategy B — 50-Day Breakout WF Analysis

### Simulation parameters
- Universe: 514 S&P 500 / Nasdaq-100 tickers with ≥2,000 calendar days of cache
- Period: 2021-10-01 → 2026-06-05 (1,174 trading days)
- Benchmark: ^GSPC (S&P 500), regime filter enabled
- Starting portfolio: $100,000

### Regime filter
Working correctly. 307 of 1,174 trading days (26.1%) were bear regime (S&P 500 below its
200 EMA). The 2022 drawdown was correctly split into three streaks: Apr–Aug, Aug–Dec, Dec–Jan.
A brief 25-day streak appeared in Mar–May 2025.

### Run 1 — base config (1.5% max risk, 8% max open, 0.3% min, 8% hard cap, 7-day time limit)

| Metric | Value |
|---|---|
| Return | -6.3% |
| Trades entered | 155 / 2,596 signals (6%) |
| Win rate | 42.0% |
| Avg win | +$1,737 / +1.55R |
| Avg loss | -$1,330 / -1.16R |
| Expectancy | -0.02R |
| Stop type: hard cap | 61.5% of all signals |
| Avg stop distance | 7.27% (median 8.00%) |
| Exit: stop hit / trail | 85 / 65 |
| Max drawdown | -23.7% (Nov 2023) |

### Run 2 — adjusted config (1.0% max risk, 8% max open, 0.5% min, 10% hard cap, 28-day time limit)

| Metric | Value |
|---|---|
| Return | -13.3% |
| Trades entered | 243 / 2,544 signals (9.6%) |
| Win rate | 38.8% |
| Avg win | +$1,338 / +1.69R |
| Avg loss | -$941 / -1.15R |
| Expectancy | -0.05R |
| Stop type: hard cap | 42.3% of signals (down from 61.5%) |
| Avg stop distance | 8.30% (median 9.12%) |
| Exit: stop hit / trail | 139 / 93 |
| Max drawdown | -38.3% (Nov 2023) |

### Key findings

**Signal rejection cascade is the dominant issue.**
In both runs, >90% of generated signals never trade. Two rejection categories account for almost
all of them:

- `zero_shares` (680–1,085 signals): the risk per share on high-priced stocks (BKNG, NVR, CMG, etc.)
  exceeds the per-trade risk budget even for a single share. Structural to the portfolio size
  vs stock price mismatch — not fixable with parameter tuning.
- `below_min_position_size` (1,200–1,600 signals): fires when the open risk budget is nearly
  exhausted (portfolio has 5–7 open positions). The remaining risk sliver falls below the minimum
  threshold. Widening `max_open_risk` to 8% made this worse: more concurrent positions → budget
  hit more often, combined with raising `min_position_risk` from 0.3% to 0.5%.

**Hard cap stop is the structural stop problem.**
61.5% (Run 1) / 42.3% (Run 2) of signals generate a hard-capped stop. On a breakout, the
structural swing low sits just below the breakout level, often 12–18% away. The hard cap
forces the stop inside the natural noise zone, causing premature stop-outs. 57–60% of closed
trades exit at `stop_hit`.

**Slightly negative expectancy in both runs.**
Breakeven requires avg_win_R × win_rate > avg_loss_R × loss_rate. Both runs come in just below
this threshold (-0.02R and -0.05R). The 7-day time limit in Run 1 cuts winners too early (avg
1.55R vs the 2.0R target). The 28-day limit in Run 2 improved the win R to 1.69R but the wider
stop enlarged losses proportionally and the win rate dropped.

**Conviction annotation is inverted.**
Elevated conviction (price within 5% of 52-week high) is a negative predictor for Strategy B.
In Run 2: elevated trades averaged -$77 each, standard trades averaged +$307 each (12 trades —
small sample, but directionally consistent across both runs). Stocks breaking out near all-time
highs appear to be more extended and prone to fading. The `near_52wk_high_pct` annotation may
need to be re-examined or inverted for breakout signals.

**Equity peak was at the very start of the simulation (Jan 2022 in Run 2).**
The portfolio was in drawdown for essentially the entire period. This is not a 2022-bear-market
artefact — the regime filter handled that. It reflects genuine negative expectancy in the
strategy across the full bull market of 2023–2024.

---

## 3. Trade-level Analysis — Top 20 Winners and Losers

### Run 1 — Top 20 Winners (base config: 1.5% risk, 8% hard cap, 7-day time limit)

| Ticker | Entry | Exit | Shrs | Risk$ | NetPnL | R | Exit | Conv |
|--------|-------|------|------|-------|--------|---|------|------|
| CRWD | 2023-10-09 | 2024-01-02 | 85 | 1201 | +5706 | 4.75 | trail_stop | elevated |
| HWM | 2023-11-14 | 2024-03-08 | 289 | 1156 | +4798 | 4.15 | trail_stop | elevated |
| JNJ | 2025-11-18 | 2026-03-05 | 97 | 1378 | +3848 | 2.79 | trail_stop | elevated |
| WSM | 2023-09-26 | 2024-01-03 | 197 | 1178 | +3645 | 3.09 | trail_stop | elevated |
| PDD | 2023-10-09 | 2024-01-17 | 117 | 989 | +3608 | 3.65 | trail_stop | elevated |
| HAS | 2025-12-12 | 2026-02-23 | 244 | 1181 | +3505 | 2.97 | trail_stop | elevated |
| ECL | 2023-12-14 | 2024-04-04 | 116 | 1151 | +3093 | 2.69 | trail_stop | elevated |
| VRSN | 2025-01-02 | 2025-04-04 | 87 | 1357 | +3072 | 2.26 | trail_stop | elevated |
| LYV | 2021-10-04 | 2021-11-08 | 189 | 1493 | +2844 | 1.91 | trail_stop | elevated |
| COR | 2022-02-02 | 2022-04-25 | 151 | 1404 | +2817 | 2.01 | trail_stop | elevated |
| AZO | 2025-03-07 | 2025-09-18 | 5 | 1281 | +2736 | 2.14 | trail_stop | elevated |
| IR | 2024-02-02 | 2024-04-10 | 296 | 1217 | +2673 | 2.20 | trail_stop | elevated |
| AVGO | 2023-05-24 | 2023-06-23 | 211 | 1114 | +2669 | 2.40 | trail_stop | elevated |
| CMI | 2025-08-06 | 2025-10-10 | 61 | 1313 | +2665 | 2.03 | trail_stop | elevated |
| JBL | 2023-05-30 | 2023-07-27 | 153 | 1114 | +2616 | 2.35 | trail_stop | elevated |
| AMAT | 2024-02-20 | 2024-06-24 | 78 | 1220 | +2392 | 1.96 | trail_stop | elevated |
| EA | 2025-05-05 | 2025-08-18 | 139 | 1391 | +2346 | 1.69 | trail_stop | standard |
| CL | 2024-07-29 | 2024-09-11 | 393 | 1497 | +2214 | 1.48 | trail_stop | elevated |
| ROL | 2025-02-14 | 2025-06-09 | 488 | 1365 | +2082 | 1.52 | trail_stop | elevated |
| FE | 2025-09-29 | 2026-03-19 | 440 | 1038 | +2065 | 1.99 | trail_stop | elevated |

### Run 1 — Bottom 20 Losers

| Ticker | Entry | Exit | Shrs | Risk$ | NetPnL | R | Exit | Conv |
|--------|-------|------|------|-------|--------|---|------|------|
| RMD | 2024-08-28 | 2025-03-05 | 77 | 1468 | -1591 | -1.08 | stop_hit | elevated |
| AFL | 2022-04-21 | 2022-04-22 | 486 | 1391 | -1592 | -1.14 | stop_hit | elevated |
| BLK | 2025-10-15 | 2025-10-22 | 21 | 1427 | -1595 | -1.12 | stop_hit | elevated |
| CF | 2021-10-04 | 2021-11-02 | 338 | 1496 | -1622 | -1.08 | stop_hit | elevated |
| MAR | 2021-10-04 | 2021-11-26 | 125 | 1498 | -1624 | -1.08 | stop_hit | elevated |
| TSN | 2024-08-06 | 2024-09-25 | 407 | 1466 | -1627 | -1.11 | stop_hit | elevated |
| SNA | 2024-01-30 | 2024-02-08 | 145 | 1361 | -1633 | -1.20 | stop_hit | elevated |
| MDLZ | 2025-07-02 | 2025-07-30 | 422 | 1446 | -1640 | -1.13 | stop_hit | standard |
| ICE | 2025-07-31 | 2025-08-18 | 224 | 1359 | -1643 | -1.21 | stop_hit | elevated |
| APD | 2021-11-08 | 2021-11-26 | 92 | 1474 | -1646 | -1.12 | stop_hit | elevated |
| FDS | 2024-11-07 | 2025-01-10 | 57 | 1472 | -1685 | -1.14 | stop_hit | elevated |
| DE | 2025-02-19 | 2025-03-04 | 34 | 1342 | -1709 | -1.27 | stop_hit | elevated |
| LHX | 2024-07-26 | 2024-07-26 | 122 | 1514 | -1709 | -1.13 | stop_hit | elevated |
| NEE | 2021-11-30 | 2022-01-07 | 457 | 1469 | -1714 | -1.17 | stop_hit | elevated |
| AON | 2024-11-26 | 2024-12-09 | 97 | 1506 | -1763 | -1.17 | stop_hit | elevated |
| WMB | 2024-07-22 | 2024-07-25 | 807 | 1535 | -1763 | -1.15 | stop_hit | elevated |
| MA | 2022-01-31 | 2022-02-24 | 47 | 1402 | -1793 | -1.28 | stop_hit | elevated |
| DELL | 2024-05-30 | 2024-05-31 | 60 | 833 | -2122 | -2.55 | stop_hit | elevated |
| DG | 2022-04-07 | 2022-05-18 | 78 | 1387 | -2191 | -1.58 | stop_hit | elevated |
| ROL | 2026-02-12 | 2026-02-12 | 382 | 1332 | -3938 | -2.96 | stop_hit | elevated |

### Run 2 — Top 20 Winners (adjusted: 1.0% risk, 10% hard cap, 28-day time limit)

| Ticker | Entry | Exit | Shrs | Risk$ | NetPnL | R | Exit | Conv |
|--------|-------|------|------|-------|--------|---|------|------|
| VST | 2023-09-08 | 2024-04-16 | 221 | 717 | +7203 | 10.05 | trail_stop | elevated |
| KMI | 2024-07-19 | 2024-12-03 | 669 | 785 | +4128 | 5.26 | trail_stop | elevated |
| TPR | 2024-11-15 | 2025-03-04 | 157 | 884 | +3443 | 3.90 | trail_stop | elevated |
| FOX | 2024-08-07 | 2025-01-21 | 335 | 809 | +2932 | 3.62 | trail_stop | elevated |
| DUK | 2024-04-22 | 2024-09-19 | 177 | 896 | +2921 | 3.26 | trail_stop | elevated |
| HAS | 2025-12-12 | 2026-02-23 | 194 | 939 | +2786 | 2.97 | trail_stop | elevated |
| CRWD | 2023-10-09 | 2024-01-02 | 39 | 689 | +2618 | 3.80 | trail_stop | elevated |
| CBOE | 2023-08-07 | 2023-12-06 | 79 | 658 | +2422 | 3.68 | trail_stop | elevated |
| GILD | 2025-08-11 | 2026-02-26 | 89 | 949 | +2406 | 2.54 | trail_stop | elevated |
| GDDY | 2024-06-11 | 2024-09-03 | 128 | 811 | +2147 | 2.65 | trail_stop | elevated |
| EME | 2023-02-24 | 2023-08-17 | 42 | 675 | +2090 | 3.10 | trail_stop | elevated |
| TPL | 2024-11-05 | 2024-11-26 | 19 | 757 | +2088 | 2.76 | trail_stop | elevated |
| VRSN | 2025-01-02 | 2025-04-04 | 59 | 921 | +2083 | 2.26 | trail_stop | elevated |
| PDD | 2023-10-09 | 2024-01-17 | 66 | 697 | +2035 | 2.92 | trail_stop | elevated |
| CMI | 2025-08-06 | 2025-10-10 | 46 | 990 | +2010 | 2.03 | trail_stop | elevated |
| BSX | 2024-01-02 | 2024-03-11 | 240 | 686 | +1896 | 2.76 | trail_stop | elevated |
| CIEN | 2024-10-03 | 2025-01-02 | 119 | 786 | +1894 | 2.41 | trail_stop | elevated |
| EXR | 2021-10-29 | 2022-01-03 | 103 | 995 | +1891 | 1.90 | trail_stop | elevated |
| ECL | 2023-12-14 | 2024-04-04 | 68 | 675 | +1813 | 2.69 | trail_stop | elevated |
| FOXA | 2024-08-07 | 2024-11-19 | 276 | 735 | +1680 | 2.28 | trail_stop | elevated |

### Run 2 — Bottom 20 Losers

| Ticker | Entry | Exit | Shrs | Risk$ | NetPnL | R | Exit | Conv |
|--------|-------|------|------|-------|--------|---|------|------|
| EXR | 2022-04-20 | 2022-04-27 | 82 | 981 | -1083 | -1.10 | stop_hit | elevated |
| D | 2022-08-16 | 2022-09-19 | 298 | 958 | -1101 | -1.15 | stop_hit | elevated |
| TER | 2026-04-09 | 2026-04-29 | 24 | 860 | -1106 | -1.29 | trail_stop | elevated |
| PPL | 2025-10-01 | 2025-11-20 | 510 | 979 | -1106 | -1.13 | stop_hit | elevated |
| APD | 2021-11-08 | 2021-11-26 | 62 | 993 | -1109 | -1.12 | stop_hit | elevated |
| NRG | 2026-02-25 | 2026-03-03 | 50 | 917 | -1116 | -1.22 | stop_hit | elevated |
| PAYX | 2021-12-23 | 2022-01-19 | 99 | 1046 | -1123 | -1.07 | stop_hit | elevated |
| SO | 2022-02-01 | 2022-02-14 | 291 | 1010 | -1127 | -1.12 | stop_hit | elevated |
| AFL | 2022-04-21 | 2022-04-22 | 344 | 985 | -1127 | -1.14 | stop_hit | elevated |
| HBAN | 2022-01-18 | 2022-01-21 | 724 | 1053 | -1128 | -1.07 | stop_hit | elevated |
| WM | 2025-03-03 | 2025-03-06 | 111 | 955 | -1128 | -1.18 | stop_hit | elevated |
| NTRS | 2022-01-10 | 2022-01-21 | 100 | 1053 | -1128 | -1.07 | stop_hit | elevated |
| NEE | 2021-11-30 | 2022-01-07 | 320 | 1028 | -1200 | -1.17 | stop_hit | elevated |
| PPG | 2022-01-06 | 2022-01-18 | 119 | 1048 | -1216 | -1.16 | stop_hit | elevated |
| PGR | 2022-04-08 | 2022-04-14 | 169 | 997 | -1241 | -1.25 | stop_hit | elevated |
| DELL | 2024-05-30 | 2024-05-31 | 50 | 867 | -1769 | -2.04 | stop_hit | elevated |
| ULTA | 2024-03-14 | 2024-03-15 | 27 | 773 | -1807 | -2.34 | stop_hit | elevated |
| TPR | 2025-08-14 | 2025-08-14 | 101 | 974 | -1826 | -1.87 | stop_hit | elevated |
| CF | 2025-06-12 | 2025-08-07 | 161 | 966 | -1839 | -1.90 | stop_hit | elevated |
| ROL | 2026-02-12 | 2026-02-12 | 262 | 914 | -2701 | -2.96 | stop_hit | elevated |

### Cross-run observations

**Winners are 100% trail_stop exits across both runs.** Every single winner ran long enough to be trailed out — there are no "hit the 2R target and exited" winners. This means the trailing stop mechanism, not the fixed target, is doing all the work on the right side. The time limit (7-day in Run 1) is cutting trades that were still open and trending — CL and ROL in Run 1 show 1.48R and 1.52R, winners that would have reached higher with more time. Run 2's 28-day limit improved avg win R from 1.55R to 1.69R.

**Same core winners appear in both runs.** CRWD, HWM (Run 1 only), PDD, ECL, VRSN, CMI, WSM, JBL — the same leadership stocks generated the alpha. Run 2 unlocked VST (+10.05R) which did not fire in Run 1, likely because Run 1's higher per-trade risk budget exhausted open capacity before VST's signal date.

**Losers are a tight -1.0R to -1.3R cluster.** Virtually every standard loser lands between -1.07R and -1.28R. The stop mechanic is consistent. The outliers are gap-downs: ROL (-2.96R, same-day, both runs), DELL (-2.55R Run 1 / -2.04R Run 2, next-day), ULTA (-2.34R Run 2, next-day), DG (-1.58R Run 1), CF/TPR (-1.87R–1.90R Run 2). These are earnings or macro gap events that jump through the stop overnight — not a strategy flaw, but unhedgeable gap risk.

**ROL is a recurring problem trade.** It appears as both a big winner (2025-02-14, +1.52R, trail_stop) and the worst single loss (2026-02-12, -2.96R, same-day gap-down) in both runs. The 2026 entry was on the same stock less than a year later and got caught in a sharp gap-down. One bad entry erased roughly 2 prior wins.

**Run 2's losers cluster in Jan–Apr 2022.** Many Run 2 bottom losses (NEE, PPG, AFL, SO, HBAN, NTRS, PAYX, D) are from the first phase of the 2022 bear market, before the regime filter kicked in (S&P crossed below its 200 EMA at the end of April 2022). These are real losses — the regime filter only catches confirmed bear conditions, not the initial breakdown. Roughly 7–8 trades in the first 6 months of 2022 represent a pre-regime-filter cost.

**Conviction annotation is structurally useless for Strategy B.** 39 of the top 40 entries across both winner lists are "elevated." 40 of the 40 bottom entries are "elevated" (MDLZ in Run 1 is the only "standard" loser). This is because breakout setups by definition require the stock to be near new highs, which always triggers the elevated label. The conviction field adds no information — it's a near-constant for breakout trades.

---

## 4. RS Line Leadership Filter — Run 3

### What changed

`strategy_b.evaluate()` gained a fourth condition: the RS line (stock price / S&P 500 price)
must be within `rs_52wk_high_pct`% (config default 5%) of its own 52-week high at the time of
the breakout.  If the RS line peaked months ago and has been declining, the stock was already
losing relative leadership before the price breakout fired — those signals fail at a higher rate.

The benchmark is now **mandatory**, not optional.  `prepare()` no longer accepts `None` for
`benchmark_df` and always computes `rs_line`.  The live `scan()` path raises `RuntimeError`
instead of degrading to `regime="unknown"` if the benchmark cannot be loaded.  Strategy B's
`evaluate()` hard-rejects with `strategy_b:rs_line_missing` if the series is absent, rather
than skipping the check silently.

Config parameter added: `signal_engine.rs_52wk_high_pct: 5` (set to 100 to disable).

### Run 3 results — adjusted config + RS filter

Same config as Run 2 (1.0% max risk, 10% hard cap, 28-day time limit).

| Metric | Run 2 — no RS filter | Run 3 — RS filter | Change |
|--------|---------------------|-------------------|--------|
| Return | -13.3% | -8.3% | **+5.0pp** |
| Trades | 243 | 227 | -16 |
| Win rate | 38.8% | 41.2% | **+2.4pp** |
| Avg win R | +1.69R | +1.53R | -0.16R |
| Avg loss R | -1.15R | -1.12R | -0.03R |
| Expectancy | -0.048R | -0.027R | **−44%** |
| stop_hit % | 59.9% | 56.9% | -3pp |
| trail_stop % | 40.1% | 43.1% | +3pp |

The filter is working as intended: win rate rose 2.4pp, the proportion of trail_stop exits
increased from 40% to 43%, and negative expectancy was nearly halved.  The slight drop in
avg win R (1.69→1.53) reflects that the filter also removed a handful of "ugly winners" —
stocks with declining RS that happened to follow through anyway.  That is the expected cost
of a quality filter.

### Breakeven analysis

With the current 1.53R avg win and 1.12R avg loss, Strategy B needs ≈42.3% win rate to break
even — just 1.1pp above where it now sits.  The gap is narrow enough that the remaining
structural issues could close it without further major redesign.

### Remaining structural issues

**Hard cap stop is still dominant.**  56.9% of losing trades exit at `stop_hit`, most of them
with the stop set by the hard cap rather than the structural swing low.  On a breakout, the
natural swing low is 12–18% below entry; the 10% cap places the stop inside the noise zone and
causes premature stop-outs.  Options:
- Raise the hard cap further (e.g., 12–15%) at the cost of larger individual losses.
- Replace the hard cap with a maximum-shares cap: accept the wide structural stop but size the
  position so risk never exceeds the budget.  This would let the stop sit where it belongs
  while keeping dollar risk controlled.

**Jan–Apr 2022 cluster.**  Roughly 7–8 trades enter before the regime filter confirmed bear
conditions (S&P crossed the 200 EMA at end of April 2022).  These are unavoidable with a
lagging-EMA regime filter; a faster confirmation method (e.g., S&P above its 50 EMA in
addition to 200 EMA) would have kept some of them out.

**Conviction annotation is a no-op for Strategy B.**  The `near_52wk_high_pct` condition
triggers on nearly every breakout signal by construction.  It can be repurposed as a true
quality filter (e.g., RS line at new high *and* price within 5% of 52-week high) or dropped
from Strategy B's signal entirely.

---

## 5. Next steps

- **Strategy A in isolation** — run `--strategies strategy_a` to establish a baseline for the
  EMA Pullback approach before combining strategies or adding a new one.
- **Stop architecture rethink** — evaluate replacing `stop_hard_cap_pct` with a max-shares cap
  for breakout trades so the structural stop can sit where it belongs.
- **Secondary trend filter** — require S&P 500 above its 50 EMA (not just 200) to reduce
  exposure during the initial phase of corrections before the lagging regime filter catches up.
- **Strategy C** — range-tightening / base-contraction before breakout.  Targets the same
  breakout entry point but adds a setup-quality gate: the stock must have consolidated in a
  narrow range for a minimum number of bars before the price expansion fires.
- **Conviction rework** — the `near_52wk_high_pct` annotation is near-constant for breakouts.
  Consider replacing it with a combined RS + price-near-high signal, or removing it from
  Strategy B's output entirely and reserving it for Strategy A where it may be meaningful.
