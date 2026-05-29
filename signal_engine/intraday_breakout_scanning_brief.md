# Intraday Breakout Scanning — Implementation Brief

*Design session: 2026-05-28 · For: coder agent handoff*

---

## Problem being solved

The signal engine currently runs once per day on EOD (end-of-day) data. For **Strategy A (EMA Pullback)**, the entry is placed as a passive resting limit at or near the prior day's close — if price pulls back to that level during the next session, you fill at the intended price with R:R intact. This works well.

For **Strategy B (Breakout)**, it does not work well. A breakout stock frequently gaps up at the open and never returns to yesterday's close. A resting limit misses the trade. The R:R gate on any gap-up entry at the open is often too compressed to pass. Previously, these trades were simply missed.

The solution is a set of **scheduled intraday runs** that evaluate Strategy B conditions against live market data during the session, allowing entry while the breakout is developing and volume is confirming.

---

## What is being added

Three scheduled intraday scans at **13:30, 14:30, and 15:30** local exchange time (Euronext session: 09:00–17:30). These runs evaluate **Strategy B only** — Strategy A is an EOD concept and is not evaluated intraday.

Each intraday run is functionally identical. The only difference between them is the volume multiplier applied (see below), which accounts for how much of the trading session has elapsed.

---

## What each intraday run evaluates

### Sourced from the Parquet cache (EOD data — do not recalculate intraday)

- **EMA chain alignment** (`EMA21 > EMA50 > EMA100 > EMA200`) — daily calculation, meaningless on a partial bar
- **Market regime** (`^STOXX50E close ≥ EMA200`) — checked once at EOD, trusted for the full session
- **MACD line direction** — partial-day MACD is too noisy; trust yesterday's reading
- **50-day highest high** — this is the breakout trigger level; does not change intraday
- **20-day average volume** — baseline for volume confirmation

### Sourced from the IBKR real-time feed (live, at run time)

- **Current price**: is `live_price > 50-day highest high`? If not, skip — no breakout.
- **Intraday volume so far**: used for volume extrapolation (see below).

### R:R re-validation (always, from scratch)

