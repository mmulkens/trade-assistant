# Trade Assistant — Pipeline Communication Schema

How modules communicate: what calls what, what lives in memory, and what is
persisted in SQLite and by whom.

---

## Live / Paper pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│  python -m signal_engine                                        │
│                                                                 │
│  SignalEngine.scan(tickers)                                     │
│    ├─ data_fetcher.cache.load(ticker, cache_dir)  [reads Parquet]
│    ├─ StrategyA.evaluate()   ──┐                                │
│    ├─ StrategyB.evaluate()   ──┤ → Signal objects (in memory)  │
│    └─ ranking.rank_signals()   ┘ → signal_rank assigned        │
│                                                                 │
│  signal_engine.db.save_signals(signals, db_path)               │
│    └─ WRITES  signals.db → signals (incl. signal_rank)         │
└─────────────────────────────────────────────────────────────────┘
                          │
                     signals.db
                          │
┌─────────────────────────────────────────────────────────────────┐
│  python -m sim_executor   (paper)                               │
│  python -m order_executor (live, IBKR — not yet built)         │
│                                                                 │
│  SimExecutor._fetch_unprocessed()                               │
│    └─ READS   signals.db → signals  ORDER BY signal_rank       │
│                                                                 │
│  for each signal (in rank order):                               │
│    RiskLayer.evaluate(signal)                                   │
│      ├─ READS  risk.db → risk_positions (open risk amount)     │
│      ├─ READS  risk.db → system_state   (trading-pause flag)   │
│      └─ returns RiskDecision (in memory)                       │
│                                                                 │
│    if approved:                                                 │
│      fills.record_fill()                                       │
│        ├─ RiskLayer.open_position()                            │
│        │    └─ WRITES risk.db → risk_positions                 │
│        └─ (OE only) IBKR API — submit order                   │
│                                                                 │
│    WRITES  signals.db → signals.processed = 1                  │
└─────────────────────────────────────────────────────────────────┘
                          │
                       risk.db
                          │
┌─────────────────────────────────────────────────────────────────┐
│  Position Manager  (runs on schedule / IBKR price events)       │
│                                                                 │
│  PositionManager.run()                                          │
│    ├─ READS  risk.db → risk_positions (all open positions)     │
│    ├─ data_fetcher.cache.load() / IBKR live price              │
│    ├─ utils.pm_math — ATR trail, cost floor, stop calc         │
│    ├─ pm_state.update_position_stop()                          │
│    │    └─ WRITES risk.db → risk_positions.stop_price          │
│    ├─ pm_state.activate_trail()                                │
│    │    └─ WRITES risk.db → risk_positions.trail_active        │
│    └─ RiskLayer.close_position(ticker, price, reason)          │
│         └─ WRITES risk.db → risk_positions (closed_at, pnl)   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Walk-Forward Simulation pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│  python -m walk_forward                                         │
│                                                                 │
│  WalkForwardRunner.run(tickers)                                 │
│    │                                                            │
│    ├─ INIT:                                                     │
│    │   rl_state.init_db(wf_db_path)                            │
│    │     └─ WRITES wf_sim.db → risk_positions, system_state    │
│    │   storage.init_db(wf_db_path)                             │
│    │     └─ WRITES wf_sim.db → wf_runs, wf_positions,         │
│    │                            wf_signals, wf_equity_curve     │
│    │   DELETE FROM risk_positions / system_state  (clean slate) │
│    │   storage.create_run()                                     │
│    │     └─ WRITES wf_sim.db → wf_runs                        │
│    │                                                            │
│    ├─ DataFrameWalker.__init__()                               │
│    │   └─ READS  all Parquet files → memory (_store dict)      │
│    │                                                            │
│    └─ Day loop (for each sim date):                            │
│                                                                 │
│        walker.advance(date)         [updates _current_date]    │
│                                                                 │
│        ── SCAN ──────────────────────────────────────────────  │
│        SignalEngine.scan(eligible)                             │
│          ├─ walker.load(ticker, _)  [reads from memory _store] │
│          ├─ StrategyA / StrategyB / indicators                 │
│          └─ ranking.rank_signals()                             │
│          → list[Signal] ranked, in memory                      │
│                                                                 │
│        ── EXITS ────────────────────────────────────────────── │
│        rl_state.get_open_positions(wf_db_path)                 │
│          └─ READS wf_sim.db → risk_positions                   │
│                                                                 │
│        for each open position:                                 │
│          walker.load(ticker)  [memory]                         │
│          ind.atr(df, period)  [memory]                         │
│          SimPositionManager.evaluate(pos, bar, …)              │
│            └─ utils.pm_math  [pure math, memory only]         │
│            → ExitResult (in memory)                            │
│                                                                 │
│          if closed:                                            │
│            RiskLayer.close_position()                          │
│              └─ WRITES wf_sim.db → risk_positions             │
│            storage.record_position_close()                     │
│              └─ WRITES wf_sim.db → wf_positions               │
│          else:                                                 │
│            pm_state.update_position_stop()   (if stop raised)  │
│              └─ WRITES wf_sim.db → risk_positions             │
│            storage.update_position_stop()    (if stop raised)  │
│              └─ WRITES wf_sim.db → wf_positions               │
│            pm_state.activate_trail()         (if trail fires)  │
│              └─ WRITES wf_sim.db → risk_positions             │
│                                                                 │
│        ── ENTRIES ──────────────────────────────────────────── │
│        for each ranked signal:                                 │
│          RiskLayer.evaluate(signal)                            │
│            └─ READS  wf_sim.db → risk_positions, system_state │
│            → RiskDecision (in memory)                          │
│                                                                 │
│          storage.record_signal(…, action)                      │
│            └─ WRITES wf_sim.db → wf_signals                   │
│                                                                 │
│          if approved:                                          │
│            RiskLayer.open_position()                           │
│              └─ WRITES wf_sim.db → risk_positions             │
│            storage.record_position_open()                      │
│              └─ WRITES wf_sim.db → wf_positions               │
│                                                                 │
│        ── EQUITY SNAPSHOT ────────────────────────────────────  │
│        storage.record_equity(date, portfolio_value, n_open)    │
│          └─ WRITES wf_sim.db → wf_equity_curve                │
│                                                                 │
│    storage.close_run(portfolio_end, total_trades)              │
│      └─ WRITES wf_sim.db → wf_runs                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## What lives where

