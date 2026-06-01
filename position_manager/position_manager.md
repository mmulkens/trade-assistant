# Position Manager

The Position Manager is the fifth component in the Trade Assistant pipeline. It takes over after a position has been opened and manages everything that happens between entry and exit: protecting accumulated profit with an ATR-based trailing stop, evaluating stalled positions for time-based exits, and recording any positions the user closes manually outside the bot.

```
Signal Engine  ──►  Risk Layer  ──►  Sim/Order Executor  ──►  Position Manager
(signals.db)        (evaluate)       (fills risk.db)           (this module)
```

The Position Manager runs once per trading day, after the DataFetcher has updated the Parquet cache with the previous session's closes. It does not need a broker connection in Phase 1 — all ATR calculations come from the Parquet cache, and stop levels are tracked in the database. When IBKR connectivity is added in Phase 2, the same logic will submit live stop orders and auto-detect manual exits.

---

## What it does

For each open position, the Position Manager runs through a fixed daily cycle:

1. **Load OHLCV** — reads the Parquet cache for the position's ticker to get the latest close and calculate ATR
2. **Advance peak_price** — if yesterday's close is a new all-time high for this trade, record it. The running high is the anchor for the ATR trail
3. **Check ATR and volatility bucket** — classify the instrument as low / medium / high volatility and select the matching ATR multiplier
4. **Trail not yet active → check trigger** — if the position has reached 1.5× its initial risk, activate the trailing stop simultaneously with the cost floor (PM-02/03)
5. **Trail not yet active → time-based exit** — if the trail has not triggered and the time limit has passed, evaluate whether the position should be closed to free up capital for better opportunities (PM-09–12)
6. **Trail already active → advance stop** — recalculate the ATR trail level from the running high; if it is higher than the current stop, update the stop in the database and issue a new stop order (PM-06/07)

---

## What it does NOT do

- **Model slippage or fills** — that is the Order Executor's job
- **Place the initial stop order** — the Order Executor places the stop at entry; the PM only modifies it as the trade progresses
- **Force a loss-based close** — PM-12 explicitly prohibits time-based closes that lock in losses; the initial stop is the only planned invalidation mechanism
- **Connect to IBKR in Phase 1** — stop orders are tracked in the database and logged; the operator must place or adjust them manually until Phase 2
- **Auto-detect manual exits in Phase 1** — Phase 2 will subscribe to `ib.positionEvent`; in Phase 1 the CLI fallback handles this

---

## How to run

```bash
# Run the daily EOD management batch (after DataFetcher completes)
python -m position_manager
python -m position_manager run

# Show all open positions with trail status and risk summary
python -m position_manager status

# Record a manual close (PM-14 fallback — when user closed via IBKR app)
python -m position_manager close ASML.AS 72.50
python -m position_manager close ASML.AS 72.50 "closed early — earnings risk"
```

**Normal daily workflow:**
```bash
python -m data_fetcher             # 1. Update Parquet cache with latest closes
python -m signal_engine            # 2. Scan for new signals, write to signals.db
python -m sim_executor             # 3. Evaluate signals, open new positions in risk.db
python -m position_manager         # 4. Manage all open positions (trail, time exit)
python -m risk_layer status        # 5. Review current risk exposure
```

Steps 2–4 run in sequence every trading day. Step 1 must complete before step 4 so the ATR calculations use fresh data.

---

## The trail logic

### Why 1.5R and not 2R?

Triggering the trail at 1.5× the initial risk — rather than at the target (2R) — gives the trade room to compound without being cut short. If the trail activated only at 2R, most winning trades would be exited near the target before they had a chance to run to 3R, 4R, or beyond. Activating at 1.5R means the stop is already risk-free (at the cost floor) before the target is reached, while the trade remains open for extended moves.

### Cost floor

The cost floor is the minimum stop level once trail is active:

```
cost_floor = entry_price × (1 + 2 × tob_pct / 100)
```

With Belgian TOB at 0.35%, a stock entered at €100 has a cost floor of €100.70. If the stop is hit exactly at the cost floor, the net P&L on the trade is approximately zero — neither a profit nor a loss after accounting for both the entry and exit TOB. No further capital is at risk once the trail is active.

### Volatility bucket and ATR multiplier

ATR% is ATR14 divided by the current close price. This normalises the raw ATR across instruments of different price levels so the bucket thresholds are meaningful in relative terms.

| Bucket | ATR% range | Multiplier | Rationale |
|---|---|---|---|
| Low | < 1.5% | 2.0× | Steady compounder; tight trail captures more of the move |
| Medium | 1.5% – 3.0% | 2.5× | Standard swing trade; balanced trail |
| High | > 3.0% | 3.0× | High-beta name needs breathing room to avoid noise stops |

### Active stop calculation

```
atr_trail_level = running_high − (ATR14 × multiplier)
active_stop     = max(cost_floor, atr_trail_level)
```

As the position advances and the running high rises, the ATR trail level rises with it. The cost floor provides the lower bound — the stop never falls back below break-even once trail is active. If ATR expands sharply on a volatile session (pushing the trail level below the current stop), the stop is left unchanged — it never moves down.

---

## The time-based exit

The time limit targets stalled positions that are consuming capital and risk budget without progressing. **It is not a deadline — it is a gate.** All three conditions must be true before any close is triggered.

### The three conditions (PM-10/11)

**1. Trading days open exceeds the limit (default: 7)**  
Counted from the OHLCV DatetimeIndex — rows in the Parquet cache since the open date. This automatically handles weekends and market holidays without a calendar library.

