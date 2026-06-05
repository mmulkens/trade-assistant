# Trade Assistant — Automated Trading Design Decisions

*Working Document · May 2026*

---

## Table of Contents

1. [Overview & Architecture](#1-overview--architecture)
2. [Design Consideration 1 — Data Fetcher](#2-design-consideration-1--data-fetcher)
3. [Design Consideration 2 — Signal Engine](#3-design-consideration-2--signal-engine)
4. [Design Consideration 3 — Entry Order Type](#4-design-consideration-3--entry-order-type)
5. [Design Consideration 4 — Stop Order Type](#5-design-consideration-4--stop-order-type)
6. [Design Consideration 5 — Automation & Human-in-the-Loop](#6-design-consideration-5--automation--human-in-the-loop)
7. [Design Consideration 6 — Hosting & Infrastructure](#7-design-consideration-6--hosting--infrastructure)
8. [Design Consideration 7 — Security](#8-design-consideration-7--security)
9. [Design Consideration 8 — Account Setup](#9-design-consideration-8--account-setup)
10. [Design Consideration 9 — Asset Scope](#10-design-consideration-9--asset-scope)
11. [Design Consideration 10 — Position Manager](#11-design-consideration-10--position-manager)
12. [Design Consideration 11 — Logging & Analytics](#12-design-consideration-11--logging--analytics)
13. [Design Consideration 12 — Reporting](#13-design-consideration-12--reporting)
14. [Pre-Build Decisions & System Parameters](#14-pre-build-decisions--system-parameters)
15. [Design Consideration 13 — IBKR Connectivity](#15-design-consideration-13--ibkr-connectivity)
16. [Design Consideration 14 — Walk-Forward Simulator](#16-design-consideration-14--walk-forward-simulator)
17. [Functional Requirements](#17-functional-requirements)

---

## 1. Overview & Architecture

This document captures design decisions for the Trade Assistant automated trading system, using the IBKR API via a dedicated IBKR account. The system evolves from a notification-based assistant to a fully automated execution engine. Notifications shift to a reporting role: trades taken, price alerts, and system events.

### System Architecture

```
Trade Assistant
    │
    ├── Data Fetcher         → yfinance OHLCV, Parquet cache, delta loads
    │
    ├── Signal Engine        → EMA Pullback + Breakout strategies
    │         (emits: conid, entry, stop, target, signal_type, liquidity_class)
    │
    ├── Risk Layer           → position sizing, 1.5% / 6% hard caps
    │
    ├── Order Executor       → IBKR TWS API
    │         ├── R:R validation (incl. costs)
    │         ├── Marketable limit orders
    │         └── Stop / target management
    │
    ├── Position Manager     → ATR trail, cost floor, time-based exits, manual exit detection
    │
    ├── Walk-Forward Simulator → strategy validation on historical data
    │         ├── DataFrame Walker      (cache interception, lookahead prevention)
    │         ├── Signal Engine         (reused unchanged via constructor injection)
    │         ├── Risk Layer            (reused unchanged, isolated state DB)
    │         ├── Sim Executor          (reused unchanged)
    │         └── Sim Position Manager  (bar-by-bar exit simulation)
    │
    ├── Logging Layer        → JSON lines + SQLite analytics DB
    │
    ├── Report Generator     → per-trade diary, portfolio summary, annual tax export,
    │                          WF equity curve and aggregate statistics
    │
    └── Notification Layer   → Telegram
```

> **Key coupling:** The Signal Engine and Order Executor are tightly coupled at the R:R validation step. A signal must pass stop and target levels so the executor can perform the full cost-inclusive R:R calculation before touching the market.

---

## 2. Design Consideration 1 — Data Fetcher

The data fetcher is the foundation of the pipeline. It sources, caches, and maintains all OHLCV data required by the signal engine and position manager.

### A. Data Source

**Decision: yfinance (Yahoo Finance)**

- Free, no API key required
- Covers EU exchanges with correct ticker suffixes (.AS, .PA, .DE, .BR etc.)
- Sufficient data quality for daily bars across the target universe
- Handles 300+ tickers without rate limit issues when batched correctly

*Upgrade path:* EODHD (~€20/month) for higher reliability and better EU coverage if yfinance quality becomes a problem at scale. Deferred until value is validated.

### B. Cache Format

**Decision: Parquet (.parquet), one file per ticker in `cache/` directory**

- Binary format — fast read/write, compressed, no dtype loss on read
- Preserves pandas datetime index without conversion
- Significantly faster than CSV at scale (300+ tickers)
- Requires `pyarrow` dependency

*Rejected:* CSV — slow at scale, dtype loss on read, no compression.

### C. Delta Load Strategy

**Decision: Full fetch on day 1, delta append on subsequent runs**

- Day 1: fetch full 700-day history per ticker
- Day 2+: fetch only missing days since last cached date, append to Parquet
- Eliminates 699 redundant days of fetching on every run after initialisation
- Performance: 300 tickers × 700 days serially = minutes; 300 tickers × 1–3 days in parallel = seconds

> **history_days set to 700** (increased from 300) to support the Walk-Forward Simulator, which requires a 200-bar indicator warmup plus a minimum of one full year (~252 trading days) of simulation depth. 700 calendar days ≈ 480 trading days, giving ~280 tradeable simulation days after warmup (~13 months). The live system is unaffected by having more history than it strictly needs. No separate WF-specific fetch config is required — one history depth serves both modes.

Complexity managed:
- Cache validation on load
- Duplicate row handling on append
- Date gap detection and backfill
- `--full-refresh` CLI flag forces complete re-fetch as escape hatch

### D. Parallel Fetching

**Decision: ThreadPoolExecutor — 8 workers, batches of 16, 2-second pause between batches**

- IO-bound task — threading is the correct model (not multiprocessing)
- No dependencies beyond Python stdlib
- Batch + pause avoids yfinance rate limiting (empirically ~2000 requests/hour before soft throttling; 429 errors observed above 32 simultaneous workers)

*Rejected:* `asyncio` + `aiohttp` — yfinance is not async-native, requires full rewrite for marginal gain.
*Rejected:* `multiprocessing` — wrong model for IO-bound tasks, higher overhead.

### E. Universe & Markets

**Decision: US equities primary, Euronext optional add-on when profitable**

- **Primary universe:** S&P 500, Nasdaq 100, Russell 2000 — approximately 2,000 liquid names
- **Real-time data:** free via IBKR for all US exchanges — no subscription required
- **FX exposure:** USD/EUR on all positions; accepted as manageable given the relatively stable pair and the diversification across uncorrelated positions
- **Belgian TOB tax:** does not apply to US stocks, slightly improving net R:R on every trade versus EU names

**Euronext (optional, deferred):**
- Activate when the bot is demonstrably profitable
- IBKR subscription: €3/month covers Amsterdam, Paris, Brussels, Lisbon, Dublin
- Effective liquid universe after the €1M daily turnover filter: ~200–300 names, the best of which overlap with the existing `eurostoxx600.txt` file
- XETRA (Germany) is a separate IBKR subscription and not included in the €3 bundle

**Rejected for now:**
- LSE — adds GBP risk on top of existing USD exposure; thinner than US for the strategies
- Warsaw (WSE) — PLN currency risk; universe too thin after liquidity filter
- EUR-denomination priority in the signal engine — adds complexity before there is live data to justify it; revisit after six months of live results

*Market hours note:* US market hours are 15:30–22:00 Belgian time. EOD scan runs after 22:00; intraday runs shift to late afternoon/evening. Acceptable for an automated system.

### F. Benchmark

**Decision: `^GSPC` (S&P 500) as primary benchmark**

- Used for RS (Relative Strength) line calculation in the signal engine
- Used for market regime filter — bull/bear determined by S&P 500 vs its 200-day EMA
- Natural benchmark for the primary US universe
- If Euronext is activated as a supplementary universe, add `^STOXX50E` as a secondary benchmark for EU names only

---

## 3. Design Consideration 2 — Signal Engine

The signal engine scans the watchlist daily and emits structured trade signals when setups meet all required conditions. Two independent strategies run in parallel.

### A. Strategy Overview

| Strategy | Type | Entry Condition | Confirmation |
|---|---|---|---|
| Strategy A | EMA Pullback | Price pulls back to 21 EMA in an uptrend | MACD histogram turning up |
| Strategy B | Breakout | Price closes above 50-day highest high | MACD line above zero and rising |

> **OR logic:** a signal fires if Strategy A OR Strategy B passes. Both firing simultaneously = elevated conviction annotation. AND logic was rejected — a pullback and a breakout are nearly mutually exclusive at the same moment.

### B. Strategy A — EMA Pullback

Trigger conditions (all must be true):
- Price is in a defined uptrend (above key moving averages, higher highs/lows)
- Price has pulled back to touch or approach the 21-day EMA
- MACD histogram is turning up (momentum recovering from pullback)

**21 EMA rationale:**
- Fibonacci number — widely used by institutional swing traders (Minervini, O'Neil / IBD methodology)
- Enough market participants watch it to generate real support
- Sweet spot between EMA10 (too reactive) and EMA50 (too slow for swing timeframes)

*Rejected:* EMA20 (less institutional backing), EMA10 (too noisy).

### C. Strategy B — Breakout

Trigger conditions (all must be true):
- Price closes above the highest high of the prior 50 trading days
- MACD line is above zero and rising

**50-day high rationale:**
- 20-day fires too frequently with lower conviction
- 52-week (N=252) fires too rarely as a daily scanner primary trigger
- 50-day balances signal frequency against conviction

*Rejected:* N=20 (too noisy), N=252 as primary trigger (too infrequent).

### D. MACD as Internal Confirmation

MACD is not a standalone strategy — it is an internal confirmation layer within each strategy.

- Strategy A: MACD histogram turning up confirms momentum recovering from the pullback
- Strategy B: MACD line above zero and rising confirms trend momentum behind the breakout
- MACD is derived entirely from price — no independent edge as a standalone entry signal; lags significantly in trending markets

> *Rejected:* MACD as a third peer strategy — adds no independent informational value.

### E. Conviction Annotation

- Both A and B firing simultaneously → elevated conviction annotation
- Price within 5% of 52-week high → additional conviction booster annotation
- Conviction level passed in signal payload and visible in notifications and logs

### F. Rejected Strategies

| Strategy | Reason Rejected |
|---|---|
| VCP (Minervini) | Volatility contraction detection inherently subjective — defining each cycle programmatically hits the same wall regardless of language. Validated in prior R project. |
| AND(A, B, C) combined signal | Would almost never fire — pullback and breakout are near-mutually exclusive simultaneously. |
| MACD as standalone | No independent edge; derived entirely from price; lags in trending markets. |
| Shorting individual stocks | Unlimited loss risk; short squeezes violent in smaller EU stocks. Phase 4+ consideration. |
| Dual Momentum ETF rotation | Monthly rebalance incompatible with swing trading. Valid as separate 30–40% portfolio sleeve — not mixed in. |

### H. Intraday Breakout Confirmation (Strategy B only)

**Problem:** the EOD signal engine fires on the prior day's close. If a breakout stock gaps up at the open and never pulls back to yesterday's close, the Order Executor cannot enter at a viable R:R using a resting limit. High-conviction breakouts running hard early would be missed entirely.

**Decision: scheduled intraday runs at 17:30, 18:30, and 19:30 Belgian time (11:30, 12:30, 13:30 ET), targeting Strategy B only.**

The intraday runs do not replace the EOD run — they complement it. The EOD run fires at market open (or pre-market) on EOD data and places resting limit orders for pullback signals. The intraday runs catch breakouts developing during the session and enter with a marketable limit at the current ask.

**What each intraday run evaluates (Strategy B conditions only):**

- EMA chain alignment and market regime: sourced from the Parquet cache (daily calculations — unchanged intraday)
- Price condition: `current live price > 50-day highest high` — evaluated against the IBKR real-time feed
- Volume confirmation: intraday volume extrapolated to full-session equivalent and checked against 1.5× 20-day average volume
- MACD: trusted from EOD data — partial-day MACD is too noisy to recalculate reliably intraday
- R:R: re-validated from scratch against the live ask price at the moment of each run

**Volume extrapolation:**
```python
session_minutes_total = 390          # NYSE/Nasdaq: 09:30–16:00 ET (15:30–22:00 Belgian time)
elapsed_minutes = now - market_open
extrapolated_volume = intraday_volume * (session_minutes_total / elapsed_minutes)
confirmed = extrapolated_volume >= volume_multiplier * avg_20d_volume
```

**Stricter volume multipliers for earlier runs** (less session elapsed = more extrapolation uncertainty):

| Run time (Belgian) | Run time (ET) | Volume multiplier | Session elapsed (approx.) |
|---|---|---|---|
| 17:30 | 11:30 | 1.8× | ~55% |
| 18:30 | 12:30 | 1.6× | ~67% |
| 19:30 | 13:30 | 1.5× | ~78% (standard) |

**R:R gate is always hard — never relaxed for intraday runs.** A breakout that has already moved so far that R:R is compressed is a worse trade regardless of conviction level. The three runs provide multiple opportunities to catch the signal at a viable price; if none of them produce a valid R:R, the trade is correctly skipped.

**Intraday deduplication — three layers:**

1. Open position check (Risk Layer): if the ticker already has an open position, skip.
2. Pending order check: if the ticker already has a live unconfirmed order from any earlier run today, skip.
3. Session rejection store: if the ticker was evaluated and **rejected for compressed R:R** in any earlier intraday run today, it is added to an in-memory `intraday_rejections` store and skipped in all subsequent runs for the remainder of the session.

The session rejection store is in-memory only and resets at the start of each trading day. It exists to prevent a "gap-and-crap" scenario: a stock that spiked at open (compressing R:R at 17:30 Belgian), then partially faded (appearing to recover at 19:30), is not a clean setup and should not be re-evaluated.

Note: tickers that fail the **price condition** (no longer above the 50-day high) at a given run time are NOT added to the rejection store — a fresh, clean breakout developing later in the session is a legitimate new signal.

**EOD self-deduplication via freshness check:** if an intraday run results in a fill on day N, the EOD run on day N+1 will see that yesterday's close was already above the 50-day high and the existing Strategy B freshness check will suppress the signal naturally. No additional logic required.

**What changes in the codebase:**
- `signal_engine`: add `intraday_mode` flag — runs Strategy B only, accepts live price and intraday volume as inputs, skips all EOD-only indicator recalculation
- Scheduler: add three jobs at 17:30, 18:30, 19:30 Belgian time calling the engine in `intraday_mode`
- Order Executor: no changes — receives a Signal object regardless of which run produced it
- Signal payload: add `run_type` field (`eod` | `intraday`) for logging and audit purposes
- In-memory `intraday_rejections` store: keyed by ticker, scoped to trading session

### I. Future — Strategy C (AVWAP)

Anchored VWAP bounce strategy designed but deferred from Phase 1.

- Anchor selection problem non-trivial to automate
- In long-duration trends, fixed anchor drifts far from price — pullbacks to AVWAP can represent 25–30% retracements, too deep to trade reliably
- Candidate anchor rules: most recent earnings gap, most recent 52-week low, most recent breakout day

*Rejected:* AVWAP with a fixed universal anchor — drift problem invalidates it.

---

## 4. Design Consideration 3 — Entry Order Type

### A. Order Type Selection

**Decision: Marketable Limit Orders**

Limit set at ask + small buffer (e.g. 0.5%) for buys. Behaves like a market order on liquid names while providing a hard price ceiling. Pre-submission R:R validation is the only gate — no post-fill corrective sells.

### B. Fill Acceptance Logic (R:R-based)

> Accept a fill at any price where risk-to-stop still meets minimum R:R threshold (e.g. 2:1) after all transaction costs.

The stop is a fixed technical level — a worse fill widens the risk band and compresses R:R:

| | Entry | Stop | Target | Risk | Reward | R:R |
|---|---|---|---|---|---|---|
| Signal price | €50.00 | €48.50 | €53.00 | €1.50 | €3.00 | 2:1 ✓ |
| Bad fill | €50.75 | €48.50 | €53.00 | €2.25 | €2.25 | 1:1 ✗ |

### C. Liquidity Tiering for EU Markets

| Liquidity | Approach | Rationale |
|---|---|---|
| Liquid names | Aggressive limit — ask + buffer | Fill quickly or cancel; spread is tight enough |
| Thin names | Passive limit — join the ask | Work the order patiently; avoid lifting a thin book |

### D. Tick Size Rounding

- Query tick size per instrument via IBKR `reqMarketRule` — never hardcode
- Handle tiered tick sizes (different tick above/below price thresholds)
- Floor for buy limits; ceil for sell limits

```python
def round_to_tick(price: float, tick_size: float) -> float:
    return round(math.floor(price / tick_size) * tick_size, 10)
```

### E. Real-Time Market Data via API

- **Event-driven, not polling:** IBKR TWS API pushes market data updates; bot subscribes per instrument
- **Exchange subscriptions:** configured directly on dedicated IBKR account during onboarding

> Data flow: Subscribe → Signal fires → Read live ask → Validate R:R incl. costs → Submit marketable limit → Fill confirmed

### F. Transaction Costs & Order Strategy

Belgian TOB 0.35% per transaction + exchange fees. Costs baked into pre-submission R:R — no corrective sells.

- Minimum R:R defined in after-cost terms (e.g. 2:1 net of ~0.7% round-trip)
- Order only submitted if cost-inclusive R:R passes; otherwise logged as skipped

> This eliminates post-fill corrective sells, keeping transaction count and tax exposure to a minimum.

---

## 5. Design Consideration 4 — Stop Order Type

### A. Stop Order Types Evaluated

| Type | Behaviour | Risk |
|---|---|---|
| Stop Market | Guaranteed exit at market price | Severe slippage on thin or fast markets |
| Stop Limit | Limit order on trigger | May not fill in a real breakdown — worst outcome |
| IBKR Stop with Protection | Market stop with price band | Good middle ground for liquid names |

### B. The Gap Problem

- Stop market: fills at open price after a gap — far through the intended level
- Stop limit: may not fill at all — position remains open in falling market
- Mitigations: avoid binary events, sector correlation awareness, daily loss limit halt

### C. Tiered Stop Logic

| Scenario | Stop Type | Rationale |
|---|---|---|
| Liquid names | Stop Limit (0.3–0.5% band) | Protects against whipsaw; exits in most breakdowns |
| Thin EU names | Stop Market | Stop Limit risks no fill; compensated by smaller size |
| All trades, overnight | Position sizing assumes 2× breach possible in gap scenario | — |

### D. Position Sizing Clarification

Position sizing is a damage containment tool, not an exit mechanism. Stop type determines exit quality; position sizing bounds the financial damage if the stop fails.

> Example: 1% portfolio risk, 3% stop distance. Gap to −12% on the stock causes ~4× intended portfolio risk — painful but survivable.

---

## 6. Design Consideration 5 — Automation & Human-in-the-Loop

### A. Approach: Hybrid Autonomy

**Decision: Hybrid Autonomy** — fully autonomous within defined parameters; outside those, trades are skipped not held for approval. Evolution path: progressively widen parameters toward full autonomy.

### B. Autonomous Trigger Conditions

All of the following must be true for the bot to trade autonomously:

- R:R passes at intended limit price (including transaction costs)
- Position size ≤ 1.5% portfolio risk
- Total open risk after trade ≤ 6% portfolio
- No open position already in this instrument
- Market open, not in auction / pre-open window
- No trading halt on the instrument
- Market data feed live and verified current
- No known binary event within N days
- Spread and volume meet minimum liquidity threshold
- IB Gateway stable — no recent reconnect flag

### C. Pause & Skip Conditions

| Condition | Action | Notes |
|---|---|---|
| R:R below threshold after costs | Skip | Logged as 'insufficient R:R after costs' |
| Position size would exceed 1.5% | Skip | Hard rule, never overridden |
| Total open risk would exceed 6% | Skip | Hard rule, never overridden |
| Earnings / binary event within N days | Skip + Notify | EU coverage caveat applies |
| Market data stale or interrupted | Skip + Notify | Do not trade on unverified data |
| Liquidity below threshold | Skip | Spread too wide or volume too low |
| Instrument already has open position | Skip | No unintentional pyramiding |
| Market closed or in auction | Skip | Re-evaluate at next open |
| Daily loss limit approaching | Skip + Notify | Threshold in config |
| Gateway reconnected recently | Skip + Notify | Clean state required |
| Ex-dividend date imminent | Skip + Notify | Price behaviour distorted around ex-div |

### D. Hard Risk Rules

| Rule | Value | Calculation |
|---|---|---|
| Max position risk | 1.5% of portfolio | (Entry − Stop) × Shares ÷ Portfolio Value |
| Max total open risk | 6% of portfolio | Sum of all open (Entry − Stop) × Shares ÷ Portfolio Value |
| Max simultaneous positions (full size) | 4 | Derived: 6% ÷ 1.5% = 4 positions |

### E. Earnings Dates for EU Stocks

- **Source:** IBKR `reqFundamentalData` — coverage varies; validate against live watchlist
- **Fallback:** flag as 'earnings date unknown', skip around results season or require manual confirmation
- **v1 status:** nice-to-have — best effort, do not block v1 launch on perfect EU coverage

### F. Future — Position Scoring & Swapping

Deferred to v3+: score open positions and new signals, swap stale positions for better opportunities at the 6% ceiling.

v2 lighter alternative: flag positions beyond expected hold window or within 10% of stop for human attention when ceiling is hit.

---

## 7. Design Consideration 6 — Hosting & Infrastructure

**Decision: Cloud from the start.** Development on laptop via VS Code with Claude plugin. The bot requires 24/7 uptime and a persistent IB Gateway session.

| Option | Cost | Specs | Notes |
|---|---|---|---|
| Oracle Cloud Free Tier | Free (permanent) | 4 ARM cores, 24GB RAM | Primary recommendation. EU presence. |
| Hetzner Cloud | ~€4–5/month | 2 cores, 2GB RAM | Fallback. EU-based, reliable, simple. |
| Render / Railway | Free tier | Variable | Not recommended — free tiers sleep. |

**IB Gateway on headless server via IBC:**

```
Server boots → IBC launches IB Gateway headlessly →
Gateway authenticates → Python bot connects → Markets open → Trading begins
```

| Phase | Environment | Gateway Mode |
|---|---|---|
| Development | Laptop / VS Code | IB Gateway locally, paper trading |
| Staging | Cloud server | IB Gateway via IBC, paper trading |
| Live | Cloud server | IB Gateway via IBC, live IBKR account |

> Paper trading on the cloud server is mandatory before any live capital is deployed.

---

## 8. Design Consideration 7 — Security

| Vector | What is at Risk | Primary Mitigation |
|---|---|---|
| IB Gateway credentials | Direct IBKR account login | Restricted file permissions, SSH key-only access |
| Running bot process | Orders through authenticated session | IBKR order limits and IP whitelist |
| Cloud server | Entry point to all of the above | SSH key-only login, firewall (port 22 only) |

> Withdrawals are always outside API scope. Worst case via API compromise is constrained trading losses, not total account loss.

| Layer | Control | Protects Against |
|---|---|---|
| IBKR account | Trusted IP whitelist (server fixed IP only) | API access from unknown IP |
| IBKR account | Order size & daily value limits | Runaway bot or malicious orders |
| IBKR account | Withdrawal requires separate auth | Capital removal via API |
| Server | SSH key-only, password auth disabled | Brute force access |
| Server | Firewall — port 22 only | Direct intrusion |
| Server | Credentials file restricted permissions | Local privilege escalation |
| Bot | Daily loss limit halt + notify | Bot malfunction |
| Operational | Notification on every trade | Catch anomalous activity |

> Configure IBKR trusted IP whitelist before any live trading begins. This is the single most effective control.

---

## 9. Design Consideration 8 — Account Setup

**Decision: Dedicated Direct IBKR Account**

A separate IBKR account opened solely for the automated bot. Existing Mexem account retained for manual trading and potentially cheaper passive ETF investing versus Bolero/KBC.

- Full direct control over API settings — IP whitelist, order limits, permissions — no introducing broker layer
- Direct IBKR support for API and technical issues
- Clean separation of P&L, positions, and risk between automated and manual trading
- Enables independent performance evaluation of the bot

---

## 10. Design Consideration 9 — Asset Scope

**Decision: Equities Only (v1), Long Only**

| Asset Class | Decision | Rationale |
|---|---|---|
| Equities (stocks) | In scope — v1, long only | Full system designed around this |
| ETFs | Out of scope for bot | Handled via separate account for passive investing |
| Options | Ruled out | Too many simultaneous factors: direction, time, magnitude |
| Futures | v2/v3 consideration | Margin mechanics, rollover, contract specs, extended hours |

---

## 11. Design Consideration 10 — Position Manager

The position manager handles all post-entry trade management: ATR trail calculation, stop updates via API, time-based exit evaluation, profit protection, and manual exit detection.

### A. Exit Philosophy

- No partial closes — entire position managed as single unit
- Initial target (1:2 R:R) is the minimum expectation, not a ceiling
- Goal: capture extended moves (1:5+) while protecting accumulated profit
- The stop is the only planned exit mechanism — initial stop, cost floor, or ATR trail
- Manual exits by the user are detected and recorded as a distinct exit type

### B. Trail Trigger & Stop Floor

At **1:1.5 R:R**: stop moves to entry + total transaction costs. ATR trailing begins simultaneously.

- Stop floor = entry + ~0.7–0.8% (cost-break-even level)
- Active stop = max(cost floor, ATR trail level)
- Stop only ever moves up — never down

> Triggering the trail at 1:1.5 rather than 1:2 gives more room for the trail to compound on extended moves.

### C. ATR Trailing Stop

**Calculation:**
- Source: Yahoo Finance daily OHLCV — 14-day ATR
- Anchor: running high since entry — trail only moves up as new highs are made
- Update: recalculated daily at session open; stop order updated via IBKR API

**Volatility Bucketing:**

| Volatility Bucket | ATR% Range | ATR Multiplier | Rationale |
|---|---|---|---|
| Low | < 1.5% | 2.0× | Tight trail appropriate; steady mover |
| Medium | 1.5% – 3.0% | 2.5× | Standard swing trade range |
| High | > 3.0% | 3.0× | More room needed to avoid noise stops |

```python
atr_trail_stop = running_high - (atr_14 * multiplier)
active_stop    = max(cost_floor, atr_trail_stop)
# Update IBKR stop order whenever active_stop rises above current stop
```

Per-stock ATR optimisation is a v3+ consideration once sufficient trade history exists.

### D. Time-Based Exit Logic

Default: **7 trading days.** Trail active = time limit ignored entirely.

Time exit only fires when: signal queue non-empty **AND** open risk ≥ 6%.

| Position State | Action | Rationale |
|---|---|---|
| Trail already active | Hold — trail manages exit | Trade in profit-protection mode |
| Within 20–30% of stop distance | Close | Stop likely incoming; recycle capital |
| At a loss but above stop | Hold — stop manages exit | Setup not yet invalidated |
| Flat, no opportunity cost | Hold | No reason to exit |

> The time exit targets stalled positions consuming capital needed elsewhere. The stop remains the primary invalidation signal for all positions.

### E. Manual Exit Handling

Manual exits occur when the user closes a position directly via the IBKR platform, bypassing the bot. The position manager must detect and record these cleanly.

**Detection — primary mechanism (automatic):**
`ib_insync` fires position change events whenever the IBKR account state changes. The position manager listens for position close events, compares against its own open position state, and if a position closes without a bot-initiated order, records it as `exit_reason: manual`.

**Detection — fallback (CLI):**
```
python -m position_manager close ASML.AS 72.50 manual
```
Used if the automatic detection misses an edge case (e.g. corporate action, Gateway was offline when the close occurred).

**What is logged on manual exit:**
- `exit_reason: manual`
- `bot_initiated: false`
- Exit price from the IBKR fill report (actual price)
- Exchange fill timestamp
- P&L calculated from actual fill vs entry price
- Optional free-text note field for the trading diary (e.g. "closed early — earnings risk")

**Valid exit reasons (structured field across all exit types):**

| Exit Reason | Initiated By | Description |
|---|---|---|
| `trail_hit` | Bot | ATR trailing stop was triggered |
| `stop_hit` | Bot | Initial or breakeven stop was triggered |
| `time_exit` | Bot | Time-based exit conditions were met |
| `manual` | User | Position closed directly via IBKR platform |

> The trading diary distinguishes bot-managed exits from manual overrides. The tax report treats all exit types equally — only the financial data matters.

### F. Combined Decision Tree

```
Position open
    ├── Price hits 1:1.5 R
    │       ├── Stop → entry + costs (cost floor)
    │       └── ATR trail begins from running high
    │               └── Trail stop hit → close entire position (exit_reason: trail_hit)
    ├── Time limit reached, trail NOT active
    │       ├── Signal queue empty OR open risk < 6% → hold
    │       └── Signal queue non-empty AND open risk ≥ 6%
    │               ├── Within 20–30% of stop → close (exit_reason: time_exit)
    │               └── Otherwise → hold, stop manages exit
    ├── Stop hit at any point → close entire position (exit_reason: stop_hit)
    └── Position closed externally → detect + log (exit_reason: manual)
```

---

## 12. Design Consideration 11 — Logging & Analytics

Logging is a first-class component. Every decision has financial consequences — the log must reconstruct exactly what happened and why.

| Component | Events & Calculations Logged |
|---|---|
| Data Fetcher | Fetch initiated · Ticker errors/retries · Cache hits vs misses · Delta rows appended · Full refresh triggered |
| Signal Engine | Signal fired (all parameters, conviction) · Signal skipped (reason) · Indicator values at signal time · Intraday run results |
| Risk Layer | Pre-trade check (portfolio value, size, open risk, pass/fail) · Hard rule triggered · Stop update received from Position Manager (RL-10) |
| Order Executor | Order submitted · Order filled (actual price, actual costs from IBKR) · Order cancelled · R:R validation inputs and result |
| Position Manager | Trail triggered · Trail updated · Time limit evaluation · Position closed (exit_reason, bot_initiated, P&L, note) |
| Market Data | Feed interruption · Liquidity check result · Earnings flag |
| Notifications | Every notification (type, content, delivery status) |
| System | Gateway connected/disconnected · Bot started/stopped · Daily loss limit status |

**Storage: Hybrid** — JSON lines (primary, per component per day) + SQLite (analytics, SSH tunnel only).

```sql
SELECT * FROM skipped_trades WHERE date > '2026-05-01' AND reason = 'insufficient_rr';
SELECT instrument, avg(fill_slippage) FROM fills GROUP BY instrument;
SELECT instrument, trail_trigger_price, close_price, pnl FROM position_manager_log;
SELECT exit_reason, count(*) FROM closed_positions GROUP BY exit_reason;
```

```
Your laptop  →  SSH tunnel (encrypted, port 22)  →  Cloud server  →  SQLite (localhost only)
```

> Database port is never open to the internet. All access routes through the SSH tunnel.

---

## 13. Design Consideration 12 — Reporting

The report generator is a separate, on-demand module that reads from the SQLite database and produces human-readable outputs. It serves two distinct purposes: a trading diary for performance review, and a tax export for annual compliance.

### A. Trading Diary (Per-Trade Reports)

Inspired by Alexander Elder's trade journal methodology. For each closed position, a structured report is generated covering both the financial outcome and the trade narrative.

**Report contents per trade:**

- **Price chart** with annotated entry, initial stop, cost-floor level, target, trail stop progression, and exit point
- **Signal metadata:** strategy type (pullback / breakout), conviction level, liquidity class, RS value at entry, MACD state at entry, `run_type` (eod / intraday)
- **Trade timeline:** entry date → trail trigger date (if applicable) → exit date, hold duration in trading days
- **Financial summary:** entry price, exit price, shares, gross P&L, actual costs (entry + exit commission from IBKR), net P&L
- **R multiple achieved** vs planned (e.g. planned 1:2, achieved 1:3.4)
- **Exit narrative:** exit reason (`trail_hit`, `stop_hit`, `time_exit`, `manual`), `bot_initiated` flag, optional note
- **All-time high reached** during the trade (for chart annotation and R multiple context)

**Format:** HTML report per trade — renderable in any browser, printable, archivable. Generated on demand, not in real time.

### B. Portfolio Summary Report

An on-demand snapshot of the current state:

- All open positions: instrument, entry date, entry price, current price, unrealised P&L, current stop level, trail status
- Running portfolio risk: current open risk %, proximity to 6% ceiling
- YTD closed trade summary: number of trades, win rate, average R multiple, gross and net P&L
- Daily loss limit status

### C. Annual Tax Export

**Purpose:** compliance with Belgian capital gains tax (applicable from 2027 on profits above €10,000 annually).

**Per-position fields required:**
- ISIN, instrument name, open date, close date, quantity
- Entry price, exit price, gross profit/loss
- Actual costs (TOB + commissions, entry + exit)
- Net profit/loss

**Annual aggregate row:** total gross profit, total costs, total net profit.

**Format:** CSV export per calendar year — simple to hand to an accountant or load into tax software.

> **ISIN note:** tax authorities typically require ISIN codes rather than exchange tickers. IBKR provides ISIN via contract details. Store it at signal time — don't rely on being able to look it up later.

### D. Technology

- **Charts:** Plotly (Python) — generates interactive HTML charts suitable for the per-trade diary
- **Tax export:** pandas → CSV from the SQLite positions table
- **Trigger:** CLI — `python -m report_generator trade ASML.AS 2026-04-12`, `python -m report_generator tax 2026`, `python -m report_generator summary`
- **No real-time dependency:** reads from SQLite only; does not connect to IBKR or the live bot

---

## 14. Pre-Build Decisions & System Parameters

### A. Signal Interface Contract

All fields required before the Order Executor will proceed:

| Field | Type | Description |
|---|---|---|
| instrument_id | str | IBKR contract ID (conid) |
| isin | str | ISIN code — sourced from IBKR contract details; stored for tax reporting |
| ticker | str | Human-readable ticker for logs and notifications |
| direction | str | 'long' — short out of scope for v1 |
| entry_price | float | Signal engine's intended entry level |
| stop_price | float | Technical stop level — fixed, not price-relative |
| target_price | float | Initial target (minimum 1:2 R:R before costs) |
| signal_type | str | Setup type (e.g. breakout, pullback) — used for time limit lookup |
| liquidity_class | str | 'liquid' or 'thin' — determines order and stop type |
| conviction | str | 'standard' or 'elevated' (both strategies fired, or near 52wk high) |
| signal_timestamp | datetime | UTC timestamp when signal was generated |
| earnings_flag | bool\|None | True if binary event within N days; None if unknown |
| run_type | str | 'eod' or 'intraday' — which engine run produced this signal |
| reference_price | float | EOD closing price used to calculate stop/target (may differ from entry_price on intraday runs) |

### B. Position Record Schema

Fields stored per position in SQLite (additions beyond the signal contract):

| Field | Type | Description |
|---|---|---|
| fill_price | float | Actual entry fill price from IBKR |
| fill_timestamp | datetime | Exchange fill timestamp |
| shares | int | Number of shares filled |
| entry_commission | float | Actual entry costs from IBKR (TOB + exchange fee) |
| exit_price | float | Actual exit fill price |
| exit_timestamp | datetime | Exchange exit timestamp |
| exit_commission | float | Actual exit costs from IBKR |
| exit_reason | str | Structured: trail_hit / stop_hit / time_exit / manual |
| bot_initiated | bool | False if position was closed manually by user |
| exit_note | str\|None | Optional free-text note for trading diary |
| peak_price | float | All-time high price reached during the trade |
| trail_triggered | bool | Whether the ATR trail was activated |
| trail_trigger_price | float\|None | Price at which trail was triggered |
| gross_pnl | float | (exit_price − entry_price) × shares |
| net_pnl | float | gross_pnl − entry_commission − exit_commission |

### C. Watchlist

- **Primary:** S&P 500, Nasdaq 100, Russell 2000 constituent lists — approximately 2,000 names; updated periodically as index compositions change
- **Secondary (deferred):** Euronext liquid names (Amsterdam, Paris, Brussels, Lisbon, Dublin) — activate when bot is profitable and €3/month IBKR data subscription is taken out; existing `eurostoxx600.txt` file covers the liquid overlap
- US equities only in v1 — no EU names, no ADRs until Euronext subscription is active

### D. Data Sources Summary

| Data Type | Source | Notes |
|---|---|---|
| Historical OHLCV | Yahoo Finance (yfinance) | Daily bars; delta load; Parquet cache |
| Real-time prices | IBKR TWS API | Event-driven; live ask/bid for order submission |
| Portfolio value | IBKR TWS API (live) | Queried live for all position sizing |
| Tick size | IBKR reqMarketRule | Per instrument, per price tier |
| ISIN + contract details | IBKR reqContractDetails | Fetched at signal time; stored on position record |
| Earnings dates | IBKR reqFundamentalData | Best-effort; conservative fallback |
| Market hours | IBKR trading hours per contract | For open/close pause conditions |
| Benchmark | Yahoo Finance (^GSPC) | RS line and market regime filter; ^STOXX50E added if Euronext universe activated |

### E. Notifications

- **Channel:** Telegram bot — free, reliable, simple API
- **Config:** bot token and chat ID in YAML config — never hardcoded
- **Format:** emoji-prefixed structured messages for quick mobile scanning
  - 🟢 BUY filled · 🔴 STOP hit · ⚠️ Skip · 🔔 Trail updated · 🕐 Time exit · ✋ Manual exit detected · ⚡ System event

### F. Configuration Structure

```yaml
risk:
  max_position_risk_pct: 1.5
  max_open_risk_pct: 6.0
  daily_loss_limit_pct: 3.0

costs:
  tob_pct: 0.35          # per transaction, both ways
  min_rr_after_costs: 2.0

data_fetcher:
  history_days: 700      # extended from 300 — supports WF warmup + 1yr simulation
  workers: 8
  batch_size: 16
  batch_pause_seconds: 2
  cache_dir: './cache'

signal_engine:
  ema_period: 21
  breakout_period: 50
  near_52wk_high_pct: 5
  benchmark: '^GSPC'
  intraday_runs:
    - time: "17:30"      # ~55% of US session elapsed (15:30–22:00 Belgian time)
      volume_multiplier: 1.8
    - time: "18:30"      # ~67% of US session elapsed
      volume_multiplier: 1.6
    - time: "19:30"      # ~78% of US session elapsed
      volume_multiplier: 1.5

position_manager:
  trail_trigger_r: 1.5
  time_limit_days: 7
  atr_period: 14
  stop_proximity_pct: 25
  atr_buckets:
    low_threshold_pct: 1.5
    high_threshold_pct: 3.0
    low_multiplier: 2.0
    medium_multiplier: 2.5
    high_multiplier: 3.0

orders:
  entry_buffer_pct: 0.5
  stop_limit_band_pct: 0.4

watchlist:
  sp500: true
  nasdaq100: true
  russell2000: true
  euronext: false        # activate with €3/month IBKR subscription when profitable
  custom: []

notifications:
  telegram_bot_token: ''
  telegram_chat_id: ''

logging:
  log_dir: './logs'
  sqlite_path: './data/trading.db'
  retain_days: 90

reporting:
  report_dir: './reports'
  tax_year_start_month: 1   # January

walk_forward:
  db_path: './data/wf_sim.db'  # isolated from risk.db — never share these paths
```

---

## 15. Design Consideration 13 — IBKR Connectivity

---

### A. Component Inventory

There are four distinct pieces of software involved in the IBKR integration. They are separate programs that work together — understanding what each one does prevents confusion during setup and debugging.

#### IB Gateway

A program provided by IBKR that runs permanently on the cloud server and maintains an authenticated session with IBKR's systems. It is the only component that ever communicates with IBKR over the internet. Your Python bot never talks to IBKR directly — it only talks to Gateway, and Gateway handles everything else.

Gateway is the stripped-down sibling of Trader Workstation (TWS), the full desktop trading platform. TWS is designed for humans — charts, watchlists, a full GUI. Gateway has none of that. It exists solely to expose a local connection that your code can use. It uses significantly less memory and CPU than TWS, and it is designed for programmatic, always-on use. **Gateway is the right choice for this system; TWS is not.**

One important property: **orders and stop orders placed via Gateway live at IBKR's side once submitted.** If the bot crashes after placing a stop, the stop is still active. The bot's own memory is not the source of truth for live orders — IBKR's systems are.

#### IBC (IB Controller)

An open-source tool that automates the Gateway startup sequence. Gateway is a Java application that, even in server mode, starts with a login dialog requiring a username and password to be entered. Without IBC, someone would need to log in manually every time Gateway starts or restarts — which on a headless cloud server would require a remote desktop session.

IBC handles: injecting credentials at startup, clicking through any confirmation dialogs, and managing the scheduled daily restart (Gateway requires a brief restart once per day; the time is configurable, typically set to 23:30 when markets are closed).

**The startup sequence on the server is:**
```
Server boots
    → IBC launches
        → IBC starts IB Gateway
            → IBC injects credentials and clicks through login
                → Gateway authenticates with IBKR
                    → Gateway listens for local connections
                        → Python bot connects
```

Without IBC, this chain requires manual intervention. With IBC, the entire sequence is automated and survives server reboots.

#### ibapi

IBKR's official Python client library. This is the low-level layer — it handles the actual message formatting and the connection to Gateway's local port. It works via callbacks: you define functions like `orderStatus()` and `position()`, and the library calls them when data arrives from Gateway. It is functional but verbose — managing the request/response state across callbacks requires significant boilerplate.

`ibapi` must be installed but is rarely used directly in this codebase.

#### ib_insync

A community-built wrapper around `ibapi` that makes the API behave like a normal sequential Python program rather than a callback maze. It adds an event loop under the hood and exposes clean, readable calls:

```python
# With ib_insync — reads as a normal function call
positions = ib.positions()

# Without it (raw ibapi) — send a request, then handle the result
# in a completely separate callback method elsewhere in the code
self.reqPositions()
# ... later, in a different method the library calls automatically:
def position(self, account, contract, position, avgCost):
    ...
```

`ib_insync` also exposes events your code can subscribe to — `ib.orderStatusEvent`, `ib.positionEvent`, etc. — which is how the Position Manager detects manual exits and how the Order Executor receives fill confirmations without polling.

**Decision: ib_insync is the Python interface used throughout this system.** It is widely used for exactly this use case, actively maintained, and integrates cleanly with a scheduler. Both `ibapi` and `ib_insync` must be installed as dependencies.

---

### B. Connection Architecture

```
Cloud Server
┌─────────────────────────────────────────────────────┐
│                                                     │
│   IBC                                               │
│    └── launches and manages                         │
│         IB Gateway  ←──── authenticated session ──────────► IBKR Servers ──► Euronext / XETRA
│              ↑                                      │
│              │ local connection (same machine)      │
│              │                                      │
│         Python Bot                                  │
│          ├── Signal Engine  (reads live prices)     │
│          ├── Order Executor (submits orders)        │
│          ├── Position Manager (updates stops)       │
│          └── Risk Layer (reads portfolio value)     │
│                                                     │
└─────────────────────────────────────────────────────┘
```

Gateway and the Python bot run on the same server and communicate locally — no network request leaves the server for this leg of the journey. The internet-facing connection is solely between Gateway and IBKR's servers.

**Local ports used:**

| Mode | Port |
|---|---|
| Live trading | 4001 |
| Paper trading | 4002 |

These ports are only reachable from within the server itself. They are never exposed to the internet (enforced by the server firewall — see SI-04).

---

### C. Which Components Use Gateway, and For What

The Data Fetcher and Report Generator never touch Gateway. Every other component does, for specific purposes:

| Component | Uses Gateway for | Type of interaction |
|---|---|---|
| Signal Engine | Live ask price (intraday runs) | Subscribes to live price stream; Gateway pushes updates |
| Signal Engine | Contract details and ISIN | Request/response — called once per new signal |
| Signal Engine | Tick size per instrument | Request/response — called once per instrument |
| Risk Layer | Live portfolio value | Request/response — called before every position sizing calculation |
| Risk Layer | Current open positions on startup | Request/response — called once at bot startup to sync state |
| Order Executor | Submit entry limit orders | One-way instruction; fill confirmation arrives as an event |
| Order Executor | Submit initial stop orders | One-way instruction after fill confirmed |
| Order Executor | Verify market open / auction status | Read from contract details (trading hours field) |
| Position Manager | Modify stop orders (trail updates) | One-way instruction each time the active stop level rises |
| Position Manager | Detect manual exits | Subscribes to position change events; Gateway pushes unsolicited updates |

---

### D. Order and Fill Lifecycle

The sequence from Risk Layer approval to portfolio state fully updated:

**1. Risk Layer approves** — produces a RiskDecision (shares, stop price, approved = True). Nothing has touched Gateway yet.

**2. Order Executor checks the live price** — asks Gateway for the current ask on the instrument. Gateway has been receiving a live price stream since market open and answers immediately. The Executor performs the final cost-inclusive R:R check against this live price. If the stock has run since the signal was generated and R:R no longer clears, the order is skipped and logged.

**3. Order Executor builds the order** — calculates the limit price (live ask + 0.5% buffer, rounded down to the correct tick), assembles the order instruction. Still local, nothing sent.

**4. Order sent to Gateway** — the instruction is passed to Gateway via ib_insync. Gateway forwards it to IBKR's servers, which validate it (buying power, permissions, market open) and route it to the exchange. The order is now live in the exchange order book.

**5. Fill event arrives** — when a matching seller is found, the exchange executes the trade and notifies IBKR's servers. IBKR notifies Gateway. Gateway pushes a fill event to the bot. The bot did not poll — Gateway sent it unsolicited. The fill event contains: actual fill price, exchange timestamp, actual commission charged.

**6. Order Executor records the fill** — logs fill price, timestamp, and costs. Calls Risk Layer to register the open position.

**7. Risk Layer updates state** — stores the open position in SQLite (using actual fill price, not signal price). Open risk budget updates. Future signals will see the reduced budget in Check 6.

**8. Position Manager takes over** — immediately submits the initial stop order to Gateway. The stop now lives at IBKR — it will execute even if the bot is offline when it triggers. Position Manager stores the stop order ID for future modifications.

**9. Telegram notification** — sent after fill and stop confirmation.

---

### E. Connection Lifecycle

Gateway requires a scheduled daily restart (a limitation of the application). This is configured in IBC to occur at **23:30 local time** — after Euronext and XETRA close (17:30) and before any pre-market activity. During the restart window (typically under 2 minutes), the bot's connection to Gateway is briefly lost.

**On disconnect:**

The bot detects the disconnect via ib_insync's `disconnectedEvent`. It enters a waiting state: no signals are evaluated, no orders are submitted. A Telegram notification is sent.

**On reconnect:**

ib_insync's `connectedEvent` fires. The bot re-syncs its state from IBKR: it queries open positions and compares against its own SQLite records to detect any fills or stops that executed during the gap. After state sync is confirmed, the bot resumes normal operation — but skips placing any new orders for a brief settling period (OE-10). A Telegram notification confirms reconnection.

**Stops during a disconnect:**

Because stop orders live at IBKR (not in the bot's memory), they remain active during any Gateway disconnect or bot downtime. A stop hit while the bot is offline will execute normally. The bot will detect the resulting position closure when it reconnects and re-syncs state.

---

### F. Paper vs Live Configuration

IBKR provides a paper trading account alongside the live account. Both are accessible via the same Gateway installation; the distinction is made at login time and reflected in the port number.

The config file carries a top-level `mode` flag:

```yaml
ibkr:
  mode: paper          # 'paper' | 'live'
  paper_port: 4002
  live_port: 4001
  host: 127.0.0.1      # always localhost — Gateway runs on same server
  gateway_con: 1       # connection label — arbitrary number, not your account number;
                       # must be unique if multiple scripts connect to Gateway simultaneously
  readonly: false
```

All components read `ibkr.mode` at startup and connect to the corresponding port. No other code changes are required to switch between paper and live. **Switching to live requires a deliberate config edit and a bot restart — it cannot happen accidentally.**

The paper account has its own portfolio value (configured in IBKR's paper trading settings), its own position state, and its own order history. P&L from paper trading is simulated and has no financial consequence.

**Mandatory progression before live trading:**
1. Full development and unit testing against paper account on laptop
2. Full pipeline integration testing against paper account on cloud server
3. Minimum two weeks of paper trading in production configuration with no open issues
4. Manual review of all paper trade logs before switching mode to live

---

### G. IBC Credential Storage

IBC stores your IBKR login in a plain text file on the server, typically at `/opt/ibc/config.ini`:

```ini
IbLoginId=your_ibkr_username
IbPassword=your_ibkr_password
TradingMode=paper   # switch to 'live' when ready
```

Because this file contains brokerage credentials in plain text, two controls are mandatory:

**File permissions:** the file must be readable only by the user account that runs the bot. Set once with `chmod 600 /opt/ibc/config.ini` and it persists. This is what SI-05 refers to.

**Two-factor authentication:** IBKR's default login requires a second factor (they call it the Secure Login System), which IBC cannot handle interactively on a headless server. The solution is IBKR's **trusted IP exemption**: once your server's fixed IP is whitelisted in the IBKR account portal, IBKR skips the second factor for logins originating from that IP. This is why SI-01 (the IP whitelist) must be configured before IBC can log in headlessly — the whitelist serves double duty as both a security control and the 2FA bypass mechanism.

**The config.ini file is never committed to version control.** It lives only on the server.

---

### H. One-Time Account Setup Checklist

These steps are performed once when the IBKR account is approved and Gateway is installed. They are configuration actions in the IBKR account portal and Gateway settings, not code.

- [ ] Enable API access in IBKR account settings (Client Portal → Settings → API)
- [ ] Set trusted IP whitelist to the cloud server's fixed IP address — this also enables the 2FA exemption required for headless IBC login (SI-01)
- [ ] Configure account-level daily order value limit and maximum order size (SI-02)
- [ ] Subscribe to required market data packages: Euronext Amsterdam, Euronext Paris, Euronext Brussels, XETRA, Borsa Italiana, BME, Nasdaq Helsinki, Nasdaq Stockholm, Wiener Börse, Euronext Lisbon, Euronext Dublin, London Stock Exchange
- [ ] Enable paper trading account and note the paper username
- [ ] Install IB Gateway on the cloud server
- [ ] Create `/opt/ibc/config.ini` with credentials; set permissions to `chmod 600` (SI-05)
- [ ] Configure IBC with 23:30 daily restart schedule
- [ ] Verify Gateway starts headlessly and bot can connect on paper port (4002)
- [ ] Confirm market data streams are live for at least one instrument from each exchange

---

## 16. Design Consideration 14 — Walk-Forward Simulator

The Walk-Forward Simulator (WF) replays the full Signal Engine → Risk Layer → Sim Executor → Sim Position Manager pipeline on historical OHLCV data, advancing one trading day at a time, to measure strategy performance without lookahead bias.

It is not a throwaway prototype. It runs on demand via CLI and accumulates results across multiple runs in a persistent simulation database, enabling comparison between parameter configurations and strategy iterations.

### A. Design Philosophy

**Reuse, don't replicate.** Every existing component is called with its real logic unchanged. The WF module's job is to drive those components with a time-restricted view of historical data — not to re-implement their logic in a simulation context.

The only genuinely new code is:
- The **DataFrame Walker** — intercepts cache reads and returns truncated data
- The **Sim Position Manager** — evaluates exit conditions bar by bar using historical OHLCV
- The **day loop orchestrator** — advances the date cursor and coordinates component calls
- The **storage layer** — writes results to `wf_sim.db`

Everything else is reuse: Signal Engine, Risk Layer, Sim Executor, `pm_math` shared functions, Report Generator.

### B. The DataFrame Walker

The Walker is a drop-in replacement for the `data_fetcher.cache` module. It exposes the same interface — `load(ticker, cache_dir) → DataFrame` — but returns a truncated view: all rows where `date <= current_simulation_date`.

The Signal Engine receives the Walker via constructor injection (SE-25). All cache reads during simulation flow through the Walker. This is the single enforcement point for lookahead prevention.

**The Walker does not copy or modify Parquet files.** It reads from the same files as the live system and applies the date filter at read time. Full historical data always remains on disk.

The Walker is testable in isolation: given a ticker, a cache directory, and a date, assert that the returned DataFrame's last index equals exactly that date.

### C. Signal Engine Constructor Injection (SE-25)

One small change to `signal_engine/engine.py` enables the Walker to be passed in:

```python
# Before
def __init__(self, config: dict, logger: Logger) -> None:
    # internally calls: cache_store.load(ticker, self._cache_dir)

# After
def __init__(self, config: dict, logger: Logger, cache=cache_store) -> None:
    self._cache = cache
    # internally calls: self._cache.load(ticker, self._cache_dir)
```

In production nothing changes — the default `cache=cache_store` means existing call sites require no modification. The Walk-Forward Simulator passes its Walker as `cache=walker`. The Signal Engine has no knowledge of the substitution.

### D. Day Loop

The simulator advances through contiguous trading days from `start_date` to `end_date`. On each iteration the loop is "between" two trading days: day D-1 has just closed, and day D is about to be revealed.

```
For each trading day D (from first warm day to end_date):

    Step 1 — Signal Engine scans on D-1 close data
        Walker is set to D-1; engine.scan(tickers) → raw signals

    Step 2 — Rank signals
        Elevated conviction first
        Within same conviction tier: tightest stop distance % of entry

    Step 3 — Risk Layer evaluates ranked signals
        For each signal in ranked order:
            RL.evaluate(signal) with simulated portfolio value and isolated DB
            If approved: queue fill at P_close(D-1)
            If rejected: record in wf_signals with reject reason

    Step 4 — Reveal day D bar
        Walker advances to D
        For each queued fill:
            If bar_D.low <= P_close(D-1) <= bar_D.high → entry confirmed at P_close(D-1)
            Else (price gapped away) → entry skipped, record as 'rejected_gap'

    Step 5 — Sim Position Manager evaluates ALL open positions against bar D
        Check exits in this order (first match per position wins):
            1. Gap stop:     bar_D.open <= active_stop → exit at bar_D.open
            2. Intraday stop: bar_D.low <= active_stop → exit at active_stop
            3. Trail trigger: bar_D.close >= entry + 1.5R → activate trail,
                              move stop to cost floor, begin trail from running_high
            4. Trail update:  trail active AND bar_D.high > running_high →
                              recalculate atr_trail_stop, update active_stop upward only
            5. Time exit:     hold_days >= 7 AND trail not active →
                              evaluate conditions → exit at bar_D.close if met

    Step 6 — Update simulated portfolio value
        portfolio_value += sum(net_pnl) for positions closed this iteration

    Step 7 — Store equity curve row
        wf_equity_curve: (run_id, date=D, portfolio_value, open_positions, open_risk_pct)
```

**Why signals first, exits second:** the EOD scan fires on the prior day's close, before day D opens. Entries are queued at that close price. Exits resolve during day D's session. A position opened at D-1's close and stopped out on day D's bar is a real and correctly captured outcome.

**Gap check on entry:** if day D's bar does not overlap P_close(D-1), the entry is skipped — we do not chase gaps. The signal is discarded; the engine may re-fire it on a subsequent day if the setup remains valid.

**Daily loss limit:** the Risk Layer's RL-06 pause flag is date-scoped and resets automatically on a new calendar day. In the simulator each loop iteration is a complete session, so the flag resets between iterations naturally. No special WF handling needed.

### E. Exit Fill Assumptions

**Gap-aware stop fill — single mode, no configuration needed.**

```python
if bar_D.open <= active_stop:
    exit_price = bar_D.open        # gapped through stop overnight
elif bar_D.low <= active_stop:
    exit_price = active_stop       # stop hit intraday
```

If the stock opens below the stop (overnight gap), the fill is at the open. If it trades through the stop intraday, the fill is at the stop price. No optimistic "always fill at stop" assumption.

### F. Signal Ranking

When multiple signals compete for a constrained risk budget, signals are processed in this priority order:

1. **Elevated conviction** before standard conviction
2. Within the same conviction tier: **tightest stop distance % of entry** first

Rationale for tightest-stop priority: a compact stop indicates a lower-volatility, higher-quality setup and consumes less risk budget, leaving room for additional positions.

### G. Sim Position Manager

The Sim PM implements the same exit logic as the live Position Manager but driven by historical OHLCV bars rather than live price events. It is a **separate module** from the live PM — the live PM is event-driven (IBKR callbacks), while the Sim PM is a deterministic bar-by-bar function. Combining them would add simulation-specific branches to production code with no production benefit.

**Shared pure functions:** ATR multiplier lookup, cost floor calculation, and active stop computation are extracted into a shared `pm_math.py` module imported by both the live PM and the Sim PM. Exit logic is not duplicated — only the data delivery mechanism differs.

**Zero-day stop-out detection:** if a position opens and stops out on the same bar D, this is logged explicitly as a data quality warning. It indicates the stop was placed above the day's low, which the signal engine's structural stop logic should prevent.

### H. State Isolation

Each WF run uses an isolated SQLite database (`./data/wf_sim.db` by default, separate from `risk.db`). The live system state is never read or written during a simulation run.

**Safety assert on startup:** the simulator aborts if `wf.db_path == risk.db_path`. One wrong config value must never corrupt live state.

The simulated portfolio value is injected into the Risk Layer via the same `portfolio_value_stub` config path. It updates after each day's exits are resolved, so the next iteration's Risk Layer calls see the current simulated portfolio value.

### I. History Depth and Warmup

**Minimum cache depth:** 700 calendar days (set globally in `data_fetcher.history_days`).

**Warmup:** the Signal Engine's `self._min_bars` is the binding constraint (EMA200 = 200 bars). The Walker skips any day D where fewer than `min_bars` of data exist for the benchmark ticker. The simulation summary reports warmup days skipped and the first effective simulation date explicitly.

**Effective simulation depth with a 700-day cache:**
- ~480 trading days total
- ~200 bars warmup
- ~280 tradeable simulation days (~13 months)

### J. Intraday Signals

Intraday Strategy B runs are not simulated in v1. All simulated signals are EOD only (`run_type: eod`). Intraday execution quality (live price at run time, volume extrapolation) cannot be reliably reconstructed from daily OHLCV. This is a known limitation, documented in the module README and recorded in the `wf_runs` metadata table per run.

### K. Storage Schema

All simulation data is stored in `wf_sim.db`. Multiple runs accumulate in the same database, each identified by a UUID `run_id`. This enables comparison across runs and parameter configurations without re-running.

**`wf_runs` — one row per simulation run:**

| Field | Description |
|---|---|
| `run_id` | UUID generated at run start |
| `start_date` | First simulation day requested |
| `effective_start_date` | First day after warmup — actual first day simulated |
| `end_date` | Last simulation day |
| `starting_portfolio_value` | Initial simulated portfolio value |
| `final_portfolio_value` | Portfolio value at end of simulation |
| `config_snapshot` | Full `config.yaml` serialised as JSON |
| `run_timestamp` | Wall-clock time the simulation was executed (UTC) |
| `universe_size` | Number of tickers in the watchlist |
| `trading_days_simulated` | Count of days the cursor advanced |
| `intraday_simulated` | Always `0` in v1 |
| Aggregate stats | Populated at run end — see Section L |

**`wf_signals` — every signal evaluated, all outcomes:**

| Field | Description |
|---|---|
| `run_id` | FK to `wf_runs` |
| `signal_date` | Day D-1 (the close date that generated the signal) |
| `ticker` | Instrument ticker |
| `signal_type` | `pullback` / `breakout` / `pullback+breakout` |
| `conviction` | `standard` / `elevated` |
| `entry_price` | Signal entry price (D-1 close) |
| `stop_price` | Technical stop at signal time |
| `stop_pct` | Stop distance as % of entry |
| `target_price` | 2:1 R:R target |
| `strategy_a_fired` | Boolean |
| `strategy_b_fired` | Boolean |
| `outcome` | `filled` / `rejected_risk` / `rejected_gap` / `rejected_duplicate` / `rejected_budget` |
| `reject_reason` | RL reject reason code if `outcome = rejected_risk` |

**`wf_positions` — same schema as `risk_positions`, plus `run_id`:**

Full `risk_positions` schema plus `run_id` and `r_multiple` (net_pnl / initial_risk_amount). This enables the existing Report Generator to produce per-trade HTML diaries and tax CSV exports from simulation data without modification.

**`wf_equity_curve` — daily portfolio value per run:**

| Field | Description |
|---|---|
| `run_id` | FK to `wf_runs` |
| `date` | Simulation day D |
| `portfolio_value` | Portfolio value at end of day D (after exits resolved) |
| `open_positions` | Count of open positions at end of day D |
| `open_risk_pct` | Total open risk % at end of day D |

### L. Aggregate Output

Printed at run end and stored in `wf_runs`:

| Metric | Description |
|---|---|
| Total trades | Count of all closed positions |
| Win rate | % of trades with net_pnl > 0 |
| Average R multiple | Mean of net_pnl / initial_risk_amount |
| Expectancy | (win_rate × avg_win_R) − (loss_rate × avg_loss_R) |
| Max consecutive losses | Longest losing streak |
| Max drawdown | Largest peak-to-trough decline in simulated equity curve |
| Exit reason breakdown | Count and % by `trail_hit` / `stop_hit` / `time_exit` |
| Strategy breakdown | Count and avg R by strategy_a / strategy_b / both |
| Signal conversion rate | Signals fired vs signals filled |
| Gap rejection rate | Signals skipped due to entry gap |
| Average hold duration | Mean trading days per closed trade |

### M. CLI Interface

```bash
# Run full simulation on cached data
python -m walk_forward_simulator

# Specify date range
python -m walk_forward_simulator --start 2025-01-01 --end 2026-01-01

# Custom starting portfolio value
python -m walk_forward_simulator --portfolio 100000

# Dry run — advance cursor and log, no writes to wf_sim.db
python -m walk_forward_simulator --dry-run

# Print aggregate summary for a specific run
python -m walk_forward_simulator --summary <run_id>

# List all stored runs with key metrics
python -m walk_forward_simulator --list-runs

# Custom config
python -m walk_forward_simulator --config /path/to/config.yaml
```

### N. Known Limitations (v1)

Documented in the module README and stored in the `wf_runs` metadata per run:

- Entry slippage not modelled — all entries fill at EOD close price
- Intraday signals not simulated — EOD only
- Transaction costs use flat TOB estimate (0.35%), not actual IBKR commission schedule
- Market impact of simulated trades on price not modelled
- EMA200 warmup reduces effective simulation window by ~200 bars (~10 months)

---

## 17. Functional Requirements

Priority: **Must** (non-negotiable) · **Should** (strongly recommended for v1) · **Could** (deferrable)

---

### Data Fetcher

| ID | Component | Requirement | Priority |
|---|---|---|---|
| DF-01 | Data Fetcher | Fetch daily OHLCV data for all watchlist instruments using yfinance | Must |
| DF-02 | Data Fetcher | On first run: fetch full 700-day history per ticker | Must |
| DF-03 | Data Fetcher | On subsequent runs: delta load — fetch only missing days since last cached date | Must |
| DF-04 | Data Fetcher | Cache OHLCV in Parquet format, one file per ticker in `cache/` directory | Must |
| DF-05 | Data Fetcher | Validate cache on load: detect duplicate rows, date gaps, dtype consistency | Must |
| DF-06 | Data Fetcher | Fetch in parallel: ThreadPoolExecutor, 8 workers, batches of 16, 2s pause between batches | Must |
| DF-07 | Data Fetcher | Handle yfinance errors and retries gracefully; log failed tickers without crashing | Must |
| DF-08 | Data Fetcher | Support `--full-refresh` CLI flag to force complete re-fetch | Must |
| DF-09 | Data Fetcher | Fetch benchmark index (^GSPC) for RS line and market regime calculations; fetch ^STOXX50E additionally if Euronext universe is active | Must |
| DF-10 | Data Fetcher | Log fetch summary: tickers attempted, succeeded, failed, rows added, duration | Must |
| DF-11 | Data Fetcher | Support EODHD as drop-in replacement if yfinance quality degrades | Could |

### Signal Engine

| ID | Component | Requirement | Priority |
|---|---|---|---|
| SE-01 | Signal Engine | Implement Strategy A: EMA Pullback — price pulls back to 21 EMA in uptrend with MACD histogram turning up | Must |
| SE-02 | Signal Engine | Implement Strategy B: Breakout — price closes above 50-day highest high with MACD line above zero and rising | Must |
| SE-03 | Signal Engine | Run Strategy A and B independently; signal fires if either passes (OR logic) | Must |
| SE-04 | Signal Engine | Annotate elevated conviction when both strategies fire simultaneously | Should |
| SE-05 | Signal Engine | Annotate elevated conviction when price is within 5% of 52-week high | Should |
| SE-06 | Signal Engine | Emit structured signal with all required interface fields including ISIN and run_type (see Section 14A) | Must |
| SE-07 | Signal Engine | Fetch ISIN and instrument name from IBKR reqContractDetails at signal time | Must |
| SE-08 | Signal Engine | Classify instrument liquidity (liquid / thin) at signal time | Should |
| SE-09 | Signal Engine | Calculate RS line vs ^GSPC benchmark per instrument (^STOXX50E for EU names if Euronext universe active) | Should |
| SE-10 | Signal Engine | Apply market regime filter — reduce or pause signals in bear market conditions | Should |
| SE-11 | Signal Engine | Flag instruments with upcoming binary events before emitting signal | Should |
| SE-12 | Signal Engine | Log every signal fired with full parameters, indicator values, and conviction level | Must |
| SE-13 | Signal Engine | Log every signal skipped with reason | Must |
| SE-14 | Signal Engine | Support Eurostoxx 600 universe + custom fixed list as watchlist sources | Must |
| SE-15 | Signal Engine | Run scheduled intraday scans at 17:30, 18:30, and 19:30 Belgian time (11:30, 12:30, 13:30 ET) for Strategy B only | Must |
| SE-16 | Signal Engine | Intraday mode: evaluate price condition against IBKR live price; extrapolate intraday volume to full-session equivalent | Must |
| SE-17 | Signal Engine | Intraday mode: apply per-run volume multipliers (17:30 → 1.8×, 18:30 → 1.6×, 19:30 → 1.5×) configurable in YAML | Must |
| SE-18 | Signal Engine | Intraday mode: reuse EMA chain, MACD, and market regime from EOD Parquet data — do not recalculate from partial intraday bars | Must |
| SE-19 | Signal Engine | Intraday mode: re-validate R:R from scratch against live ask price at run time — R:R gate is never relaxed | Must |
| SE-20 | Signal Engine | Maintain in-memory session rejection store: tickers rejected for compressed R:R in any intraday run are skipped in all subsequent intraday runs that session | Must |
| SE-21 | Signal Engine | Intraday deduplication: skip tickers with an open position, a pending order, or an entry in the session rejection store | Must |
| SE-22 | Signal Engine | Tickers that fail the price condition intraday (no longer above 50-day high) are NOT added to the rejection store — a clean breakout later in the session is a fresh signal | Must |
| SE-23 | Signal Engine | Add `run_type` field ('eod' \| 'intraday') to signal payload; add `reference_price` field (EOD close used for stop/target calculation) | Must |
| SE-24 | Signal Engine | Log intraday run results: tickers evaluated, skipped (with reason), signals fired, rejection store additions | Must |
| SE-25 | Signal Engine | Accept optional `cache` parameter in constructor (default: real `cache_store`); use `self._cache.load()` internally — enables DataFrame Walker injection for walk-forward simulation without any other Signal Engine code changes | Must |

### Risk Layer

| ID | Component | Requirement | Priority |
|---|---|---|---|
| RL-01 | Risk Layer | Hard cap: max position risk 1.5% of portfolio per trade. Never overridden. | Must |
| RL-02 | Risk Layer | Hard cap: max total open risk 6% of portfolio. Never overridden. | Must |
| RL-03 | Risk Layer | Calculate position size from (entry − stop) and live portfolio value from IBKR API | Must |
| RL-04 | Risk Layer | Query live portfolio value from IBKR TWS API for all position sizing | Must |
| RL-05 | Risk Layer | Track all open positions and risk contribution in real time | Must |
| RL-06 | Risk Layer | Enforce daily loss limit; pause trading and notify when breached | Must |
| RL-07 | Risk Layer | Prevent unintentional pyramiding — duplicate instrument check | Must |
| RL-08 | Risk Layer | Monitor sector/correlation exposure across open positions | Should |
| RL-09 | Risk Layer | Apply smaller position sizing for thin/illiquid instruments | Should |
| RL-10 | Risk Layer | Recalculate and update `risk_amount` for an open position whenever the Position Manager reports a stop update; once the active stop is at or above entry + costs, that position contributes zero to the open risk budget (RL-02) | Must |

### Order Executor

| ID | Component | Requirement | Priority |
|---|---|---|---|
| OE-01 | Order Executor | Validate full cost-inclusive R:R before submitting. Skip if below threshold. | Must |
| OE-02 | Order Executor | Submit entries as marketable limit orders — never pure market orders | Must |
| OE-03 | Order Executor | Include TOB 0.35% each way + exchange fees in all R:R calculations | Must |
| OE-04 | Order Executor | Query tick size via reqMarketRule and round all prices to valid tick multiples | Must |
| OE-05 | Order Executor | Handle tiered tick sizes (different tick above/below price thresholds) | Must |
| OE-06 | Order Executor | Two-tier limit logic: aggressive for liquid names, passive for thin names | Should |
| OE-07 | Order Executor | Stop Limit (0.3–0.5% band) for liquid; Stop Market for thin names | Must |
| OE-08 | Order Executor | Verify market open and not in auction before submitting | Must |
| OE-09 | Order Executor | Verify market data feed live and current before any order | Must |
| OE-10 | Order Executor | Flag and skip first trade after Gateway reconnect; notify | Should |
| OE-11 | Order Executor | Check ex-dividend date proximity before submission | Should |
| OE-12 | Order Executor | Log all submissions, fills, and cancellations including actual IBKR-reported costs | Must |
| OE-13 | Order Executor | Never submit corrective sell after entry fill — exits via stop/target/trail only | Must |

### Position Manager

| ID | Component | Requirement | Priority |
|---|---|---|---|
| PM-01 | Position Manager | Monitor all open positions continuously throughout the session | Must |
| PM-02 | Position Manager | At 1:1.5 R: move stop to entry + total transaction costs (cost floor) | Must |
| PM-03 | Position Manager | At 1:1.5 R: begin ATR trailing stop from running high since entry | Must |
| PM-04 | Position Manager | Calculate ATR trail using 14-day ATR from Yahoo Finance daily OHLCV | Must |
| PM-05 | Position Manager | Classify stock into volatility bucket and apply corresponding ATR multiplier | Must |
| PM-06 | Position Manager | Active stop = max(cost floor, ATR trail level) — stop only moves up, never down | Must |
| PM-07 | Position Manager | Update IBKR stop order via API whenever active stop level rises | Must |
| PM-08 | Position Manager | Log every trail update: running high, new trail level, new active stop, timestamp | Must |
| PM-09 | Position Manager | Evaluate time-based exit after 7 trading days if trail not yet active | Must |
| PM-10 | Position Manager | Time exit only fires if: signal queue non-empty AND open risk ≥ 6% | Must |
| PM-11 | Position Manager | When time exit conditions met: close only if within 20–30% of stop distance; otherwise hold | Must |
| PM-12 | Position Manager | Never force-close a position at a loss based on time alone — stop manages invalidation | Must |
| PM-13 | Position Manager | Detect position closures not initiated by the bot; record as exit_reason: manual | Must |
| PM-14 | Position Manager | Support CLI fallback for manual exit recording: close <ticker> <price> manual | Must |
| PM-15 | Position Manager | Store bot_initiated flag and optional exit_note on all closed position records | Must |
| PM-16 | Position Manager | Track and store peak_price (all-time high during trade) for diary reporting | Must |
| PM-17 | Position Manager | Log all time exit evaluations with conditions checked, decision, and rationale | Must |
| PM-18 | Position Manager | Notify on trail trigger, significant trail updates, time exit decisions, and manual exit detection | Must |
| PM-19 | Position Manager | All multipliers and time limit configurable in YAML config | Must |
| PM-20 | Position Manager | Notify the Risk Layer whenever the active stop is updated, passing ticker and new stop level so the Risk Layer can recalculate the live risk amount for that position (see RL-10) | Must |

### Market Data

| ID | Component | Requirement | Priority |
|---|---|---|---|
| MD-01 | Market Data | Subscribe to real-time market data streams via IBKR TWS API (event-driven) | Must |
| MD-02 | Market Data | Configure and verify exchange subscriptions on dedicated IBKR account during onboarding | Must |
| MD-03 | Market Data | Detect and handle stale data feed — halt trading and notify if interrupted | Must |
| MD-04 | Market Data | Assess spread width and volume at signal time for liquidity classification | Should |
| MD-05 | Market Data | Source earnings dates via IBKR reqFundamentalData; conservative fallback if unavailable | Should |

### Notification Layer

| ID | Component | Requirement | Priority |
|---|---|---|---|
| NL-01 | Notifications | Deliver all notifications via Telegram bot (token and chat ID in YAML config) | Must |
| NL-02 | Notifications | Notify on trade executed: instrument, fill price, size, stop, target, R:R | Must |
| NL-03 | Notifications | Notify on trade skipped with reason | Should |
| NL-04 | Notifications | Notify on stop hit or target reached with trade summary and P&L | Must |
| NL-05 | Notifications | Notify on trail triggered and significant trail stop updates | Must |
| NL-06 | Notifications | Notify on time exit evaluation decision | Should |
| NL-07 | Notifications | Notify on manual exit detected (bot_initiated: false) | Must |
| NL-08 | Notifications | Notify on any pause condition triggered | Must |
| NL-09 | Notifications | Use emoji-prefixed structured format for quick mobile scanning | Should |
| NL-10 | Notifications | Notifications are informational only — no action required under normal operation | Must |
| NL-11 | Notifications | Notify on intraday signal fired: include run time, live entry price, R:R, and volume confirmation ratio | Should |

### Logging & Analytics

| ID | Component | Requirement | Priority |
|---|---|---|---|
| LL-01 | Logging | Structured JSON line logs per component, one file per component per day | Must |
| LL-02 | Logging | Log every calculation with inputs and outputs (R:R, sizing, tick rounding, ATR trail) | Must |
| LL-03 | Logging | Log every event across all components with full context and timestamp | Must |
| LL-04 | Logging | Populate SQLite analytics DB from log processor for structured post-trade queries | Must |
| LL-05 | Logging | Store actual IBKR-reported costs (entry + exit commission) on every closed position record | Must |
| LL-06 | Logging | Store ISIN, exit_reason, bot_initiated, exit_note, peak_price on every closed position record | Must |
| LL-07 | Logging | Retain full audit trail of all system decisions | Must |
| LL-08 | Logging | SQLite accessible via SSH tunnel only — database port never exposed to internet | Must |
| LL-09 | Logging | Analytics dashboard (daily summary: trades, skips, open risk, P&L, system health) | Could |

### Report Generator

| ID | Component | Requirement | Priority |
|---|---|---|---|
| RG-01 | Report Generator | Generate per-trade HTML report including annotated price chart, signal metadata, trade timeline, financial summary, and R multiple achieved | Must |
| RG-02 | Report Generator | Annotate chart with: entry, initial stop, cost-floor level, target, trail stop progression, exit point, peak price | Must |
| RG-03 | Report Generator | Include exit narrative in report: exit_reason, bot_initiated flag, run_type, optional exit_note | Must |
| RG-04 | Report Generator | Generate portfolio summary report: open positions, running risk, YTD closed trade stats | Should |
| RG-05 | Report Generator | Generate annual tax export as CSV: all closed positions with ISIN, dates, prices, quantities, gross P&L, actual costs, net P&L | Must |
| RG-06 | Report Generator | Tax export covers one calendar year; filterable by year via CLI | Must |
| RG-07 | Report Generator | Tax export includes annual aggregate row: total gross profit, total costs, total net profit | Must |
| RG-08 | Report Generator | All reports generated on demand via CLI — not in real time | Must |
| RG-09 | Report Generator | Report generator reads from SQLite only — no live IBKR connection required | Must |
| RG-10 | Report Generator | Reports saved to configurable output directory (reporting.report_dir in config) | Must |

### Security & Infrastructure

| ID | Component | Requirement | Priority |
|---|---|---|---|
| SI-01 | Security | Configure IBKR trusted IP whitelist to server's fixed IP before any live trading | Must |
| SI-02 | Security | Configure IBKR account-level order size and daily value limits | Must |
| SI-03 | Security | Server: SSH key-only login, password authentication disabled | Must |
| SI-04 | Security | Server: firewall with port 22 only; Gateway and database not publicly exposed | Must |
| SI-05 | Security | IB Gateway credentials stored with restricted file permissions on server | Must |
| SI-06 | Infrastructure | Use IBC to run IB Gateway headlessly and manage automated re-authentication | Must |
| SI-07 | Infrastructure | Cloud server maintains fixed IP address (required for IBKR IP whitelist) | Must |
| SI-08 | Infrastructure | All development and staging against paper trading account before live deployment | Must |
| SI-09 | Infrastructure | Dedicated direct IBKR account for bot — separate from Mexem manual account | Must |
| SI-10 | Infrastructure | All system parameters in YAML config — no magic numbers in code | Must |

### Walk-Forward Simulator

| ID | Component | Requirement | Priority |
|---|---|---|---|
| WF-01 | Walk-Forward Simulator | Advance a date cursor through contiguous trading days from `start_date` to `end_date`; on each day present only data up to that date to all downstream components | Must |
| WF-02 | Walk-Forward Simulator | Implement DataFrame Walker: drop-in replacement for `data_fetcher.cache` exposing the same `load(ticker, cache_dir)` interface; returns `df[df.index <= current_date]` — no Parquet files modified | Must |
| WF-03 | Walk-Forward Simulator | Inject DataFrame Walker into Signal Engine via constructor parameter (SE-25); no other Signal Engine code changes | Must |
| WF-04 | Walk-Forward Simulator | Skip warmup days: do not begin simulation until `min_bars` of data exist for the benchmark; log warmup days skipped and first effective simulation date in run summary | Must |
| WF-05 | Walk-Forward Simulator | Day loop order per iteration: (1) signal engine scans on D-1 close, (2) rank signals, (3) risk layer evaluates, (4) reveal bar D and confirm/reject entries on gap check, (5) sim PM evaluates all open positions against bar D, (6) update portfolio value, (7) store equity curve row | Must |
| WF-06 | Walk-Forward Simulator | Signal ranking: elevated conviction first; within same tier, tightest stop distance % of entry first | Must |
| WF-07 | Walk-Forward Simulator | Gap check on entry: if bar_D does not overlap P_close(D-1), skip entry and record outcome as `rejected_gap` in `wf_signals` | Must |
| WF-08 | Walk-Forward Simulator | Gap-aware exit fill: `exit_price = bar_D.open` if `bar_D.open <= active_stop`; else `exit_price = active_stop` if `bar_D.low <= active_stop` | Must |
| WF-09 | Walk-Forward Simulator | Implement Sim Position Manager: evaluate stop hit, trail trigger, trail update, and time exit conditions for each open position on each bar D | Must |
| WF-10 | Walk-Forward Simulator | Sim PM exit priority order per bar: (1) gap stop, (2) intraday stop, (3) trail trigger, (4) trail update, (5) time exit | Must |
| WF-11 | Walk-Forward Simulator | Extract shared ATR multiplier lookup, cost floor calculation, and active stop computation into `pm_math.py`; import in both live PM and Sim PM — no logic duplication | Must |
| WF-12 | Walk-Forward Simulator | Log zero-day stop-outs (position opened and closed on same bar) as explicit data quality warnings | Should |
| WF-13 | Walk-Forward Simulator | Use isolated SQLite database (`wf_sim.db`) for all simulation state; never read or write `risk.db` during a simulation run | Must |
| WF-14 | Walk-Forward Simulator | Assert on startup that `wf.db_path != risk.db_path`; abort with clear error message if equal | Must |
| WF-15 | Walk-Forward Simulator | Inject simulated portfolio value into Risk Layer via `portfolio_value_stub` config path; update after each day's exits are resolved | Must |
| WF-16 | Walk-Forward Simulator | Store every signal evaluated (all outcomes) in `wf_signals` table with `run_id`, `outcome`, and `reject_reason` | Must |
| WF-17 | Walk-Forward Simulator | Store every closed position in `wf_positions` table using full `risk_positions` schema plus `run_id` and `r_multiple` | Must |
| WF-18 | Walk-Forward Simulator | Store daily equity curve in `wf_equity_curve` table: `run_id`, `date`, `portfolio_value`, `open_positions`, `open_risk_pct` | Must |
| WF-19 | Walk-Forward Simulator | Store run metadata in `wf_runs` table including full config snapshot, date range, universe size, trading days simulated, and aggregate statistics | Must |
| WF-20 | Walk-Forward Simulator | Accumulate multiple runs in `wf_sim.db` identified by `run_id` — never overwrite prior runs | Must |
| WF-21 | Walk-Forward Simulator | Print and store aggregate statistics at run end: total trades, win rate, average R, expectancy, max drawdown, max consecutive losses, exit reason breakdown, strategy breakdown, signal conversion rate, gap rejection rate, average hold duration | Must |
| WF-22 | Walk-Forward Simulator | Support CLI flags: `--start`, `--end`, `--portfolio`, `--dry-run`, `--summary <run_id>`, `--list-runs`, `--config` | Must |
| WF-23 | Walk-Forward Simulator | Dry-run mode: advance cursor, evaluate signals, log outcomes, make no writes to `wf_sim.db` | Must |
| WF-24 | Walk-Forward Simulator | Do not simulate intraday signals in v1; all simulated signals are `run_type='eod'`; document as known limitation in module README and `wf_runs` metadata | Must |
| WF-25 | Walk-Forward Simulator | `wf_positions` schema is compatible with the Report Generator — per-trade HTML diaries and tax CSV exports can be generated from simulation data without Report Generator code changes | Must |
| WF-26 | Walk-Forward Simulator | `history_days` set to 700 globally in `data_fetcher` config; no separate WF-specific fetch config needed | Must |

---

*— End of Document —*
