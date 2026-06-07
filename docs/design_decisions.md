# Trade Assistant — Architecture Design Decisions

Reference document covering the key architecture decisions made during the
build of the Signal Engine, Risk Layer, Walk-Forward Simulator, and the
planned Order Executor. Written as a durable record of *why* things are built
the way they are.

---

## 1. Pipeline overview

```
Signal Engine  →  signals.db  →  Sim Executor (paper)  →  Risk Layer  →  risk.db
                               →  Order Executor (live, IBKR)

Walk-Forward Runner (offline sim):
  Signal Engine (walker injected)  →  Risk Layer (wf_sim.db)  →  Sim PM  →  wf_sim.db
```

Every component reads from config.yaml. No magic numbers in code.

---

## 2. Module roles

| Module | Role | DB writes |
|---|---|---|
| Signal Engine | Scans OHLCV data, fires ranked signals | `signals.db → signals` |
| Risk Layer | Pre-trade gate: 6 checks, position sizing | `risk.db → risk_positions, system_state` |
| Sim Executor (SX) | Paper mode: reads signals.db, passes to RL, records fills | `risk.db → risk_positions` |
| Order Executor (OE) | Live mode: same as SX but calls IBKR API (not yet built) | `risk.db → risk_positions` |
| Position Manager (PM) | Manages stops, ATR trail, peak price for live positions | `risk.db → risk_positions` |
| Walk-Forward Runner | Offline sim orchestrator — drives SE, RL, SimPM day-by-day | `wf_sim.db` (all tables) |
| Sim PM | Sim variant of PM: gap-aware exits, ATR trail, time-exit. Pure math, no IO. | none |

**SX is paper/simulation, not live.** OE is the live module (IBKR API). Both
read from `signals.db` and pass signals to the same RL code.

---

## 3. Signal ranking

**Decision:** Ranking logic lives in `signal_engine/ranking.py` and is called
by `SignalEngine.scan()` before returning. Every caller — SX, OE, WF runner —
always receives a ranked list. `signal_rank=1` is highest priority.

**Ranking rule:**
1. Elevated conviction first (both strategies fired, or near 52-week high)
2. Within same tier: tightest stop % of entry (better capital efficiency)

**Why SE owns it:** SE has all the fields needed to rank (conviction, entry,
stop). It's the natural place to apply the rule once so all callers benefit
automatically.

**Persistence:** `signal_rank` is a field on the `Signal` dataclass and a
column in both `signals` (signals.db) and `wf_signals` (wf_sim.db). SX reads
signals ordered by `COALESCE(signal_rank, 99999), signal_timestamp`.

---

## 4. Risk Layer capacity check

**Decision:** RL uses *incremental discovery* — it evaluates signals one at a
time in ranked order. Check 6 (total open risk cap, 6%) fires naturally once
adding the next position would exceed the cap. There is no upfront "I can take
N more" calculation.

**Why:** The existing per-signal `evaluate()` already handles all six checks
correctly and in sequence. An explicit capacity pre-check would duplicate the
logic without adding value. The ranked input ensures the best signals are
evaluated first, so when the cap is hit, only lower-priority signals are left
behind.

---

## 5. Database structure — keep it simple

**Decision:** No database proliferation. Three databases total:

| Database | Contents | Mode |
|---|---|---|
| `signals.db` | All signals fired by SE | live / paper |
| `risk.db` | Open + closed positions, system state (daily-loss pause) | live / paper |
| `wf_sim.db` | Everything for one simulation run: WF tables + RL tables | WF sim only |

**Rejected alternatives:**
- `signals_sim.db` / `risk_sim.db` — unnecessary; wf_sim.db already isolates
  everything sim-related. The RL and SE simply receive a different `db_path`.
- `positions.db` — `risk.db → risk_positions` already holds both open and
  closed positions (closed rows have `closed_at` set). It *is* the trade log.

**WF isolation (Option C):** `wf_sim.db` holds both WF-specific tables and the
RL's standard tables (`risk_positions`, `system_state`). The WF runner deep-
copies config and overrides `risk.db_path → wf_db_path`. RL tables are cleared
at each run start. A startup `assert` aborts if paths are accidentally equal.

---

## 6. Walk-Forward Runner as sim orchestrator

**Decision:** The WF runner (`walk_forward/runner.py`) is the orchestrator for
the simulation, playing the combined role of SX + PM for the sim context.

- It drives the day loop (advance walker → scan → exits → entries → equity)
- It calls RL directly (not via SX)
- It calls SimPM for position management (no IBKR, no Telegram)
- It persists results to wf_sim.db

The live pipeline (SE → signals.db → SX/OE → RL → PM) and the sim pipeline
(WF runner → SE[walker] → RL[wf_sim.db] → SimPM) are parallel tracks that
share RL and SE code but nothing else.

---

## 7. Lookahead prevention (WF)

**Single enforcement point:** `DataFrameWalker.load(ticker, dir)` returns
`df[df.index <= current_date]`. All data access in the sim goes through the
walker. SE receives the walker via constructor injection (`cache=walker`, SE-25).
No other code needs to change.

---

## 8. Shared math utility: utils/pm_math.py

ATR trailing-stop math that is used by both live PM and sim PM lives in
`utils/pm_math.py`. It was moved from `position_manager/trail.py` (deleted).
Both `position_manager/manager.py` and `walk_forward/sim_pm.py` import from
`utils.pm_math`. There is no duplication of cost floor, volatility bucket, ATR
multiplier, or active stop formulas.

---

## 9. Portfolio value in simulation

After each exit, the WF runner:
1. Adds `ExitResult.net_pnl` to `portfolio_value`
2. Updates `wf_config["risk"]["portfolio_value_stub"]` (for SimPM sizing)
3. Directly mutates `risk_layer._portfolio_value_stub` (because RL copies the
   stub at init — changing the config dict afterwards has no effect)

This ensures each day's position sizing is based on the actual simulated
account value, not the original starting capital.

---

## 10. "Positions with active RISK" — deferred

The RL currently stores the original `risk_amount` at entry and never updates
it as stops trail. A position whose stop has trailed above cost floor has zero
(or negative) actual downside, but RL still counts it at its original risk
amount. This means `current_open_risk_pct` is slightly overstated for
trailing positions.

**Status:** Noted, not yet implemented. Fixing this requires `get_open_risk_amount()`
in `risk_layer/state.py` to recompute risk dynamically from the current stop
price rather than the stored `risk_amount` column.
