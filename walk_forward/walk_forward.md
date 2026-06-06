# Walk-Forward Simulator

The Walk-Forward Simulator is an isolated replay engine that runs the full trade pipeline — Signal Engine, Risk Layer, and position management — on historical OHLCV data, advancing one trading day at a time. It produces an equity curve, a trade log, and summary statistics, all written to a dedicated `wf_sim.db` SQLite database that never touches the live `risk.db`.

```
Historical OHLCV (Parquet cache)
          │
          ▼
   DataFrameWalker          ← replaces data_fetcher.cache; truncates to current sim date
          │
          ├──► SignalEngine  ← injected with Walker via SE-25 constructor parameter
          │         │
          │         ▼
          │    RiskLayer     ← reused unchanged; writes to wf_sim.db (isolated)
          │         │
          ▼         ▼
   SimPositionManager        ← gap-aware exits, ATR trail, time-exit; pure math, no IO
          │
          ▼
     wf_sim.db               ← wf_runs, wf_positions, wf_signals, wf_equity_curve
```

The simulator is not a toy backtest. It reuses every piece of production logic — the same indicator calculations, the same six-check Risk Layer, the same ATR multiplier tiers, the same time-exit conditions. The only things that change are the data source (Walker instead of live cache) and the output target (wf_sim.db instead of risk.db).

---

## What it does

1. **DataFrameWalker** pre-loads all Parquet files at startup. Each call to `walker.load(ticker, cache_dir)` returns only the rows up to and including the current simulation date — the single enforcement point for no-lookahead.

2. **WalkForwardRunner** orchestrates the day loop:
   - Advances the walker to today's date
   - Scans for signals via the (walker-injected) Signal Engine
   - Processes exits for all open positions: gap-down fills, intraday stop hits, ATR trail advances, and time-based exits
   - Tries to enter approved signals through the Risk Layer
   - Records a daily equity curve snapshot

3. **SimPositionManager** evaluates one position against one OHLCV bar. It returns an `ExitResult` dataclass — closed positions include P&L; open positions include the updated stop and a `trail_activated` flag if 1.5R was reached for the first time.

4. **RankSignals** sorts signals before entry attempts: elevated conviction first, then tightest stop percentage within the same tier.

5. **Summary** computes return, win rate, profit factor, max drawdown, average holding period, and exit reason breakdown from a completed run's DB records.

---

## What it does NOT do

- **Model intraday fill slippage** — entry fills at the signal's closing price; exits fill at the open or stop level as applicable.
- **Simulate partial fills or order queuing** — each approved signal fills in full on the day it fires.
- **Predict future volatility** — ATR is recomputed from walker-truncated data; no lookahead into future bars is possible.
- **Modify live databases** — wf_sim.db is strictly separate from risk.db and signals.db. A startup assert aborts the run if the paths are ever accidentally equal.
- **Send notifications** — the sim_pm is notification-free; all output goes to the structured JSON logger.

---

## Lookahead prevention

The `DataFrameWalker` is the only mechanism needed. The runner calls `walker.advance(date)` once per day, and every subsequent `walker.load()` call — whether from SignalEngine, the runner itself, or ATR computation — returns `df[df.index <= date]`. No other guard is required, and no other code needs to change to prevent lookahead.

The Signal Engine receives the walker via constructor injection (SE-25):
```python
engine = SignalEngine(config, logger, cache=walker)
```
This is the only change to existing Signal Engine code.

---

## State isolation

The simulation uses `wf_sim.db` for everything:
- **Risk Layer tables** (`risk_positions`, `system_state`) are created by `rl_state.init_db(wf_db_path)` at run start and cleared before each run.
- **Simulation tables** (`wf_runs`, `wf_positions`, `wf_signals`, `wf_equity_curve`) are created by `storage.init_db()` and accumulate across runs — each run gets a UUID `run_id` for independent querying.

The RiskLayer instance is created with a deep-copied config where `risk.db_path` points to `wf_sim.db`. All Risk Layer evaluations, open position checks, and closes write to wf_sim.db, never risk.db.

---

## Portfolio value

`config['risk']['portfolio_value_stub']` is the starting value (default €100,000). After each exit, the runner adds `ExitResult.net_pnl` to `portfolio_value` and updates both the config dict and the RiskLayer instance's internal stub. This means the Risk Layer sizes the next day's positions against the actual simulated account value, not the original starting capital.

---

## How to run

```bash
# Run simulation on the configured watchlist (nasdaq100, sp500, custom)
python -m walk_forward

# Explicit subcommand
python -m walk_forward run

# List all past simulation runs
python -m walk_forward runs

# Show detailed summary for a specific run
python -m walk_forward summary <run_id>
```

**Before running the simulation**, make sure the Parquet cache covers 700+ days of history:
```bash
# Update config.yaml: history_days is already set to 700
python -m data_fetcher --full-refresh
```

The simulation requires at least `min_ticker_days` (default 700) calendar days of Parquet cache per ticker, plus `min_warmup_bars` (default 200) bars on the benchmark before the simulation window opens. Tickers that fail the eligibility check are skipped and logged; the simulation runs on the remaining eligible universe.

---

## Shared utility: utils/pm_math.py

The ATR trailing-stop math that was previously in `position_manager/trail.py` is now in `utils/pm_math.py`. Both `position_manager/manager.py` (live) and `walk_forward/sim_pm.py` (simulation) import from this shared module. There is no duplication of the cost floor, volatility bucket, ATR multiplier, or active stop formulas.

---

## Output tables (wf_sim.db)

| Table | Description |
|---|---|
| `wf_runs` | One row per simulation run: dates, portfolio start/end, trade count |
| `wf_positions` | One row per trade: entry, exit, P&L, signal metadata |
| `wf_signals` | Every signal that fired during the sim, with action taken (entered / skipped) |
| `wf_equity_curve` | Daily portfolio value snapshot and open position count |
| `risk_positions` | Standard RL table, cleared at each run start |
| `system_state` | Standard RL table for daily-loss pause flag |
