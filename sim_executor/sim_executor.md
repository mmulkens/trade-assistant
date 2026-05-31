# Sim Executor (SX)

The Sim Executor is the fourth component in the Trade Assistant pipeline. It slots between the Risk Layer and the Position Manager and acts as a stand-in for the real Order Executor: rather than placing orders through a broker, it records simulated fills directly in the risk database at the signal's entry price. This allows the full system to be exercised end-to-end — including real Risk Layer evaluations, real position state tracking, and real Telegram notifications — without any broker connectivity.

```
Signal Engine  ──►  Risk Layer  ──►  Sim Executor  ──►  Position Manager
(signals.db)        (evaluate)       (this module)       (risk.db positions)
```

The Sim Executor is not a throwaway prototype. When the real Order Executor is built, it replaces SX in the pipeline under a `mode: live` configuration flag. SX stays in the codebase under `mode: sim` so simulated runs remain available for walk-forward backtesting and strategy validation without touching live capital.

---

## What it does

1. Reads unprocessed signals from the Signal Engine's SQLite database (`signals.db`)
2. Passes each signal to the real `RiskLayer.evaluate()` — no mocking, no shortcuts
3. If approved: records the position in `risk.db` at the signal's `entry_price`, marked as a bot-initiated simulated fill
4. If rejected: logs the rejection reason; optionally sends a Telegram alert for system-level conditions
5. Marks each signal `processed = 1` in `signals.db` so re-runs are idempotent

---

## What it does NOT do

- **Model fill slippage** — entry is always at `signal.entry_price`. The sim executor's value is in exercising Risk Layer logic, not in modelling execution quality.
- **Validate R:R after costs** — the signal's R:R is trusted as-is. Cost revalidation belongs to the real Order Executor.
- **Check market hours or auction status** — those belong to the real executor.
- **Place or manage stop orders** — that is the Position Manager's job. SX only opens positions.
- **Interact with IBKR in any way** — no `ib_insync`, no tick sizes, no live prices.

These omissions are intentional. They keep SX simple and focused.

---

## How to run

```bash
# Process all pending signals once (normal daily use — run after the Signal Engine)
python -m sim_executor

# Watch for new signals continuously (future intraday use)
python -m sim_executor --watch

# Evaluate and log without writing positions or sending notifications
python -m sim_executor --dry-run

# Use a custom config file
python -m sim_executor --config /path/to/config.yaml
```

**Normal daily workflow:**
```bash
python -m signal_engine          # 1. Scan watchlist, write signals to signals.db
python -m sim_executor           # 2. Evaluate signals, write positions to risk.db
python -m risk_layer status      # 3. Inspect open positions and risk budget
```

The two steps are separate processes. SX reads from `signals.db` independently of the Signal Engine, which mirrors the production architecture where the real Order Executor also reads from `signals.db`.

---

## Processing logic

### Signal acquisition

SX queries `signals.db` for all rows where `processed IS NOT 1`, ordered by `signal_timestamp`. Both `NULL` and `0` are treated as unprocessed — `NULL` appears in rows written before the `processed` column was added, and is handled transparently via forward schema migration on startup.

### Per-signal pipeline

Each row is processed in five steps:

**Step 1 — Reconstruct Signal**  
The `signals.db` row is converted back into a `Signal` dataclass. SQLite booleans (`INTEGER 0/1`) are converted to Python `bool`. Naive timestamps get UTC attached. If any field is missing or malformed, the row is logged as a parse error, marked processed, and skipped — a corrupt row should not block every signal after it in the batch.

**Step 2 — Risk Layer evaluation**  
The real `RiskLayer.evaluate(signal)` is called. This runs all six pre-trade risk checks (trading paused, duplicate, daily loss limit, position sizing, per-trade cap, total open risk cap) against the live state of `risk.db`. See the Risk Layer documentation for a full description of each check.

**Step 3 — Fill recording (approved)**  
If the signal is approved, `fills.py` constructs the position record and calls `risk_layer.open_position()` to write it to `risk.db`. The fill price is always `signal.entry_price`. A TOB commission estimate is calculated and stored on the position record for later P&L accounting.

**Step 4 — Rejection handling**  
If the signal is rejected, the reason is logged. Most rejection codes (`duplicate_instrument`, `open_risk_cap_exceeded`, `position_risk_cap_exceeded`, `zero_shares`) are routine and require no notification. Two codes indicate system-level conditions that warrant operator attention (see *Rejection notification policy* below).

**Step 5 — Mark processed**  
After all side effects complete, the signal row is updated to `processed = 1`. This step is deliberately last: if the process crashes mid-step, the signal remains unprocessed and eligible for a safe retry on the next run. In `--dry-run` mode this step is skipped so the same signals can be re-evaluated in a subsequent real run.

---

## Fill record

When a signal is approved, SX writes a position record to `risk.db` (`risk_positions` table). Beyond the fields the Risk Layer derives from the decision, SX supplies these additional fields:

| Field | Value |
|---|---|
| `fill_price` | `signal.entry_price` (no slippage) |
| `fill_timestamp` | UTC timestamp at time of processing |
| `entry_commission` | `fill_price × shares × tob_pct / 100` (TOB flat estimate) |
| `bot_initiated` | `True` |
| `peak_price` | `fill_price` — initialised to entry; Position Manager updates as the position moves in our favour |
| `trail_triggered` | `False` — Position Manager sets this once the trail activates |
| `target_price` | From signal — stored for Position Manager reference |
| `run_type` | `'eod'` (all current signals are EOD) |