### In memory (not persisted)

| Object | Where created | Lifetime |
|---|---|---|
| `list[Signal]` | `SignalEngine.scan()` | per scan call |
| `DataFrameWalker._store` | `WalkForwardRunner.run()` init | entire WF run |
| `ExitResult` | `SimPositionManager.evaluate()` | per bar per position |
| `RiskDecision` | `RiskLayer.evaluate()` | per signal |
| `ticker_to_wf_id: dict` | WF runner day loop | entire WF run |
| `portfolio_value: float` | WF runner | entire WF run |

### SQLite: signals.db

| Table | Written by | Read by |
|---|---|---|
| `signals` | `signal_engine.db.save_signals()` | SX / OE `_fetch_unprocessed()` |

### SQLite: risk.db

| Table | Written by | Read by |
|---|---|---|
| `risk_positions` | RL `open_position()`, `close_position()` / PM `update_position_stop()`, `activate_trail()` | RL `evaluate()`, PM `run()` |
| `system_state` | RL `set_trading_pause()` | RL `is_trading_paused()` |

### SQLite: wf_sim.db

| Table | Written by | Read by |
|---|---|---|
| `wf_runs` | `storage.create_run()`, `storage.close_run()` | `summary.calculate_summary()`, `__main__._cmd_runs()` |
| `wf_positions` | `storage.record_position_open/close/update_stop()` | `summary.calculate_summary()` |
| `wf_signals` | `storage.record_signal()` | `summary` / manual queries |
| `wf_equity_curve` | `storage.record_equity()` | `summary._calc_max_drawdown()` |
| `risk_positions` | RL `open/close_position()`, pm_state helpers (wf_db path) | RL `evaluate()`, `rl_state.get_open_positions()` |
| `system_state` | RL `set_trading_pause()` (wf_db path) | RL `is_trading_paused()` (wf_db path) |

---

## Key injection points

| What | Default (live) | Injected (WF sim) |
|---|---|---|
| `SignalEngine` cache source | `data_fetcher.cache` (Parquet files) | `DataFrameWalker` (pre-loaded, time-bounded) |
| `RiskLayer` db_path | `config["risk"]["db_path"]` → risk.db | deep-copied config → wf_sim.db |
| `pm_state` db_path | risk.db | wf_sim.db (passed explicitly by WF runner) |
