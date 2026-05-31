# Sim Executor (SX) — Build Briefing

The Sim Executor is a thin stand-in for the real Order Executor. It slots into the
pipeline between the Risk Layer and the (not yet built) Position Manager, and allows
the full system to be exercised end-to-end without any broker connectivity.

```
Signal Engine  ──►  Risk Layer  ──►  Sim Executor  ──►  Position Manager
(signals.db)        (evaluate)       (this module)       (risk.db positions)
```

---

## What it does

1. Reads unprocessed signals from the Signal Engine's SQLite database (`signals.db`)
2. Passes each signal to the real `RiskLayer.evaluate()` — no mocking, no shortcuts
3. If approved: records the position in the Risk Layer's database (`risk.db`) at the
   signal's `entry_price`, marked as a simulated fill
4. If rejected: logs the rejection reason and moves on
5. Sends a Telegram notification in the same format the real Order Executor will use

That's the entire scope. No Gateway, no ib_insync, no tick size queries, no market
hours checks. All of those belong to the real Order Executor and are explicitly out
of scope here.

---

## What it does NOT do

- Model fill slippage (entry is always at `signal.entry_price`)
- Validate R:R against a live price (the signal's R:R is trusted as-is)
- Check market hours or auction status
- Place or manage stop orders (that is the Position Manager's job)
- Interact with IBKR in any way

These omissions are intentional. The sim executor's value is in exercising the Risk
Layer logic and producing a realistic position state — not in modelling execution
quality.

---

## Interface contract

### Input — Signal fields consumed

The Signal object comes from the Signal Engine's SQLite database. SX reads these fields:

| Field | Used for |
|---|---|
| `ticker` | Logging, notifications, duplicate check key |
| `entry_price` | Simulated fill price |
| `stop_price` | Passed to Risk Layer for risk_per_share calculation |
| `target_price` | Stored on position record |
| `signal_type` | Stored on position record; used by Position Manager for time limit lookup |
| `liquidity_class` | Passed to Risk Layer (affects thin_size_multiplier) |
| `conviction` | Stored on position record; logged and notified |
| `signal_timestamp` | Stored as position open timestamp |
| `run_type` | Stored on position record (`eod` only for now) |

Fields `instrument_id`, `isin`, `earnings_flag` are stored on the position record
as-is. In v1, `instrument_id` is the ticker string (placeholder) and `isin` and
`earnings_flag` are `None` — SX does not need to resolve these.

### Output — Position fields written to risk.db

SX calls `risk_layer.open_position(decision)` after a simulated fill. The Risk Layer
writes the position record. SX is responsible for supplying these additional fields
that the Risk Layer does not derive itself:

| Field | Value in SX |
|---|---|
| `fill_price` | `signal.entry_price` |
| `fill_timestamp` | Current UTC timestamp at time of processing |
| `entry_commission` | `fill_price × shares × 0.0035` (TOB flat estimate — no IBKR fee schedule) |
| `bot_initiated` | `True` |
| `exit_price`, `exit_timestamp`, `exit_commission` | `None` (position is open) |
| `exit_reason`, `exit_note` | `None` |
| `peak_price` | `fill_price` (initialised to entry; Position Manager updates this) |
| `trail_triggered` | `False` |
| `trail_trigger_price` | `None` |
| `gross_pnl`, `net_pnl` | `None` (position is open) |

---

## Processing mode

SX runs in two modes, selectable via CLI flag:

**Batch mode (default)** — processes all signals in `signals.db` with
`processed = False`, in `signal_timestamp` order. Intended for the daily EOD run:
after the Signal Engine has completed its scan, SX is invoked once and works through
the day's output.

**Watch mode (`--watch`)** — polls `signals.db` every N seconds for new unprocessed
signals and processes them as they arrive. Intended for future intraday use, but
building it now costs nothing and avoids a rewrite later.

In both modes, each signal is marked `processed = True` in `signals.db` after
handling — whether approved or rejected — so re-runs are idempotent.

---

## Rejection handling

When the Risk Layer rejects a signal, SX:

- Marks the signal `processed = True` in `signals.db`
- Logs the rejection with the `reject_reason` code from the `RiskDecision`
- Does **not** send a Telegram notification for rejections by default (too noisy
  during normal operation). Rejection logging is to file only. This matches the
  behaviour planned for the real Order Executor.

Exception: if the rejection reason is `daily_loss_limit` or `trading_paused`, SX
does send a Telegram notification — these indicate system-level conditions the
operator should know about, not routine risk budget management.

---

## Notifications

SX sends one Telegram message per approved fill, in this format:

```
🟢 SIM FILL · ASML.AS
Strategy: breakout · Elevated conviction
Entry: €1,424.60 · Stop: €1,310.63 · Target: €1,652.54
Shares: 5 · Risk: €569.85 (1.1%)
Open risk: 3.6% → 4.7%
```

The `SIM` prefix is present on every message so simulated fills are unambiguously
distinguishable from live fills in the notification history. The real Order Executor
will use the same format without the prefix.

---

## File structure

```
sim_executor/
├── __main__.py     CLI entry point — batch and watch modes
├── executor.py     SimExecutor class — main processing loop
├── fills.py        Simulated fill writer — builds position record, calls risk_layer.open_position()
├── notify.py       Telegram notification formatting (reused by real Order Executor later)
└── __init__.py     Exports SimExecutor
```

`notify.py` is written as a standalone module from the start so the real Order
Executor can import it directly without modification.

---

## Configuration

SX reads from the existing `config.yaml`. No new top-level section is needed. It
uses:

```yaml
risk:
  portfolio_value_stub: 100000.0   # used by Risk Layer for sizing
  db_path: './data/risk.db'

signal_engine:
  db_path: './data/signals.db'     # SX reads from here

costs:
  tob_pct: 0.35                    # used for entry_commission estimate

notifications:
  telegram_bot_token: ''
  telegram_chat_id: ''

sim_executor:
  watch_poll_seconds: 10           # only used in --watch mode
```

---

## Running

```bash
# Process all pending signals once (normal daily use)
python -m sim_executor

# Watch for new signals continuously
python -m sim_executor --watch

# Dry run — evaluate and log without writing positions or sending notifications
python -m sim_executor --dry-run

# Custom config
python -m sim_executor --config /path/to/config.yaml
```

---

## What this enables downstream

Once SX is running, the following become buildable immediately:

- **Position Manager** — open positions now exist in `risk.db` for it to monitor
- **Notification Layer** — `notify.py` written here is the foundation; expand for
  other event types (stop hit, trail update, etc.)
- **Walk-forward simulation** — SX's fill logic is reused directly by the simulation
  runner; the only difference is the date cursor and the truncated data view

The real Order Executor is a replacement for SX, not an extension of it. When the
time comes, SX stays in the codebase under `mode: sim` in the config and the real
executor activates under `mode: live`. No other component changes.