Fields owned by the Position Manager (`exit_price`, `exit_timestamp`, `exit_commission`, `gross_pnl`, `net_pnl`, `trail_trigger_price`) are `NULL` until that component is built.

---

## Rejection notification policy

Most rejections are routine risk budget decisions and are logged to file only — sending a notification for every duplicate or cap breach would create noise that masks genuinely important alerts.

Two rejection codes indicate system-level conditions that require operator attention before the next session:

| Code | Meaning |
|---|---|
| `daily_loss_limit_breached` | Realised losses today crossed the configured limit. Trading is now paused for the rest of the session. |
| `trading_paused:daily_loss_limit_reached` | The pause flag from a prior breach is still active (SX is being run a second time today after the limit was already triggered). |

For these two codes, SX sends a Telegram alert so the operator can review the account before tomorrow's run.

---

## Telegram notifications

SX sends one Telegram message per approved fill:

```
🟢 SIM FILL · ASML.AS
Strategy: breakout · Elevated conviction
Entry: €1,424.60 · Stop: €1,310.63 · Target: €1,652.54
Shares: 5 · Risk: €569.85 (1.1%)
Open risk: 3.6% → 4.7%
```

The `SIM` prefix appears on every message so simulated fills are unambiguously distinguishable from live fills in the notification history — particularly important during any future period when both modes run in parallel for validation.

The real Order Executor will use the same `notify.py` module with `is_sim=False` to produce identical messages without the prefix.

**Token absent = notifications off.** If `notifications.telegram_bot_token` is empty in `config.yaml`, notifications are silently disabled. Any other failure (network error, invalid token, HTTP error from Telegram) is logged at `WARNING` level and the process continues — a missed notification never aborts a run or undoes a fill that has already been written to the database.

---

## Operating modes

### Batch mode (default)

Processes all `processed IS NOT 1` signals in `signal_timestamp` order, logs a summary, and exits. This is the normal daily use case, called once after the Signal Engine scan completes.

### Watch mode (`--watch`)

Polls `signals.db` every `sim_executor.watch_poll_seconds` seconds and processes any new unprocessed signals. Does not exit. Intended for future intraday use; in EOD-only operation, batch mode is sufficient.

### Dry-run mode (`--dry-run`)

Evaluates all signals through the Risk Layer and logs the outcome of every check, but makes no writes:
- No positions written to `risk.db`
- No Telegram notifications sent
- No signals marked processed in `signals.db`

Because signals are not marked processed, a dry run can be immediately followed by a real run that processes the same signals normally. Useful for verifying that a day's signals will pass risk checks before committing.

---

## File structure

```
sim_executor/
├── __main__.py     CLI entry point — batch and watch modes
├── executor.py     SimExecutor class — main processing loop + Signal reconstruction
├── fills.py        Fill writer — builds position record, calls risk_layer.open_position()
├── notify.py       Telegram notifications — reusable by the real Order Executor
└── __init__.py     Exports SimExecutor
```

---

## Configuration reference

SX uses existing sections of `config.yaml`. The only new section is `sim_executor`:

| Parameter | Location | Default | Description |
|---|---|---|---|
| `watch_poll_seconds` | `sim_executor` | `10` | Poll interval for `--watch` mode (seconds) |
| `db_path` | `signal_engine` | `./data/signals.db` | SQLite file SX reads signals from |
| `db_path` | `risk` | `./data/risk.db` | SQLite file SX writes positions to |
| `tob_pct` | `costs` | `0.35` | Belgian TOB rate used for entry commission estimate |
| `telegram_bot_token` | `notifications` | `''` | Telegram Bot API token; empty = notifications disabled |
| `telegram_chat_id` | `notifications` | `''` | Target chat ID for notifications |
| `portfolio_value_stub` | `risk` | `100000.0` | Portfolio value used by the Risk Layer for sizing (Phase 1) |

---

## What this enables downstream

With SX running:

- **Position Manager** — open positions now exist in `risk.db` for it to monitor, trail, and close
- **Walk-forward simulation** — SX's fill logic (`fills.py`) is reused directly by the simulation runner; the only difference is a date cursor and a truncated data view
- **Notification Layer** — `notify.py` written here is the foundation; the same module is extended for other event types (stop hit, trail activated, target reached) without modification to the fill notification format

---

## Schema changes introduced by SX

SX extends two existing database tables via forward migration (new columns are added with `ALTER TABLE ADD COLUMN`; no existing data is touched):

**`signals` table (`signals.db`):**
- `processed INTEGER` — `1` once the signal has been handled; `NULL`/`0` = unprocessed
- `run_type TEXT` — `'eod'` or `'intraday'`; all existing rows default to `'eod'`

**`risk_positions` table (`risk.db`):**
- `target_price`, `isin`, `run_type` — signal metadata stored on the position for PM reference
- `fill_price`, `fill_timestamp`, `entry_commission`, `bot_initiated` — fill details set at open
- `exit_commission`, `exit_note` — exit details; set by Position Manager at close
- `peak_price`, `trail_triggered`, `trail_trigger_price` — trailing stop tracking; managed by Position Manager
- `gross_pnl`, `net_pnl` — calculated at close; `NULL` while the position is open
