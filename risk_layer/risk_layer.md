# Risk Layer

The Risk Layer is the third component in the Trade Assistant pipeline. It sits between the Signal Engine and the Order Executor and acts as a mandatory gate: no order reaches the broker until every signal has been evaluated against a set of portfolio-level risk rules. Its job is not to judge the quality of a trade setup — that is the Signal Engine's job — but to answer one question: *can we afford this trade, given what we already have on?*

```
Data Fetcher  ──►  Signal Engine  ──►  Risk Layer  ──►  Order Executor  ──►  Position Manager
                   (signals fired)     (this module)    (order placement)
```

---

## What it produces

For every incoming Signal, the Risk Layer produces a **RiskDecision**:

| Field | Description |
|---|---|
| `approved` | `True` if all checks passed; `False` if any check failed |
| `shares` | Number of shares to buy (0 if rejected) |
| `risk_per_share` | Entry price minus stop price |
| `risk_amount` | Exact currency amount at risk: `shares × risk_per_share` |
| `position_risk_pct` | Risk amount as a percentage of portfolio value |
| `effective_cap_pct` | Cap that was actually applied (lower for thin instruments) |
| `current_open_risk_pct` | Total open risk before this trade |
| `projected_open_risk_pct` | Total open risk if this trade is taken |
| `portfolio_value` | Portfolio value used for all calculations |
| `reject_reason` | Machine-readable reason code if rejected; `None` if approved |

The Order Executor only proceeds with an order when `approved` is `True`.

---

## How to run

The Risk Layer does not run standalone under normal operation — it is called programmatically by the Order Executor as part of the order flow. However, the CLI is useful for inspecting state, manually correcting positions, and testing.

```bash
# Show open positions and current risk exposure
python -m risk_layer status

# Manually close a position (e.g. after a stop was hit outside the system)
python -m risk_layer close ASML.AS 720.50 stop

# Clear the daily loss limit pause flag (manual override)
python -m risk_layer unpause

# Use a custom config file
python -m risk_layer --config /path/to/config.yaml status
```

The `close` command accepts four reasons: `stop`, `target`, `trail`, `manual`.

---

## The six-step check sequence

When `evaluate(signal)` is called, the Risk Layer runs six checks in order. The checks are ordered from cheapest (single flag read) to most expensive (database aggregation), and from most absolute (global pause) to most contextual (aggregate risk budget).

A signal fails at the first check that does not pass and the remaining checks are skipped.

---

### Check 1 — Trading paused

```
is_trading_paused()  →  True?  →  REJECT
```

If the daily loss limit was triggered earlier today, all new orders are blocked for the rest of the session. This check reads a single value from the database and adds no meaningful latency.

The pause flag is date-scoped: it stores the date on which the limit was triggered. If today is a new calendar day, the flag is automatically stale and the check passes — no manual reset is needed at the start of each session.

---

### Check 2 — Duplicate instrument (RL-07)

```
has_open_position(ticker)  →  True?  →  REJECT
```

Only one open position per ticker is allowed. If ASML.AS is already held, a new pullback or breakout signal on ASML.AS is rejected regardless of its quality.

**Why this matters:** entering a second position on the same name is pyramiding — adding to an existing winner. While pyramiding can be a valid strategy, it requires a fundamentally different sizing model (the risk on the existing position changes the calculation). For v1, pyramiding is out of scope. One ticker, one position.

---

### Check 3 — Daily loss limit (RL-06)

```
daily_realised_pnl / portfolio_value  <  −3.0%  →  PAUSE + REJECT
```

If the sum of all realised P&L from positions closed today exceeds the daily loss limit (3% of portfolio by default), trading is paused for the remainder of the session and the current signal is rejected.

**Why pause rather than just reject?** A loss of 3% in a single session suggests something unusual is happening — adverse market conditions, a series of quick stop-outs, or a data quality issue. Halting new orders prevents a cascading series of losses from a system that may be operating in an environment it was not designed for.

**Phase 1 limitation:** only *realised* losses are counted. Unrealised intraday losses on open positions are not yet captured. This means the limit can only trigger after a position has been closed at a loss, not preemptively. Phase 2 will add live pricing from IBKR to cover unrealised drawdown.

---

### Check 4 — Position sizing (RL-03, RL-09)

```
risk_per_share = entry_price − stop_price
max_risk_amount = portfolio_value × effective_cap_pct / 100
shares = floor(max_risk_amount / risk_per_share)
```

The number of shares is sized so that the worst-case loss — if the stop is hit and filled exactly at the stop price — does not exceed the per-trade risk cap.

**Why `floor()` and not `round()`?**  
Rounding up would give one extra share whose risk pushes the actual position over the cap. `floor()` is the only rounding direction that guarantees the hard cap is never breached — even by a fraction.

