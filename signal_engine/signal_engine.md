# Signal Engine

The Signal Engine is the second component in the Trade Assistant pipeline. It runs once per trading day, scans the full EU equity watchlist, and identifies stocks that have set up a high-quality entry condition. For every qualifying stock it emits a structured **Signal** — a precise set of trade parameters that the downstream Risk Layer uses to decide whether and how to place an order.

```
Data Fetcher  ──►  Signal Engine  ──►  Risk Layer  ──►  Order Executor  ──►  Position Manager
(Parquet cache)   (this module)
```

---

## What it produces

A Signal is not a recommendation to buy. It is a statement that, *as of today's close*, a particular stock satisfies a defined set of technical conditions. The Risk Layer still applies portfolio-level checks (position sizing, open risk limits) before any order is placed.

Each Signal contains:

| Field | Description |
|---|---|
| `ticker` | Yahoo Finance ticker, e.g. `ASML.AS` |
| `entry_price` | Today's closing price — the intended entry level |
| `stop_price` | The technical stop loss, fixed at signal time |
| `target_price` | The minimum acceptable target (2:1 reward:risk) |
| `signal_type` | `pullback`, `breakout`, or `pullback+breakout` |
| `conviction` | `standard` or `elevated` |
| `liquidity_class` | `liquid` or `thin` — affects order type downstream |
| `stop_capped` | `True` if the hard cap was applied to the stop distance |
| `strategy_a_fired` | Whether the EMA Pullback strategy triggered |
| `strategy_b_fired` | Whether the Breakout strategy triggered |
| `near_52wk_high` | Whether price is within 5% of its 52-week high |
| `market_regime` | `bull` or `bear` at the time of the scan |
| `rs_value` | Stock price divided by the ^STOXX50E benchmark price |
| `earnings_flag` | `None` — stub until IBKR fundamental data is wired up |

---

## How to run

```bash
# Scan the full watchlist (from config.yaml)
python -m signal_engine

# Scan specific tickers only
python -m signal_engine ASML.AS SIE.DE ALV.DE

# Custom config file
python -m signal_engine --config /path/to/config.yaml
```

Results are printed to the terminal and saved to `./data/signals.db` (SQLite).  
Detailed logs go to `./logs/signal_engine_YYYY-MM-DD.jsonl` (JSON-lines).

---

## Full evaluation pipeline

For every scan run, the engine works through the following steps in order.

### Step 1 — Market regime check

Before looking at any individual stock, the engine checks the health of the broad market by loading the ^STOXX50E index from the cache and comparing its closing price to its 200-period EMA.

- **Bull** (`close ≥ EMA200`): scan proceeds.
- **Bear** (`close < EMA200`): the entire scan is aborted and zero signals are returned.

**Why this matters:** individual stock setups — even technically perfect ones — have significantly lower follow-through probability when the broad market is in a downtrend. This is a hard gate, not a soft filter.

---

### Step 2 — General trend filters (applied to every ticker, before any strategy)

These two checks run before either strategy is evaluated. A ticker that fails either filter is skipped entirely, regardless of what the strategies would say.

#### Guard A — EMA chain alignment

```
EMA21 > EMA50 > EMA100 > EMA200
```

All four exponential moving averages must be in descending order from fastest to slowest. This confirms the stock is in a multi-timeframe uptrend. The EMA21 is the most sensitive of the four — it turns negative (below EMA50) in a downtrending stock well before the slower averages catch up. This makes it an early-warning gate.

**Why this applies to breakouts too:** a stock can technically close above its 50-day high while its EMAs are still in a downtrend alignment — for example, after a sharp drop followed by a single strong recovery bar. That is not the setup we want. The EMA chain check ensures any breakout or pullback signal is in the context of a genuine multi-week uptrend.

#### Guard B — Freefall rejection

```
(highest high of last 20 bars − close) / highest high  ≤  30%
```

If the stock is more than 30% below its 20-day high, it is in a freefall. Because EMAs are lagging indicators, a stock in freefall can still pass Guard A for several bars while the slower EMAs gradually catch up to the new price reality. Guard B closes this window by directly measuring recent price deterioration.

---

### Step 3 — Strategy evaluation (OR logic)

Both strategies are evaluated independently. A signal fires if **either** passes. Both firing simultaneously is rare — a pullback and a breakout are nearly mutually exclusive at the same moment — but when it happens, it triggers an elevated conviction annotation.

---

## Strategy A — EMA Pullback

**The idea:** buy a strong uptrending stock at a natural support level, catching the earliest sign that sellers are exhausted.

All four conditions must be true on the same bar.

### Condition 1 — Price above EMA50

```
close > EMA50
```

The Guard A chain already confirms EMA21 > EMA50. This additional check ensures *price itself* has not broken below the medium-term trend line. A stock where EMA21 > EMA50 but price has dropped below EMA50 is structurally weakening.

### Condition 2 — EMA21 touch or recovery

This is the precise definition of a "pullback to the 21 EMA". Two cases are accepted:

