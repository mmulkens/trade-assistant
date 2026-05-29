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
13. [Pre-Build Decisions & System Parameters](#13-pre-build-decisions--system-parameters)
14. [IBKR API — Preliminary Notes](#14-ibkr-api--preliminary-notes)
15. [Functional Requirements](#15-functional-requirements)

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
    ├── Position Manager     → ATR trail, cost floor, time-based exits
    │
    ├── Logging Layer        → JSON lines + SQLite analytics DB
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

- Day 1: fetch full 300-day history per ticker
- Day 2+: fetch only missing days since last cached date, append to Parquet
- Eliminates 299 redundant days of fetching on every run after initialisation
- Performance: 300 tickers × 300 days serially = minutes; 300 tickers × 1–3 days in parallel = seconds

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

**Decision: EU equities, EUR-denominated, Euronext + XETRA primary**

- Euronext (AMS: .AS, PAR: .PA, BRU: .BR) and XETRA Frankfurt (.DE)
- EUR denomination eliminates FX risk
- Universe: full Eurostoxx 600 constituent list + custom fixed list of smaller EU-denominated stocks

*US tickers:* stubbed in settings but inactive by default. Phase 3+ expansion.
*Rejected:* mandatory US market inclusion in Phase 1 — FX complexity without clear benefit.

### F. Benchmark

**Decision: `^STOXX50E` (Eurostoxx 50) as primary benchmark**

- Used for RS (Relative Strength) line calculation in the signal engine
- Used for market regime filter (reduce exposure in bear market conditions)
- Swap to `^GSPC` or run dual benchmarks if US tickers are activated

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

**Decision: scheduled intraday runs at 13:30, 14:30, and 15:30 (local exchange time), targeting Strategy B only.**

The intraday runs do not replace the EOD run — they complement it. The EOD run fires at market open (or pre-market) on EOD data and places resting limit orders for pullback signals. The intraday runs catch breakouts developing during the session and enter with a marketable limit at the current ask.

**What each intraday run evaluates (Strategy B conditions only):**

- EMA chain alignment and market regime: sourced from the Parquet cache (daily calculations — unchanged intraday)
- Price condition: `current live price > 50-day highest high` — evaluated against the IBKR real-time feed
- Volume confirmation: intraday volume extrapolated to full-session equivalent and checked against 1.5× 20-day average volume
- MACD: trusted from EOD data — partial-day MACD is too noisy to recalculate reliably intraday
- R:R: re-validated from scratch against the live ask price at the moment of each run

**Volume extrapolation:**
```python
session_minutes_total = 510          # Euronext: 09:00–17:30
elapsed_minutes = now - market_open
extrapolated_volume = intraday_volume * (session_minutes_total / elapsed_minutes)
confirmed = extrapolated_volume >= volume_multiplier * avg_20d_volume
```

**Stricter volume multipliers for earlier runs** (less session elapsed = more extrapolation uncertainty):

| Run time | Volume multiplier | Session elapsed (approx.) |
|---|---|---|
| 13:30 | 1.8× | ~55% |
| 14:30 | 1.6× | ~67% |
| 15:30 | 1.5× | ~78% (standard) |

**R:R gate is always hard — never relaxed for intraday runs.** A breakout that has already moved so far that R:R is compressed is a worse trade regardless of conviction level. The three runs provide multiple opportunities to catch the signal at a viable price; if none of them produce a valid R:R, the trade is correctly skipped.

**Intraday deduplication — three layers:**

1. Open position check (Risk Layer): if the ticker already has an open position, skip.
2. Pending order check: if the ticker already has a live unconfirmed order from any earlier run today, skip.
3. Session rejection store: if the ticker was evaluated and **rejected for compressed R:R** in any earlier intraday run today, it is added to an in-memory `intraday_rejections` store and skipped in all subsequent runs for the remainder of the session.

The session rejection store is in-memory only and resets at the start of each trading day. It exists to prevent a "gap-and-crap" scenario: a stock that spiked at open (compressing R:R at 13:30), then partially faded (appearing to recover at 15:30), is not a clean setup and should not be re-evaluated.

Note: tickers that fail the **price condition** (no longer above the 50-day high) at a given run time are NOT added to the rejection store — a fresh, clean breakout developing later in the session is a legitimate new signal.

**EOD self-deduplication via freshness check:** if an intraday run results in a fill on day N, the EOD run on day N+1 will see that yesterday's close was already above the 50-day high and the existing Strategy B freshness check will suppress the signal naturally. No additional logic required.

**What changes in the codebase:**
- `signal_engine`: add `intraday_mode` flag — runs Strategy B only, accepts live price and intraday volume as inputs, skips all EOD-only indicator recalculation
- Scheduler: add three jobs at 13:30, 14:30, 15:30 calling the engine in `intraday_mode`
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
| ETFs | Out of scope for bot | Handled via Mexem for passive investing |
| Options | Ruled out | Too many simultaneous factors: direction, time, magnitude |
| Futures | v2/v3 consideration | Margin mechanics, rollover, contract specs, extended hours |

---

## 11. Design Consideration 10 — Position Manager

The position manager handles all post-entry trade management: ATR trail calculation, stop updates via API, time-based exit evaluation, and profit protection.

### A. Exit Philosophy

- No partial closes — entire position managed as single unit
- Initial target (1:2 R:R) is the minimum expectation, not a ceiling
- Goal: capture extended moves (1:5+) while protecting accumulated profit
- The stop is the only exit mechanism — initial stop, cost floor, or ATR trail

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

### E. Combined Decision Tree

```
Position open
    ├── Price hits 1:1.5 R
    │       ├── Stop → entry + costs (cost floor)
    │       └── ATR trail begins from running high
    │               └── Trail stop hit → close entire position
    ├── Time limit reached, trail NOT active
    │       ├── Signal queue empty OR open risk < 6% → hold
    │       └── Signal queue non-empty AND open risk ≥ 6%
    │               ├── Within 20–30% of stop → close
    │               └── Otherwise → hold, stop manages exit
    └── Stop hit at any point → close entire position
```

---

## 12. Design Consideration 11 — Logging & Analytics

Logging is a first-class component. Every decision has financial consequences — the log must reconstruct exactly what happened and why.

| Component | Events & Calculations Logged |
|---|---|
| Data Fetcher | Fetch initiated · Ticker errors/retries · Cache hits vs misses · Delta rows appended · Full refresh triggered |
| Signal Engine | Signal fired (all parameters, conviction) · Signal skipped (reason) · Indicator values at signal time |
| Risk Layer | Pre-trade check (portfolio value, size, open risk, pass/fail) · Hard rule triggered |
| Order Executor | Order submitted · Order filled · Order cancelled · R:R validation inputs and result |
| Position Manager | Trail triggered · Trail updated · Time limit evaluation · Position closed (reason, P&L) |
| Market Data | Feed interruption · Liquidity check result · Earnings flag |
| Notifications | Every notification (type, content, delivery status) |
| System | Gateway connected/disconnected · Bot started/stopped · Daily loss limit status |

**Storage: Hybrid** — JSON lines (primary, per component per day) + SQLite (analytics, SSH tunnel only).

```sql
SELECT * FROM skipped_trades WHERE date > '2026-05-01' AND reason = 'insufficient_rr';
SELECT instrument, avg(fill_slippage) FROM fills GROUP BY instrument;
SELECT instrument, trail_trigger_price, close_price, pnl FROM position_manager_log;
```

```
Your laptop  →  SSH tunnel (encrypted, port 22)  →  Cloud server  →  SQLite (localhost only)
```

> Database port is never open to the internet. All access routes through the SSH tunnel.

---

## 13. Pre-Build Decisions & System Parameters

### A. Signal Interface Contract

All fields required before the Order Executor will proceed:

| Field | Type | Description |
|---|---|---|
| instrument_id | str | IBKR contract ID (conid) |
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

### B. Watchlist

- **Primary:** Eurostoxx 600 full constituent list — static file updated periodically
- **Secondary:** custom fixed list of smaller EU-denominated stocks, manually curated
- EU-denominated equities only — no US names, no ADRs in v1

### C. Data Sources Summary

| Data Type | Source | Notes |
|---|---|---|
| Historical OHLCV | Yahoo Finance (yfinance) | Daily bars; delta load; Parquet cache |
| Real-time prices | IBKR TWS API | Event-driven; live ask/bid for order submission |
| Portfolio value | IBKR TWS API (live) | Queried live for all position sizing |
| Tick size | IBKR reqMarketRule | Per instrument, per price tier |
| Earnings dates | IBKR reqFundamentalData | Best-effort; conservative fallback |
| Market hours | IBKR trading hours per contract | For open/close pause conditions |
| Benchmark | Yahoo Finance (^STOXX50E) | RS line and market regime filter |

### D. Notifications

- **Channel:** Telegram bot — free, reliable, simple API
- **Config:** bot token and chat ID in YAML config — never hardcoded
- **Format:** emoji-prefixed structured messages for quick mobile scanning
  - 🟢 BUY filled · 🔴 STOP hit · ⚠️ Skip · 🔔 Trail updated · 🕐 Time exit · ⚡ System event

### E. Configuration Structure

```yaml
risk:
  max_position_risk_pct: 1.5
  max_open_risk_pct: 6.0
  daily_loss_limit_pct: 3.0

costs:
  tob_pct: 0.35          # per transaction, both ways
  min_rr_after_costs: 2.0

data_fetcher:
  history_days: 300
  workers: 8
  batch_size: 16
  batch_pause_seconds: 2
  cache_dir: './cache'

signal_engine:
  ema_period: 21
  breakout_period: 50
  near_52wk_high_pct: 5
  benchmark: '^STOXX50E'
  intraday_runs:
    - time: "13:30"
      volume_multiplier: 1.8
    - time: "14:30"
      volume_multiplier: 1.6
    - time: "15:30"
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
  eurostoxx600: true
  custom: []

notifications:
  telegram_bot_token: ''
  telegram_chat_id: ''

logging:
  log_dir: './logs'
  sqlite_path: './data/trading.db'
  retain_days: 90
```

---

## 14. IBKR API — Preliminary Notes

- **TWS API via IB Gateway:** preferred — designed for API-only use, lighter than full TWS
- **Python `ib_insync`:** standard wrapper; event-driven, asyncio-based
- **Paper trading:** mandatory for all development and testing before live deployment
- **Direct IBKR account:** API permissions, IP whitelist, and order limits configured directly without introducing broker involvement

---

## 15. Functional Requirements

Priority: **Must** (non-negotiable) · **Should** (strongly recommended for v1) · **Could** (deferrable)

---

### Data Fetcher

| ID | Component | Requirement | Priority |
|---|---|---|---|
| DF-01 | Data Fetcher | Fetch daily OHLCV data for all watchlist instruments using yfinance | Must |
| DF-02 | Data Fetcher | On first run: fetch full 300-day history per ticker | Must |
| DF-03 | Data Fetcher | On subsequent runs: delta load — fetch only missing days since last cached date | Must |
| DF-04 | Data Fetcher | Cache OHLCV in Parquet format, one file per ticker in `cache/` directory | Must |
| DF-05 | Data Fetcher | Validate cache on load: detect duplicate rows, date gaps, dtype consistency | Must |
| DF-06 | Data Fetcher | Fetch in parallel: ThreadPoolExecutor, 8 workers, batches of 16, 2s pause between batches | Must |
| DF-07 | Data Fetcher | Handle yfinance errors and retries gracefully; log failed tickers without crashing | Must |
| DF-08 | Data Fetcher | Support `--full-refresh` CLI flag to force complete re-fetch | Must |
| DF-09 | Data Fetcher | Fetch benchmark index (^STOXX50E) for RS line and market regime calculations | Must |
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
| SE-06 | Signal Engine | Emit structured signal with all required interface fields (see Section 13A) | Must |
| SE-07 | Signal Engine | Classify instrument liquidity (liquid / thin) at signal time | Should |
| SE-08 | Signal Engine | Calculate RS line vs ^STOXX50E benchmark per instrument | Should |
| SE-09 | Signal Engine | Apply market regime filter — reduce or pause signals in bear market conditions | Should |
| SE-10 | Signal Engine | Flag instruments with upcoming binary events before emitting signal | Should |
| SE-11 | Signal Engine | Log every signal fired with full parameters, indicator values, and conviction level | Must |
| SE-12 | Signal Engine | Log every signal skipped with reason | Must |
| SE-13 | Signal Engine | Support Eurostoxx 600 universe + custom fixed list as watchlist sources | Must |
| SE-14 | Signal Engine | Run scheduled intraday scans at 13:30, 14:30, and 15:30 (local exchange time) for Strategy B only | Must |
| SE-15 | Signal Engine | Intraday mode: evaluate price condition against IBKR live price; extrapolate intraday volume to full-session equivalent | Must |
| SE-16 | Signal Engine | Intraday mode: apply per-run volume multipliers (13:30 → 1.8×, 14:30 → 1.6×, 15:30 → 1.5×) configurable in YAML | Must |
| SE-17 | Signal Engine | Intraday mode: reuse EMA chain, MACD, and market regime from EOD Parquet data — do not recalculate from partial intraday bars | Must |
| SE-18 | Signal Engine | Intraday mode: re-validate R:R from scratch against live ask price at run time — R:R gate is never relaxed | Must |
| SE-19 | Signal Engine | Maintain in-memory session rejection store: tickers rejected for compressed R:R in any intraday run are skipped in all subsequent intraday runs that session | Must |
| SE-20 | Signal Engine | Intraday deduplication: skip tickers with an open position, a pending order, or an entry in the session rejection store | Must |
| SE-21 | Signal Engine | Tickers that fail the price condition intraday (no longer above 50-day high) are NOT added to the rejection store — a clean breakout later in the session is a fresh signal | Must |
| SE-22 | Signal Engine | Add `run_type` field ('eod' \| 'intraday') to signal payload; add `reference_price` field (EOD close used for stop/target calculation) | Must |
| SE-23 | Signal Engine | Log intraday run results: tickers evaluated, skipped (with reason), signals fired, rejection store additions | Must |

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
| OE-12 | Order Executor | Log all submissions, fills, cancellations with full parameters | Must |
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
| PM-13 | Position Manager | Log all time exit evaluations with conditions checked, decision, and rationale | Must |
| PM-14 | Position Manager | Notify on trail trigger, significant trail updates, and time exit decisions | Must |
| PM-15 | Position Manager | All multipliers and time limit configurable in YAML config | Must |
| PM-16 | Position Manager | Notify the Risk Layer whenever the active stop is updated, passing ticker and new stop level so the Risk Layer can recalculate the live risk amount for that position (see RL-10) | Must |

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
| NL-07 | Notifications | Notify on any pause condition triggered | Must |
| NL-08 | Notifications | Use emoji-prefixed structured format for quick mobile scanning | Should |
| NL-09 | Notifications | Notifications are informational only — no action required under normal operation | Must |
| NL-10 | Notifications | Notify on intraday signal fired: include run time, live entry price, R:R, and volume confirmation ratio | Should |

### Logging & Analytics

| ID | Component | Requirement | Priority |
|---|---|---|---|
| LL-01 | Logging | Structured JSON line logs per component, one file per component per day | Must |
| LL-02 | Logging | Log every calculation with inputs and outputs (R:R, sizing, tick rounding, ATR trail) | Must |
| LL-03 | Logging | Log every event across all components with full context and timestamp | Must |
| LL-04 | Logging | Populate SQLite analytics DB from log processor for structured post-trade queries | Must |
| LL-05 | Logging | Retain full audit trail of all system decisions | Must |
| LL-06 | Logging | SQLite accessible via SSH tunnel only — database port never exposed to internet | Must |
| LL-07 | Logging | Analytics dashboard (daily summary: trades, skips, open risk, P&L, system health) | Could |

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

---

*— End of Document —*