**Thin instrument adjustment (RL-09):**  
For instruments classified as `thin` (average daily turnover below €1M), the position is sized to half the normal cap (configurable via `thin_size_multiplier`). The reason: thin names carry spread risk and potential market impact that the stop price alone does not capture. A €720 stock with a €30 stop might look like a clean 4.2% risk distance, but in a thin name that stop could easily gap through by €5–10, turning a 4.2% stop into a 6% actual loss. Halving the size keeps the expected dollar loss in a realistic range.

If `shares` comes back as 0 — which happens when the risk per share is larger than the entire risk budget — the signal is rejected. This can occur on volatile names with very wide stops; the Signal Engine's `stop_hard_cap_pct` parameter is designed to prevent this, but the check here acts as a final backstop.

---

### Check 5 — Per-trade risk hard cap (RL-01)

```
position_risk_pct  >  1.5%  →  REJECT
```

No single trade may risk more than 1.5% of portfolio value. This rule is **never overridden** — there is no configuration option to raise it, no override flag, no exception path.

In practice, the `floor()` in Check 4 means this check only fires if the sizing math produced an unexpected result (e.g. a future change to the sizing function introduced a rounding error). It acts as an explicit safety net rather than a primary gate. The 0.001% tolerance in the comparison accounts for normal floating-point precision in the percentage calculation.

---

### Check 6 — Total open risk hard cap (RL-02)

```
current_open_risk + new_position_risk  >  6.0%  →  REJECT
```

The aggregate risk of all open positions — including the new one — must not exceed 6% of portfolio value. This rule is also **never overridden**.

When this cap is hit, the session is considered "fully invested" from a risk perspective: four positions each risking 1.5% fills the budget exactly. Any further signals are held back until an existing position closes and the budget opens up again.

**Why 6%?** Four positions at 1.5% each equals 6%. This is a deliberate design choice: the system is never more than four trades deep at once. With four positions in uncorrelated names, a simultaneous stop-out on all four loses 6% — the portfolio survives and can continue operating.

---

## Position state tracking (RL-05)

Approved trades are not automatically recorded in the state store — only *filled* trades are. The Order Executor calls `open_position(decision)` after a fill is confirmed by the broker. This distinction matters: an approved signal may still fail to fill (the session closes, the order is rejected, the price gaps away), and recording it prematurely would:

1. Inflate the open risk count, blocking subsequent valid signals
2. Create a phantom position that the duplicate check would block

The state is stored in SQLite (`./data/risk.db`, table: `risk_positions`). Each row records the full position details including the risk amount used for cap calculations, the portfolio value at the time of the trade, and the fill price and P&L when the position closes.

---

## Logging

Every call to `evaluate()` produces a structured JSON-lines log entry regardless of outcome:

- `risk_check_passed` — all six checks passed; includes shares, risk amounts, and both open risk figures
- `risk_check_failed` — a check failed; includes the reject reason code and the check context
- `daily_loss_limit_breached` — emitted as a WARNING when Check 3 triggers a pause
- `position_opened` — recorded when `open_position()` is called after a fill
- `position_closed` — recorded when `close_position()` is called

Log files go to `./logs/risk_layer_YYYY-MM-DD.jsonl`. The same events are also printed to the console.

---

## File structure

```
risk_layer/
├── __main__.py      CLI: status / close / unpause commands
├── layer.py         RiskLayer class + RiskDecision dataclass
├── calculator.py    Pure sizing math: size_position(), open_risk_pct()
├── state.py         SQLite persistence: positions, daily P&L, pause flag
└── __init__.py      Exports RiskLayer and RiskDecision
```

---

## Configuration reference

All parameters live in `config.yaml` under the `risk:` section.

| Parameter | Default | Description |
|---|---|---|
| `max_position_risk_pct` | `1.5` | Hard cap: maximum % of portfolio value risked on a single trade (RL-01). Never overridden. |
| `max_open_risk_pct` | `6.0` | Hard cap: maximum total open risk across all positions (RL-02). Never overridden. |
| `daily_loss_limit_pct` | `3.0` | Pause all trading if realised losses today exceed this % of portfolio (RL-06) |
| `portfolio_value_stub` | `100000.0` | Portfolio value used for sizing until IBKR API is wired (Phase 1 only) |
| `thin_size_multiplier` | `0.5` | Thin instruments are sized to this fraction of the normal cap (RL-09) |
| `db_path` | `./data/risk.db` | SQLite file path for open positions and system state |

---

## Stubbed features (pending Phase 2)

| Feature | Current state | Waiting on |
|---|---|---|
| Live portfolio value (RL-04) | Returns `risk.portfolio_value_stub` from config | IBKR `reqAccountSummary` |
| Unrealised P&L in daily loss limit (RL-06) | Realised only — positions closed today | IBKR live pricing |
| Sector / correlation exposure monitoring (RL-08) | Not implemented | Design decision + data source |