**Case A — Same-bar wick rejection:**
```
today.low  ≤  EMA21
today.close  >  EMA21
```
The candle's intraday low touched or pierced the EMA, but the day closed back above it. Classic support-holding behaviour — sellers drove price into the EMA and buyers pushed it back up before the close.

**Case B — Prior-bar breach, today's recovery:**
```
(yesterday.low < EMA21  OR  yesterday.close < EMA21)
AND  today.close > EMA21
```
Price dipped below (or closed below) the EMA on the previous bar. Today it has fully reclaimed the level. A one-day-delayed recovery is equally valid as a setup signal.

**What does NOT qualify:**
- Price approaching the EMA from above without touching it (proximity is not contact)
- Price below the EMA with no recovery candle (that is a breakdown, not a pullback)
- Price below the EMA for two or more bars without reclaiming it

### Condition 3 — MACD histogram "dark red to light red"

The MACD histogram (fast EMA minus slow EMA, minus the signal line) must show a specific three-bar shape:

```
h[-2] < h[-3]    — prior bar: histogram was getting more negative  (prior weakness)
h[-1] > h[-2]    — today: histogram has ticked up                  (first improvement)
h[-1] < 0        — histogram is still negative                     (not yet extended)
```

In plain terms: selling pressure was intensifying, and has now just begun to ease — but momentum has not yet turned positive. This is the earliest possible signal of a rotation from selling to buying interest, and it confirms we are entering at the turning point rather than chasing a move already in progress.

**Why the prior-decline requirement?** Without it, the histogram check could fire on a positive histogram that happens to have ticked up slightly — completely the wrong market condition for a pullback entry. Requiring that the histogram was previously declining confirms there was genuine selling pressure to recover from.

---

## Strategy B — 50-Day Breakout

**The idea:** buy a stock the moment it breaks out to new highs, when institutional money is visibly committing to the move.

All three conditions must be true on the same bar.

### Condition 1 — Fresh breakout above 50-day high

**Price condition:**
```
today.close  >  max(high, prior 50 trading days)
```
Today's close exceeds the highest high of the past 50 trading days (today's bar is excluded from the lookback to avoid look-ahead bias).

**Why 50 days?** A 20-day lookback fires too frequently — too many short-lived false breaks. A 52-week lookback fires too rarely to be useful as a daily scanner. 50 days is the practical balance between signal frequency and conviction.

**Freshness check — this must be the first day of the breakout:**
```
yesterday.close  ≤  max(high, the 50 days ending two days ago)
```
If yesterday's close already exceeded the 50-day high as it stood at yesterday's close, the breakout already fired yesterday. Signalling again today means entering a day (or more) late, after the stock has already moved. This matters for two reasons:

1. The intended entry price has deteriorated — you are chasing.
2. The stop distance inflates: the structural swing low is fixed at the breakout level while the entry price has risen, which pushes the risk percentage higher and causes more signals to hit the hard cap unnecessarily.

The freshness check ensures the engine only signals on day one.

### Condition 2 — MACD line above zero and rising

```
MACD line > 0                          — broad trend momentum is positive
MACD line today > MACD line yesterday  — momentum is currently accelerating
```

The MACD *line* (not histogram) is used here, because it reflects sustained trend direction rather than short-term momentum oscillation. A line above zero means the fast EMA is above the slow EMA — the stock has been trending up for an extended period. A rising line confirms that trend is strengthening on the breakout day.

### Condition 3 — Volume confirmation

```
today.volume  ≥  1.5 ×  avg(volume, last 20 trading days)
```

A breakout without elevated volume is suspect. Volume represents conviction — when institutional money is buying, it shows up in the volume data. A stock that closes above its 50-day high on average or below-average volume is more likely to stall and reverse than to follow through. The 1.5× threshold is the standard from IBD / O'Neil breakout methodology.

---

## Stop price calculation (3 steps)

The stop is calculated the same way regardless of which strategy fired.

### Step 1 — Structural stop (swing low)

```
structural_stop = min(low, last 10 trading bars)
```

The stop is placed below the most recent swing low — the lowest intraday price of the past 10 bars. The logic: if price undercuts a level that buyers were previously defending, the trade thesis is invalidated.

### Step 2 — ATR floor

```
atr_stop = entry − (1.5 × ATR14)
stop = min(structural_stop, atr_stop)   ← take the lower (wider) of the two
```

If the structural swing low happens to be very close to the entry price, the stop is vulnerable to being triggered by normal daily price noise. The ATR (Average True Range over 14 bars) measures typical daily volatility. Ensuring the stop is at least 1.5×ATR below entry gives the trade enough breathing room to survive routine fluctuations.

Taking the *lower* of the two stops means: whichever level gives more distance from entry — the structural low or the ATR floor — wins. This produces the most defensible stop.

### Step 3 — Hard cap

```
if (entry − stop) / entry  >  8%:
    stop = entry × 0.92
    stop_capped = True
```