The stop and target are calculated from the **EOD reference price** (prior day's close), not the live price. The live ask is used as the entry price for R:R calculation. This means a gapped-up entry will have a wider risk band and compressed R:R — which is correct. The R:R gate is always hard; it is never relaxed for intraday runs.

```
risk      = live_ask − stop_price          (stop anchored to EOD reference)
reward    = target_price − live_ask        (target anchored to EOD reference)
rr_ratio  = reward / risk
submit    = rr_ratio ≥ min_rr_after_costs  (2.0 after Belgian TOB + exchange fees)
```

---

## Volume extrapolation

```python
session_minutes_total = 510   # Euronext 09:00–17:30
elapsed_minutes       = (current_time - market_open).total_seconds() / 60
extrapolated_volume   = intraday_volume_so_far * (session_minutes_total / elapsed_minutes)
confirmed             = extrapolated_volume >= volume_multiplier * avg_20d_volume
```

Volume multipliers by run time (all configurable in `config.yaml`):

| Run time | Multiplier | Rationale |
|---|---|---|
| 13:30 | 1.8× | ~55% of session elapsed; higher bar compensates for extrapolation uncertainty |
| 14:30 | 1.6× | ~67% of session elapsed |
| 15:30 | 1.5× | ~78% of session elapsed; standard IBD/O'Neil threshold |

---

## Deduplication — three layers, checked in order

Before evaluating any ticker in an intraday run, apply these three checks. Skip if any fires.

**Layer 1 — Open position check** (existing Risk Layer logic, unchanged):
```python
has_open_position(ticker)  →  skip
```

**Layer 2 — Pending order check** (new, lightweight):
```python
has_pending_order(ticker)  →  skip
```
A fill may not be confirmed by the time the next scheduled run fires. Do not submit a second order on the same ticker if one is already live.

**Layer 3 — Session rejection store** (new, in-memory only):
```python
ticker in intraday_rejections  →  skip
```
See next section.

---

## The session rejection store

This is the most important piece of new logic.

**Purpose:** prevent re-evaluating a ticker that was already rejected for compressed R:R earlier in the session. A stock that spiked (compressing R:R at 13:30) and then partially faded (appearing viable at 15:30) is an intraday "gap and crap" pattern — not a clean setup. Once the R:R gate has rejected a ticker in an intraday run, it should not be reconsidered that session.

**Implementation:**
- Plain Python dict, in-memory, keyed by ticker
- Populated when: a ticker passes all other Strategy B conditions (price, EMA chain, volume) but fails the R:R gate
- Resets: at the start of each trading day (process restart, or explicit daily reset in the scheduler)
- Persisted: nowhere — intentionally ephemeral

```python
intraday_rejections: dict[str, dict] = {}

# On R:R failure during an intraday run:
intraday_rejections[ticker] = {
    "rejected_at": "13:30",
    "reason": "rr_compressed",
    "live_price_at_rejection": 37.80,
    "rr_at_rejection": 1.41
}
```

**Critical distinction — what does NOT go into the rejection store:**

If a ticker fails the **price condition** (live price ≤ 50-day highest high) at 13:30, it is simply skipped silently — do NOT add it to the rejection store. If the same stock breaks out cleanly at 14:30 or 15:30 with proper volume, that is a legitimate fresh signal and should be evaluated normally.

Only R:R failures are poisoned. Price condition failures are not.

---

## Signal payload additions

Add two new fields to the Signal dataclass:

| Field | Type | Values | Description |
|---|---|---|---|
| `run_type` | str | `'eod'` \| `'intraday'` | Which engine run produced this signal |
| `reference_price` | float | Prior day's close | EOD closing price used to calculate stop and target; may differ from entry_price on intraday runs |

The Order Executor does not need to change — it receives a Signal object and processes it identically regardless of `run_type`. The `run_type` field exists for logging and audit purposes only.

---

## EOD self-deduplication (no extra code needed)

If an intraday run on day N results in a fill, the EOD run on day N+1 will see that yesterday's close was already above the 50-day highest high. The existing **freshness check** in Strategy B will suppress the signal:

```python
# Existing freshness check (Strategy B):
yesterday.close > max(high, the 50 days ending two days ago)  →  already broke out, skip
```

This is free deduplication. No additional logic is needed.

---

## Scheduler changes

Add three new scheduled jobs alongside the existing EOD run:

```python
schedule.every().day.at("07:15").do(run_signal_engine, mode="eod")
schedule.every().day.at("13:30").do(run_signal_engine, mode="intraday")
schedule.every().day.at("14:30").do(run_signal_engine, mode="intraday")
schedule.every().day.at("15:30").do(run_signal_engine, mode="intraday")
```

Intraday jobs should check that the market is open before running (use the existing market hours check). If the market is closed or in an auction window, skip the run silently.

---

## Logging

Each intraday run should produce a log entry covering:

- Run time and mode (`intraday`)
- Tickers evaluated (count)
- Tickers skipped: open position, pending order, session rejection store, price condition, volume condition (with counts per reason)
- R:R rejections (tickers added to session rejection store this run)
- Signals fired (with full parameters)

Log format: JSON lines, same as existing signal engine logs. File: `./logs/signal_engine_YYYY-MM-DD.jsonl` (appended, not a separate file).

The session rejection store state should be loggable on demand (e.g. at end of day or on process shutdown) for audit purposes.

---

## Config additions (`config.yaml`)

```yaml
signal_engine:
  # ... existing parameters unchanged ...
  intraday_runs:
    - time: "13:30"
      volume_multiplier: 1.8
    - time: "14:30"
      volume_multiplier: 1.6
    - time: "15:30"
      volume_multiplier: 1.5
```

All three run times and their volume multipliers are configurable. Adding or removing runs requires only a config change, no code change.

---

## Files to modify

| File | Change |
|---|---|
| `signal_engine/engine.py` | Add `intraday_mode` flag; add `run_type` and `reference_price` to Signal dataclass |
| `signal_engine/strategy_b.py` | Accept live price and intraday volume as parameters when called in intraday mode |
| `signal_engine/__main__.py` | Wire up intraday mode; manage `intraday_rejections` store; expose session reset |
| `scheduler.py` (or equivalent) | Add three intraday jobs |
| `config.yaml` | Add `intraday_runs` list under `signal_engine` |

No changes required to: Risk Layer, Order Executor, Position Manager, Notification Layer.

---

## What this does NOT change

- Strategy A (EMA Pullback) is **EOD only**. Pullback signals from the morning EOD run are placed as resting day-limit orders near the prior close. If price pulls back to them during the session, they fill. If not, they expire. No intraday evaluation of Strategy A.
- The R:R hard gate is **never relaxed** for intraday runs, regardless of conviction level. The three-run schedule is specifically designed so that high-conviction breakouts can be caught at a viable price earlier in the day without needing to compromise on entry quality.
- The Risk Layer position sizing, hard caps, and duplicate checks are **unchanged and still apply** to intraday signals exactly as they do to EOD signals.

---

*End of brief*