**2. Signal queue is non-empty**  
If there are no pending signals in `signals.db`, there is no opportunity cost to holding the stalled position. A capital-free hold in a flat market does not hurt the portfolio.

**3. Open risk is at or above the 6% cap**  
If the risk budget has room, the system can take new trades regardless. The time exit only matters when the stalled position is actively blocking a better opportunity.

**4. Price is within the stop proximity threshold (default: 25%)**  
Even when conditions 1–3 are met, the position is only closed if the price is within 25% of the entry-to-stop distance from the stop. Meaning: if the risk distance was €10 and the stop is €90, the price must be at or below €92.50 before a time exit fires.

**Why that last condition?** If the price is still well above the stop — say, 80% of the way from stop to entry — the trade is simply stalling at a neutral level. The stop is still far away and there is no particular reason to expect a stop-hit is imminent. Exiting there would lock in a small loss for no reason. The stop already handles the case where the setup is truly invalidated.

### Hold decisions are logged

Every time the time limit is exceeded but the conditions are not met, the Position Manager logs the specific reason a close was not triggered (`no_pending_signals`, `open_risk_below_cap`, `not_near_stop`). This gives the operator a clear picture of ageing positions on each day's log without hiding the reasoning behind the hold.

---

## Manual exits

When the operator closes a position directly via the IBKR platform — without the bot being involved — the position remains open in `risk.db` until it is recorded. In Phase 1 this requires the CLI fallback:

```bash
python -m position_manager close ASML.AS 72.50 "closed early — earnings risk"
```

This records the close with `bot_initiated=False`, `exit_reason='manual'`, and the optional note in the `exit_note` field for the trading diary. Net P&L is calculated and stored including exit commission.

In Phase 2, the Position Manager will subscribe to `ib.positionEvent`. Any position that goes to zero shares at IBKR without a corresponding bot-initiated order will be detected automatically and recorded with `exit_reason='manual'`.

---

## RL-10: how stop updates free the risk budget

Every time the active stop rises, `state.update_position_stop()` writes three fields to `risk_positions` atomically:

| Field | Update |
|---|---|
| `stop_price` | New stop level |
| `risk_per_share` | `entry_price − new_stop` (negative once stop is above entry) |
| `risk_amount` | `max(0, shares × risk_per_share)` |

The Risk Layer's `get_open_risk_amount()` sums `risk_amount WHERE status='open'`. Once the stop is at or above `entry + costs`, `risk_amount` becomes 0. The freed risk budget is reflected automatically on the Risk Layer's next `evaluate()` call — no explicit callback or notification is needed. A position that is running well and has a fully-raised trail effectively disappears from the open risk count, making room for new entries at the hard 6% cap.

---

## Telegram notifications

The Position Manager sends structured notifications for every significant event (PM-18):

| Event | Message |
|---|---|
| Trail activated | `🔵 TRAIL ACTIVATED · ASML.AS` with current price, new stop, cost floor, vol bucket |
| Trail updated | `⬆️ TRAIL UPDATE · ASML.AS` with old/new stop and running high |
| Time exit fired | `⏱️ TIME EXIT · ASML.AS` with close price, days open, gross and net P&L |
| Time exit hold | `⏳ TIME LIMIT REACHED · ASML.AS` with hold reason |
| Manual exit recorded | `🤚 MANUAL EXIT · ASML.AS` with close price, net P&L, and detection source |

As with all other components, notifications are best-effort: failures are logged at WARNING level and never interrupt position management. A database write is always committed before the notification is sent.

---

## File structure

```
position_manager/
├── __main__.py    CLI entry point: run / status / close
├── manager.py     PositionManager class — EOD loop, trail logic, time-exit, manual close
├── trail.py       Pure math: cost floor, volatility bucket, ATR trail, proximity ratio
├── state.py       SQLite writes: stop update (RL-10), peak price, trail activate, full close
├── notify.py      Telegram notifications for all PM events
└── __init__.py
```

---

## Configuration reference

All parameters live in `config.yaml` under the `position_manager:` section.

| Parameter | Default | Description |
|---|---|---|
| `trail_trigger_r` | `1.5` | Activate trail when profit reaches this multiple of initial risk |
| `time_limit_days` | `7` | Trading days open before time-exit conditions are evaluated |
| `atr_period` | `14` | ATR lookback (Wilder's smoothing, same as Signal Engine) |
| `stop_proximity_pct` | `25` | Time exit fires only if price is within this % of the stop distance |
| `atr_buckets.low_threshold_pct` | `1.5` | ATR% below this = low volatility bucket |
| `atr_buckets.high_threshold_pct` | `3.0` | ATR% above this = high volatility bucket |
| `atr_buckets.low_multiplier` | `2.0` | Trail width for low-volatility instruments |
| `atr_buckets.medium_multiplier` | `2.5` | Trail width for medium-volatility instruments |
| `atr_buckets.high_multiplier` | `3.0` | Trail width for high-volatility instruments |

---

## Stubbed features (pending Phase 2)

| Feature | Current state | Waiting on |
|---|---|---|
| Submit/modify stop orders at IBKR (PM-07) | Logs intent; operator places manually | `ib_insync` + IBKR connectivity |
| Auto-detect manual exits (PM-13) | No-op stub; use CLI close command | `ib.positionEvent` subscription |
| Current price for intraday trail updates | Uses daily close from Parquet cache | IBKR live tick feed |