No stop may be more than 8% below the entry price. This prevents extreme stop distances on very volatile or thinly traded names from inflating the position risk beyond the 1.5% portfolio cap enforced by the Risk Layer. Signals where the hard cap was applied are flagged with `⚠ CAP` in the terminal output and `stop_capped = True` in the Signal object — they warrant manual review before acting.

---

## Target price

```
risk   = entry − stop
target = entry + (2.0 × risk)
```

The minimum acceptable target is a 2:1 reward-to-risk ratio at the technical level. Note that the Order Executor re-validates this ratio *after* adding transaction costs (0.35% Belgian stock tax per side) before placing any order — a signal that passes the 2:1 technical target may still be rejected if it doesn't clear 2:1 after costs.

---

## Conviction annotation

A signal is marked `elevated` (versus `standard`) when either of the following is true:

- **Both strategies fired on the same ticker** — a pullback and a breakout occurring simultaneously is rare and indicates an unusually strong setup.
- **Price is within 5% of its 52-week high** — stocks near new 52-week highs are in a leadership position in the market, and breakouts/pullbacks from those levels have historically higher follow-through rates.

Conviction is an annotation passed to the Risk Layer and notifications. It does not automatically change position sizing — that decision belongs to the Risk Layer.

---

## Liquidity classification

```
avg_daily_turnover = mean(close × volume,  last 20 trading days)

'thin'   if avg_daily_turnover < €1,000,000
'liquid' otherwise
```

Instruments with thin trading volumes receive different handling downstream: the Order Executor uses limit orders instead of market orders, and the stop order type changes to reduce slippage risk in low-volume names.

---

## Relative strength (RS) annotation

```
rs_value = stock close / ^STOXX50E close   (on matched dates)
```

The RS value is the stock's price divided by the benchmark index price. A rising RS line means the stock is outperforming the index. This is stored as an annotation on every signal — it is not a filter, but it is a useful secondary data point for manual review.

---

## Logging and persistence

**JSON-lines log** (`./logs/signal_engine_YYYY-MM-DD.jsonl`):  
Every fired signal and every skipped ticker is logged with a reason code. The log is the complete audit trail for the daily scan. Skip reasons use the format `component:reason_detail`, e.g. `strategy_a:histogram_not_negative` or `trend_filter:ema_chain_not_aligned`.

**SQLite database** (`./data/signals.db`, table: `signals`):  
Every fired Signal is persisted in full. The database accumulates across all scan runs, making it queryable for historical analysis (e.g. "how many breakout signals fired last month?", "which signals hit the stop cap?").

---

## File structure

```
signal_engine/
├── __main__.py      CLI entry point — wires config, logger, engine, and DB
├── engine.py        SignalEngine orchestrator + Signal dataclass
├── strategy_a.py    Strategy A: EMA Pullback conditions
├── strategy_b.py    Strategy B: 50-Day Breakout conditions
├── indicators.py    Pure indicator functions: EMA, MACD, ATR, RS line
├── db.py            SQLite schema + persistence functions
└── README.md        This file
```

---

## Configuration reference

All parameters live in `config.yaml` under the `signal_engine` section.

| Parameter | Default | Description |
|---|---|---|
| `ema_period` | `21` | EMA period for the Strategy A pullback anchor |
| `breakout_period` | `50` | Lookback bars for the Strategy B highest-high check |
| `near_52wk_high_pct` | `5` | Price must be within this % of its 52-week high for elevated conviction |
| `benchmark` | `^STOXX50E` | Yahoo Finance ticker for the market regime check and RS line |
| `macd_fast` | `12` | MACD fast EMA period |
| `macd_slow` | `26` | MACD slow EMA period |
| `macd_signal_period` | `9` | MACD signal line period |
| `atr_period` | `14` | ATR lookback (Wilder's smoothing) |
| `swing_low_period` | `10` | Bars to look back for the structural stop |
| `stop_atr_multiplier` | `1.5` | ATR floor multiplier for minimum stop distance |
| `stop_hard_cap_pct` | `8.0` | Maximum stop distance as % of entry price |
| `min_rr_ratio` | `2.0` | Minimum reward:risk for the technical target |
| `trend_drawdown_guard_pct` | `30.0` | Guard B: reject if price is more than this % below its recent high |
| `trend_drawdown_period` | `20` | Guard B: bars to look back for the recent high |
| `breakout_volume_multiplier` | `1.5` | Strategy B: breakout bar volume must be at least this × 20-day average |
| `liquidity_min_turnover` | `1000000` | Daily price×volume below €1M → classified as `thin` |
| `liquidity_avg_days` | `20` | Days to average for turnover and volume calculations |
| `db_path` | `./data/signals.db` | SQLite output file path |

---

## Stubbed features (pending Phase 2)

| Feature | Current state | Waiting on |
|---|---|---|
| `instrument_id` (IBKR conid) | Returns the ticker string as a placeholder | IBKR API integration |
| `earnings_flag` | Always `None` | IBKR `reqFundamentalData` |
| RS line as a filter | Currently an annotation only — not used to gate signals | Design decision |
