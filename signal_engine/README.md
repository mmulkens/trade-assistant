# Signal Engine

Scans the EU equity watchlist daily and emits structured trade signals when a setup meets all required conditions. Two independent strategies run in parallel; a signal fires when **either** passes (OR logic). Both firing simultaneously, or price near a 52-week high, triggers an **elevated conviction** annotation.

Sits between the Data Fetcher (Parquet cache) and the Risk Layer in the pipeline:

```
Data Fetcher → Signal Engine → Risk Layer → Order Executor → Position Manager
```

---

## Files

| File | Purpose |
|---|---|
| `indicators.py` | Pure indicator functions: EMA, MACD, ATR, RS line. No side effects. |
| `engine.py` | `SignalEngine` class + `Signal` dataclass. Market regime check, strategy A & B logic, stop/target calculation. |
| `db.py` | SQLite persistence — initialise schema, insert signals into `signals.db`. |
| `__main__.py` | CLI entry point (`python -m signal_engine`). Wires config, logger, engine, and DB together. |

---

## How to run

```bash
# Scan the full watchlist (from config.yaml)
python -m signal_engine

# Scan specific tickers only
python -m signal_engine ASML.AS SIE.DE ALV.DE

# Custom config path
python -m signal_engine --config /path/to/config.yaml
```

Output is printed to stdout and also persisted to `./data/signals.db`.  
JSON-lines logs are written to `./logs/signal_engine_YYYY-MM-DD.jsonl`.

---

## Market Regime Gate

Before scanning any ticker, the engine loads `^STOXX50E` from the Parquet cache and computes its 200-period EMA.

- **Bull** (`close >= EMA200`): scan proceeds normally.
- **Bear** (`close < EMA200`): the entire scan is skipped and an empty signal list is returned.

Individual breakouts and pullbacks in a bear-market environment have significantly lower follow-through rates. This is a hard gate, not a soft filter.

---

## Strategy A — EMA Pullback

Fires when **all** of the following are true:

| # | Condition | Why |
|---|---|---|
| 1 | `close > EMA50` | Price is above the medium-term trend line |
| 2 | `EMA21 > EMA50` | Fast EMA above slow EMA — bullish momentum ordering |
| 3 | `EMA50 > EMA100 > EMA200` | All major averages in bullish stack |
| 4a | `abs(close − EMA21) / EMA21 ≤ 2%` | Price is approaching the 21 EMA from above (OR) |
| 4b | `low ≤ EMA21 < close` | Intraday wick tagged the EMA; close recovered above it |
| 5 | MACD histogram `< 0` and rising for 2 consecutive bars | Momentum recovering from pullback, but not yet extended |

**21 EMA rationale:** Fibonacci number; widely watched by institutional swing traders (Minervini / IBD methodology). Enough market participants observe this level to generate real support on pullbacks. EMA20 has less institutional backing; EMA10 is too noisy for swing timeframes.

**MACD histogram condition detail:** The requirement is `h[-1] > h[-2] > h[-3]` *and* `h[-1] < 0`. This captures the "dark red to light red" transition — momentum is recovering from weakness but has not yet turned positive. Avoids signals on already-extended bounces.

---

## Strategy B — 50-Day Breakout

Fires when **all** of the following are true:

| # | Condition | Why |
|---|---|---|
| 1 | `close > max(high[-51:-1])` | Price closes above the highest high of the prior 50 trading days |
| 2 | `MACD line > 0` | Overall trend momentum is positive |
| 3 | `MACD line[-1] > MACD line[-2]` | Momentum is accelerating on the breakout |

**50-day lookback rationale:** 20 days fires too frequently with lower conviction. 52 weeks fires too rarely as a daily scanner primary. 50 days balances signal frequency against conviction quality.

**Look-ahead guard:** The breakout window is `df["high"].iloc[-(N+1):-1]` — today's bar is excluded from the comparison so we test "did today's close exceed the *prior* N-day high?"

---

## Stop, Target & Conviction

### Stop Price (3-step calculation)

```
1. structural_stop = min(low[-swing_low_period:])   # lowest low of last N bars
2. atr_stop        = entry − (stop_atr_multiplier × ATR14)
3. stop            = min(structural_stop, atr_stop)  # take the wider stop
4. if (entry − stop) / entry > stop_hard_cap_pct:
       stop = entry × (1 − stop_hard_cap_pct / 100)  # hard cap
```

**Why take the wider stop?** If the structural swing low is closer to entry than 1.5×ATR, the stop is vulnerable to being triggered by normal daily noise. Taking the lower of the two ensures the trade always has at least 1.5 ATR of breathing room.

**Hard cap (default 8%):** Prevents extreme stops on very volatile or thinly traded instruments from inflating position risk beyond the 1.5% portfolio cap enforced by the Risk Layer.

All stop parameters are configurable in `config.yaml` under `signal_engine`.

### Target Price

```
risk   = entry − stop
target = entry + (min_rr_ratio × risk)   # default: 2.0 → 2:1 R:R
```

The Order Executor re-validates R:R *after* applying transaction costs (TOB tax at 0.35% per side) before touching the market.

### Conviction

| Condition | Conviction |
|---|---|
| Only one strategy fired | `standard` |
| Both strategies fired simultaneously | `elevated` |
| Price within `near_52wk_high_pct`% (default 5%) of 52-week high | `elevated` |
| Both strategies fired **and** near 52-week high | `elevated` (not "super-elevated" — kept binary) |

---

## Liquidity Classification

```
avg_turnover = mean(close × volume, last 20 days)
classification = 'thin' if avg_turnover < €1,000,000 else 'liquid'
```

`thin` instruments receive different treatment downstream: the Order Executor uses limit orders instead of market/stop-market orders, and the stop order type changes to avoid excessive slippage in low-volume names.

---

## Signal Output Contract

Every emitted `Signal` has the following fields (see `engine.py → Signal`):

| Field | Type | Description |
|---|---|---|
| `instrument_id` | `str` | IBKR conid — stubbed as ticker until IBKR integration |
| `ticker` | `str` | Yahoo Finance ticker (e.g. `ASML.AS`) |
| `direction` | `str` | Always `'long'` in v1 |
| `entry_price` | `float` | Last close at signal time |
| `stop_price` | `float` | Technical stop, fixed at signal time |
| `target_price` | `float` | Minimum 2:1 R:R target |
| `signal_type` | `str` | `pullback` \| `breakout` \| `pullback+breakout` |
| `liquidity_class` | `str` | `liquid` \| `thin` |
| `conviction` | `str` | `standard` \| `elevated` |
| `signal_timestamp` | `datetime` | UTC timestamp |
| `earnings_flag` | `bool\|None` | Always `None` until IBKR `reqFundamentalData` is wired |
| `strategy_a_fired` | `bool` | — |
| `strategy_b_fired` | `bool` | — |
| `near_52wk_high` | `bool` | — |
| `market_regime` | `str` | `bull` \| `bear` \| `unknown` |
| `rs_value` | `float\|None` | Stock / benchmark ratio (annotation only, not a filter) |

---

## Configuration (`config.yaml` → `signal_engine` section)

| Key | Default | Description |
|---|---|---|
| `ema_period` | `21` | EMA period for Strategy A pullback anchor |
| `breakout_period` | `50` | Lookback for Strategy B highest high |
| `near_52wk_high_pct` | `5` | % below 52-wk high for elevated conviction |
| `benchmark` | `^STOXX50E` | Index for regime filter and RS line |
| `macd_fast` | `12` | MACD fast EMA period |
| `macd_slow` | `26` | MACD slow EMA period |
| `macd_signal_period` | `9` | MACD signal line period |
| `atr_period` | `14` | ATR lookback period (Wilder's) |
| `swing_low_period` | `10` | Bars to look back for structural stop |
| `stop_atr_multiplier` | `1.5` | ATR floor multiplier for stop distance |
| `stop_hard_cap_pct` | `8.0` | Maximum stop distance as % of entry |
| `min_rr_ratio` | `2.0` | Minimum reward:risk ratio for target |
| `pullback_tolerance_pct` | `2.0` | Close within N% of EMA21 counts as pullback |
| `liquidity_min_turnover` | `1000000` | €/day threshold for thin classification |
| `liquidity_avg_days` | `20` | Days to average for turnover calculation |
| `db_path` | `./data/signals.db` | SQLite output file |

---

## Stubbed Features (Phase 1)

These features are architecturally present but not yet implemented:

| Feature | Status | Blocking on |
|---|---|---|
| `instrument_id` (IBKR conid) | Stubbed as ticker string | IBKR integration |
| `earnings_flag` | Always `None` | IBKR `reqFundamentalData` |
| RS line as a filter | Currently annotation-only | Design decision (may stay as annotation) |

---

## Design Decisions & Rejected Alternatives

See `trade_assistant_design.md` — Section 3 (Signal Engine) for full rationale.

Key rejections:
- **AND(A, B)** combined signal: A pullback and a breakout are nearly mutually exclusive at the same moment — AND logic would almost never fire.
- **MACD as standalone Strategy C**: Derived entirely from price, adds no independent informational value, lags in trending markets.
- **VCP (Minervini)**: Volatility contraction detection is inherently subjective — programmatic definition consistently fails validation.
- **Higher highs / lower lows for uptrend**: More precise but significantly harder to implement robustly than the EMA stack.
