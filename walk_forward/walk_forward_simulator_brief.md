# Walk-Forward Simulator — Coding Brief
# Trade Assistant · VS Code Implementation Handoff

*This document is the complete implementation specification for the Walk-Forward
Simulator (WF). Read it fully before writing any code. All design decisions are final
unless explicitly marked as open.*

---

## 1. What you are building

A CLI module (`walk_forward_simulator/`) that replays the existing Signal Engine →
Risk Layer → Sim Executor → Sim Position Manager pipeline on historical OHLCV data,
advancing one trading day at a time, to measure strategy performance without lookahead
bias.

**The core principle: reuse, don't replicate.** The Signal Engine, Risk Layer, and Sim
Executor are called with their real logic unchanged. The WF module drives them with a
time-restricted view of historical data. The only new logic you write is:

1. The **DataFrame Walker** — intercepts cache reads, returns truncated data
2. The **Sim Position Manager** — bar-by-bar exit simulation
3. The **day loop orchestrator** — coordinates component calls per iteration
4. The **storage layer** — writes results to wf_sim.db

---

## 2. One required change to existing code

### signal_engine/engine.py — constructor injection

This is the only change to any existing file. It enables the DataFrame Walker to be
passed in as the cache source.

**Find this in `__init__`:**
```python
from data_fetcher import cache as cache_store

class SignalEngine:
    def __init__(self, config: dict, logger: Logger) -> None:
        ...
```

**Change to:**
```python
from data_fetcher import cache as cache_store

class SignalEngine:
    def __init__(self, config: dict, logger: Logger, cache=cache_store) -> None:
        self._cache = cache
        ...
```

**Then replace every occurrence of `cache_store.load(` inside the class with
`self._cache.load(`.**

Searching the file, there are two calls to replace:
- In `scan()`: `benchmark_df = cache_store.load(self._benchmark, self._cache_dir)`
- In `_scan_one()`: `df = cache_store.load(ticker, self._cache_dir)`

Both become `self._cache.load(...)`.

**Verify nothing is broken:** existing call sites like `SignalEngine(config, logger)`
continue to work identically — the default `cache=cache_store` means no callers need
updating.

---

## 3. New shared module: signal_engine/pm_math.py

Before building the Sim PM, extract shared pure functions from the live Position
Manager into this module. Both the live PM and the Sim PM will import from here.
No logic duplication.

Functions to extract (check the live Position Manager source for exact implementations
and move them here):

```python
# pm_math.py

def atr_multiplier(atr_pct: float, config: dict) -> float:
    """Return ATR trail multiplier based on volatility bucket.
    
    Buckets from config (position_manager.atr_buckets):
        low_threshold_pct:   default 1.5  → multiplier 2.0
        high_threshold_pct:  default 3.0  → multiplier 3.0
        else medium:                       → multiplier 2.5
    """
    ...

def cost_floor(entry_price: float, shares: int, tob_pct: float) -> float:
    """Return the minimum stop level that covers round-trip transaction costs.
    
    cost_floor = entry_price + (entry_price * tob_pct / 100 * 2)
    (both entry and exit TOB)
    """
    ...

def active_stop(cost_floor: float, atr_trail_stop: float) -> float:
    """Return max(cost_floor, atr_trail_stop). Stop only ever moves up."""
    return max(cost_floor, atr_trail_stop)

def atr_trail_level(running_high: float, atr_value: float, multiplier: float) -> float:
    """Return running_high - (atr_value * multiplier)."""
    return running_high - (atr_value * multiplier)
```

---

## 4. Module structure

```
walk_forward_simulator/
├── __main__.py         CLI entry point — argument parsing, wires all components
├── runner.py           WalkForwardRunner — day loop orchestrator
├── walker.py           DataFrameWalker — cache interception and date truncation
├── sim_pm.py           SimPositionManager — bar-by-bar exit logic
├── ranking.py          signal_ranking() — sorts signals before RL evaluation
├── storage.py          SQLite schema creation, all read/write functions for wf_sim.db
├── summary.py          Aggregate statistics calculation and formatting
└── __init__.py         Exports WalkForwardRunner
```

---

## 5. DataFrameWalker (walker.py)

### Purpose
Drop-in replacement for `data_fetcher.cache`. Exposes the same interface but returns
data truncated to the current simulation date.

### Interface contract
Must match `data_fetcher.cache` exactly:
```python
def load(ticker: str, cache_dir: str) -> Optional[pd.DataFrame]:
    ...
```

### Implementation

```python
import pandas as pd
from datetime import date
from typing import Optional
from data_fetcher import cache as real_cache

class DataFrameWalker:
    def __init__(self, current_date: date) -> None:
        self.current_date = current_date  # updated by runner on each iteration

    def load(self, ticker: str, cache_dir: str) -> Optional[pd.DataFrame]:
        df = real_cache.load(ticker, cache_dir)
        if df is None:
            return None
        # Strict truncation: only rows with date index <= current_date
        # The DatetimeIndex may be tz-naive; compare accordingly
        cutoff = pd.Timestamp(self.current_date)
        return df[df.index <= cutoff]
```

### Key points
- `current_date` is a mutable attribute. The runner updates it at the start of each
  iteration: `walker.current_date = simulation_date`. The walker is created once and
  reused across all iterations.
- The truncation is strict `<=`. Never `<`.
- The walker delegates the actual Parquet read to the real cache — it does not
  re-implement file reading.
- If `real_cache.load()` returns None (ticker not in cache), the walker returns None.

### Testability
Write a unit test: load a known ticker, set `current_date` to an arbitrary date in the
middle of its history, call `walker.load()`, assert that `df.index.max().date() == current_date`.

---

## 6. WalkForwardRunner (runner.py)

### Purpose
The day loop orchestrator. Advances the date cursor and coordinates all component calls
on each iteration.

### Constructor
```python
class WalkForwardRunner:
    def __init__(
        self,
        config: dict,
        logger: Logger,
        start_date: date,
        end_date: date,
        starting_portfolio_value: float,
        run_id: str,
        db_path: str,
        dry_run: bool = False,
    ) -> None:
```

### Startup safety check
Before anything else:
```python
assert db_path != config['risk']['db_path'], (
    f"ABORT: wf db_path '{db_path}' must not equal risk db_path "
    f"'{config['risk']['db_path']}'. Running the simulator against the live "
    f"risk database would corrupt live state."
)
```

### Warmup skip
Before entering the day loop, determine the first valid simulation date:

```python
# Load benchmark to count available bars
benchmark_df = real_cache.load(config['signal_engine']['benchmark'], cache_dir)
trading_days = benchmark_df.index  # full index, all dates

# Find first date where at least min_bars of data exist
# min_bars comes from SignalEngine — instantiate it once to read the attribute
engine = SignalEngine(config, logger, cache=walker)
min_bars = engine._min_bars

first_valid_date = None
for d in trading_days:
    bars_available = (trading_days <= d).sum()
    if bars_available >= min_bars:
        first_valid_date = d.date()
        break

effective_start = max(start_date, first_valid_date)
if effective_start > start_date:
    logger.info({
        'event': 'warmup_skip',
        'requested_start': str(start_date),
        'effective_start': str(effective_start),
        'warmup_days_skipped': (effective_start - start_date).days,
        'min_bars_required': min_bars,
    })
```

### Day loop (pseudocode — implement exactly this sequence)

```python
walker = DataFrameWalker(current_date=effective_start - one_trading_day)
open_positions = []  # list of dicts, each representing an open simulated position
portfolio_value = starting_portfolio_value

for sim_date in trading_days_between(effective_start, end_date):
    
    # -----------------------------------------------------------------------
    # Step 1: Signal Engine scans on (sim_date - 1) close data
    # Walker is already set to sim_date - 1 from prior iteration end
    # (or initialised to effective_start - 1 trading day before loop starts)
    # -----------------------------------------------------------------------
    signals = engine.scan(tickers)

    # -----------------------------------------------------------------------
    # Step 2: Rank signals
    # -----------------------------------------------------------------------
    ranked = ranking.rank_signals(signals)

    # -----------------------------------------------------------------------
    # Step 3: Risk Layer evaluates ranked signals
    # -----------------------------------------------------------------------
    queued_fills = []
    for signal in ranked:
        decision = risk_layer.evaluate(signal)
        outcome = map_rl_outcome(decision)  # see mapping table below
        storage.insert_wf_signal(signal, outcome, decision.reject_reason, run_id, db)
        if decision.approved:
            queued_fills.append((signal, decision))

    # -----------------------------------------------------------------------
    # Step 4: Reveal bar sim_date — confirm or reject entries
    # -----------------------------------------------------------------------
    walker.current_date = sim_date  # NOW reveal today's bar

    for signal, decision in queued_fills:
        bar = get_bar(signal.ticker, sim_date)  # single row from walker
        if bar is None:
            # No data for this ticker on sim_date — skip, log
            continue
        entry_price = signal.entry_price  # D-1 close
        if bar['low'] <= entry_price <= bar['high']:
            # Entry confirmed — open position
            position = build_position(signal, decision, sim_date, entry_price)
            open_positions.append(position)
            risk_layer.open_position(decision)
            storage.insert_wf_position(position, run_id, db)
            # Update wf_signals outcome to 'filled'
            storage.update_wf_signal_outcome(signal, 'filled', db)
        else:
            # Gap — entry rejected
            storage.update_wf_signal_outcome(signal, 'rejected_gap', db)

    # -----------------------------------------------------------------------
    # Step 5: Sim PM evaluates ALL open positions against bar sim_date
    # -----------------------------------------------------------------------
    still_open = []
    for position in open_positions:
        bar = get_bar(position['ticker'], sim_date)
        if bar is None:
            still_open.append(position)
            continue
        exit_result = sim_pm.evaluate(position, bar, signals, open_positions, config)
        if exit_result.closed:
            position.update(exit_result.fields)
            risk_layer.close_position(position['ticker'], exit_result.exit_price, exit_result.exit_reason)
            storage.update_wf_position_closed(position, db)
            # Notify RL if trail was updated before close
        else:
            # Update position state (trail level, running_high, hold_days)
            position.update(exit_result.updated_fields)
            if exit_result.stop_updated:
                risk_layer.update_stop(position['ticker'], exit_result.new_stop)
            still_open.append(position)
    open_positions = still_open

    # -----------------------------------------------------------------------
    # Step 6: Update simulated portfolio value
    # -----------------------------------------------------------------------
    closed_today = [p for p in open_positions if p not in still_open]
    portfolio_value += sum(p['net_pnl'] for p in closed_today)
    # Inject updated value into Risk Layer for next iteration
    config['risk']['portfolio_value_stub'] = portfolio_value

    # -----------------------------------------------------------------------
    # Step 7: Store equity curve row
    # -----------------------------------------------------------------------
    storage.insert_equity_curve(run_id, sim_date, portfolio_value, len(open_positions), db)

    # Advance walker to sim_date for next iteration's signal scan
    # (next iteration Step 1 will scan on sim_date, which becomes D-1)
    # Walker is already at sim_date from Step 4 reveal — no change needed
```

### RL outcome mapping

| RL reject_reason | wf_signals outcome |
|---|---|
| `None` (approved) | `filled` (updated in Step 4) or `rejected_gap` |
| `duplicate_instrument` | `rejected_duplicate` |
| `open_risk_cap_exceeded` | `rejected_budget` |
| `position_risk_cap_exceeded` | `rejected_budget` |
| `zero_shares` | `rejected_risk` |
| `trading_paused*` | `rejected_risk` |
| any other | `rejected_risk` |

---

## 7. Signal Ranking (ranking.py)

```python
from signal_engine.engine import Signal

def rank_signals(signals: list[Signal]) -> list[Signal]:
    """Sort signals for Risk Layer evaluation priority.
    
    Priority:
        1. Elevated conviction before standard
        2. Within same tier: tightest stop distance % of entry first
           (tightest = smallest (entry - stop) / entry)
    """
    def sort_key(s: Signal):
        conviction_rank = 0 if s.conviction == 'elevated' else 1
        stop_pct = (s.entry_price - s.stop_price) / s.entry_price
        return (conviction_rank, stop_pct)

    return sorted(signals, key=sort_key)
```

---

## 8. SimPositionManager (sim_pm.py)

### Purpose
Evaluates exit conditions for a single open position against a single historical bar.
Returns either a close result or an updated position state.

### Position state dictionary
Each open position is represented as a dict with these fields:

```python
{
    # Set at entry
    'ticker': str,
    'entry_price': float,
    'entry_date': date,
    'stop_price': float,          # active stop — updated as trail moves up
    'initial_stop': float,        # original stop at entry — never changes
    'target_price': float,
    'shares': int,
    'risk_per_share': float,      # entry_price - initial_stop
    'liquidity_class': str,
    'signal_type': str,
    'conviction': str,
    'run_id': str,
    'tob_pct': float,             # from config, for cost floor calculation

    # Trail state — updated by Sim PM
    'trail_active': bool,         # False until 1.5R reached
    'cost_floor': float,          # entry + round-trip TOB — set when trail activates
    'running_high': float,        # highest close since entry — updated daily
    'atr_trail_stop': float,      # current ATR trail level — None until trail active
    'atr_multiplier': float,      # set when trail activates, fixed thereafter

    # Tracking
    'hold_days': int,             # trading days since entry
    'peak_price': float,          # highest price reached during hold
}
```

### evaluate() method

```python
from dataclasses import dataclass
from typing import Optional
import pandas as pd
from . import pm_math

@dataclass
class ExitResult:
    closed: bool
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None   # stop_hit / trail_hit / time_exit
    net_pnl: Optional[float] = None
    gross_pnl: Optional[float] = None
    updated_fields: dict = None          # position state updates if not closed
    stop_updated: bool = False
    new_stop: Optional[float] = None

class SimPositionManager:

    def evaluate(
        self,
        position: dict,
        bar: pd.Series,              # single row: open, high, low, close, volume
        signal_queue: list,          # today's new signals (for time exit check)
        all_open_positions: list,    # all open positions (for risk % check)
        config: dict,
        atr_value: float,            # ATR14 for this ticker on this bar (from walker)
    ) -> ExitResult:

        active_stop = position['stop_price']
        entry = position['entry_price']
        initial_stop = position['initial_stop']
        risk = entry - initial_stop

        # --- Check 1: Gap stop ---
        if bar['open'] <= active_stop:
            return self._close(position, bar['open'], 'stop_hit')

        # --- Check 2: Intraday stop ---
        if bar['low'] <= active_stop:
            return self._close(position, active_stop, 'stop_hit')

        # --- Check 3: Trail trigger (1.5R) ---
        trail_trigger_price = entry + 1.5 * risk
        if not position['trail_active'] and bar['close'] >= trail_trigger_price:
            position['trail_active'] = True
            position['cost_floor'] = pm_math.cost_floor(
                entry, position['shares'], position['tob_pct']
            )
            atr_pct = (atr_value / bar['close']) * 100
            position['atr_multiplier'] = pm_math.atr_multiplier(atr_pct, config)
            position['running_high'] = bar['high']
            position['atr_trail_stop'] = pm_math.atr_trail_level(
                position['running_high'], atr_value, position['atr_multiplier']
            )
            new_stop = pm_math.active_stop(position['cost_floor'], position['atr_trail_stop'])
            position['stop_price'] = new_stop
            # Fall through — trail just activated, no exit on this bar

        # --- Check 4: Trail update ---
        if position['trail_active'] and bar['high'] > position['running_high']:
            position['running_high'] = bar['high']
            position['atr_trail_stop'] = pm_math.atr_trail_level(
                position['running_high'], atr_value, position['atr_multiplier']
            )
            new_stop = pm_math.active_stop(position['cost_floor'], position['atr_trail_stop'])
            if new_stop > position['stop_price']:
                position['stop_price'] = new_stop
                # Signal RL to update risk contribution
                return ExitResult(
                    closed=False,
                    updated_fields=position,
                    stop_updated=True,
                    new_stop=new_stop,
                )

        # --- Check 5: Time exit ---
        position['hold_days'] += 1
        if position['hold_days'] >= config['position_manager']['time_limit_days']:
            if not position['trail_active']:
                if self._time_exit_conditions_met(position, signal_queue, all_open_positions, config):
                    return self._close(position, bar['close'], 'time_exit')

        # --- Update tracking fields ---
        position['peak_price'] = max(position['peak_price'], bar['high'])
        return ExitResult(closed=False, updated_fields=position)

    def _time_exit_conditions_met(
        self,
        position: dict,
        signal_queue: list,
        all_open: list,
        config: dict,
    ) -> bool:
        """Time exit fires only if all three conditions are true:
        1. Signal queue is non-empty (there is a better use for this capital)
        2. Total open risk >= 6% (budget is full — capital cannot be deployed anyway
           without closing something)
        3. Position is within 20-30% of its stop distance (near invalidation)
        """
        if not signal_queue:
            return False
        
        # Calculate total open risk — sum across all open positions
        # This uses the same formula as RL-02
        portfolio_value = config['risk']['portfolio_value_stub']
        total_open_risk = sum(
            (p['risk_per_share'] * p['shares']) / portfolio_value * 100
            for p in all_open
        )
        if total_open_risk < config['risk']['max_open_risk_pct']:
            return False
        
        # Check stop proximity
        entry = position['entry_price']
        stop = position['stop_price']
        current_price = position['running_high']  # best proxy for current price
        stop_distance = current_price - stop
        proximity_threshold = config['position_manager'].get('stop_proximity_pct', 25) / 100
        if (current_price - stop) / (entry - stop) > proximity_threshold:
            return False
        
        return True

    def _close(self, position: dict, exit_price: float, reason: str) -> ExitResult:
        gross_pnl = (exit_price - position['entry_price']) * position['shares']
        entry_cost = position['entry_price'] * position['shares'] * position['tob_pct'] / 100
        exit_cost = exit_price * position['shares'] * position['tob_pct'] / 100
        net_pnl = gross_pnl - entry_cost - exit_cost
        position.update({
            'exit_price': exit_price,
            'exit_reason': reason,
            'gross_pnl': gross_pnl,
            'net_pnl': net_pnl,
        })
        return ExitResult(
            closed=True,
            exit_price=exit_price,
            exit_reason=reason,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            updated_fields=position,
        )
```

### Zero-day stop-out detection
In the runner, after Step 5, check:
```python
for closed_position in positions_closed_this_iteration:
    if closed_position['exit_date'] == closed_position['entry_date']:
        logger.warning({
            'event': 'zero_day_stopout',
            'ticker': closed_position['ticker'],
            'entry_price': closed_position['entry_price'],
            'stop_price': closed_position['initial_stop'],
            'bar_low': bar['low'],
            'message': 'Position stopped out on entry bar — stop may have been above bar low at signal time',
        })
```

---

## 9. Storage layer (storage.py)

### Schema creation

Run on startup (CREATE TABLE IF NOT EXISTS):

```sql
-- Run metadata
CREATE TABLE IF NOT EXISTS wf_runs (
    run_id TEXT PRIMARY KEY,
    start_date TEXT,
    end_date TEXT,
    effective_start_date TEXT,
    starting_portfolio_value REAL,
    final_portfolio_value REAL,
    config_snapshot TEXT,          -- full config.yaml as JSON string
    run_timestamp TEXT,            -- ISO8601 UTC
    universe_size INTEGER,
    trading_days_simulated INTEGER,
    intraday_simulated INTEGER,    -- always 0 in v1
    -- Aggregate stats (populated at run end)
    total_trades INTEGER,
    win_rate REAL,
    avg_r_multiple REAL,
    expectancy REAL,
    max_drawdown REAL,
    max_consecutive_losses INTEGER,
    signal_conversion_rate REAL,
    gap_rejection_rate REAL,
    avg_hold_days REAL
);

-- Every signal evaluated, all outcomes
CREATE TABLE IF NOT EXISTS wf_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    signal_date TEXT,
    ticker TEXT,
    signal_type TEXT,
    conviction TEXT,
    entry_price REAL,
    stop_price REAL,
    stop_pct REAL,
    target_price REAL,
    strategy_a_fired INTEGER,
    strategy_b_fired INTEGER,
    outcome TEXT,                  -- filled/rejected_risk/rejected_gap/rejected_duplicate/rejected_budget
    reject_reason TEXT
);

-- Closed positions — same schema as risk_positions plus run_id
CREATE TABLE IF NOT EXISTS wf_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    ticker TEXT,
    entry_date TEXT,
    exit_date TEXT,
    entry_price REAL,
    exit_price REAL,
    initial_stop REAL,
    stop_price REAL,
    target_price REAL,
    shares INTEGER,
    risk_per_share REAL,
    signal_type TEXT,
    conviction TEXT,
    liquidity_class TEXT,
    trail_active INTEGER,
    trail_trigger_price REAL,
    peak_price REAL,
    hold_days INTEGER,
    exit_reason TEXT,              -- stop_hit / trail_hit / time_exit
    gross_pnl REAL,
    net_pnl REAL,
    entry_commission REAL,
    exit_commission REAL,
    r_multiple REAL                -- net_pnl / (risk_per_share * shares)
);

-- Daily equity curve
CREATE TABLE IF NOT EXISTS wf_equity_curve (
    run_id TEXT,
    date TEXT,
    portfolio_value REAL,
    open_positions INTEGER,
    open_risk_pct REAL,
    PRIMARY KEY (run_id, date)
);
```

---

## 10. Summary calculation (summary.py)

```python
def calculate_summary(run_id: str, db_conn) -> dict:
    """Calculate aggregate statistics from wf_positions and wf_signals for a run."""
    
    # Query closed positions
    positions = db_conn.execute(
        'SELECT * FROM wf_positions WHERE run_id = ?', (run_id,)
    ).fetchall()
    
    if not positions:
        return {'total_trades': 0}
    
    r_multiples = [p['r_multiple'] for p in positions]
    pnls = [p['net_pnl'] for p in positions]
    wins = [r for r in r_multiples if r > 0]
    losses = [r for r in r_multiples if r <= 0]
    
    win_rate = len(wins) / len(r_multiples)
    avg_win_r = sum(wins) / len(wins) if wins else 0
    avg_loss_r = abs(sum(losses) / len(losses)) if losses else 0
    expectancy = (win_rate * avg_win_r) - ((1 - win_rate) * avg_loss_r)
    
    # Max consecutive losses
    max_consec = current_consec = 0
    for r in r_multiples:
        if r <= 0:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0
    
    # Max drawdown from equity curve
    equity = db_conn.execute(
        'SELECT portfolio_value FROM wf_equity_curve WHERE run_id = ? ORDER BY date',
        (run_id,)
    ).fetchall()
    values = [row['portfolio_value'] for row in equity]
    max_dd = 0
    peak = values[0]
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        max_dd = max(max_dd, dd)
    
    # Signal conversion rate
    signals = db_conn.execute(
        'SELECT outcome, COUNT(*) as n FROM wf_signals WHERE run_id = ? GROUP BY outcome',
        (run_id,)
    ).fetchall()
    outcome_counts = {row['outcome']: row['n'] for row in signals}
    total_signals = sum(outcome_counts.values())
    filled = outcome_counts.get('filled', 0)
    gap_rejected = outcome_counts.get('rejected_gap', 0)
    
    # Exit reason breakdown
    exits = db_conn.execute(
        'SELECT exit_reason, COUNT(*) as n FROM wf_positions WHERE run_id = ? GROUP BY exit_reason',
        (run_id,)
    ).fetchall()
    exit_breakdown = {row['exit_reason']: row['n'] for row in exits}
    
    # Strategy breakdown
    strat_a = [p for p in positions if p['signal_type'] in ('pullback', 'pullback+breakout')]
    strat_b = [p for p in positions if p['signal_type'] in ('breakout', 'pullback+breakout')]
    
    return {
        'total_trades': len(positions),
        'win_rate': round(win_rate, 4),
        'avg_r_multiple': round(sum(r_multiples) / len(r_multiples), 3),
        'expectancy': round(expectancy, 3),
        'max_consecutive_losses': max_consec,
        'max_drawdown_pct': round(max_dd * 100, 2),
        'exit_breakdown': exit_breakdown,
        'strategy_a_trades': len(strat_a),
        'strategy_a_avg_r': round(sum(p['r_multiple'] for p in strat_a) / len(strat_a), 3) if strat_a else None,
        'strategy_b_trades': len(strat_b),
        'strategy_b_avg_r': round(sum(p['r_multiple'] for p in strat_b) / len(strat_b), 3) if strat_b else None,
        'signal_conversion_rate': round(filled / total_signals, 4) if total_signals else 0,
        'gap_rejection_rate': round(gap_rejected / total_signals, 4) if total_signals else 0,
        'avg_hold_days': round(sum(p['hold_days'] for p in positions) / len(positions), 1),
    }
```

---

## 11. CLI entry point (__main__.py)

```python
"""
Walk-Forward Simulator CLI

Usage:
    python -m walk_forward_simulator
    python -m walk_forward_simulator --start 2025-01-01 --end 2026-01-01
    python -m walk_forward_simulator --portfolio 100000
    python -m walk_forward_simulator --dry-run
    python -m walk_forward_simulator --summary <run_id>
    python -m walk_forward_simulator --list-runs
    python -m walk_forward_simulator --config /path/to/config.yaml
"""

import argparse
import uuid
from datetime import date
import yaml

from .runner import WalkForwardRunner
from .storage import list_runs, print_summary

def main():
    parser = argparse.ArgumentParser(description='Walk-Forward Strategy Simulator')
    parser.add_argument('--start', type=date.fromisoformat, default=None)
    parser.add_argument('--end', type=date.fromisoformat, default=date.today())
    parser.add_argument('--portfolio', type=float, default=100_000.0)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--summary', type=str, metavar='RUN_ID')
    parser.add_argument('--list-runs', action='store_true')
    parser.add_argument('--config', type=str, default='config.yaml')
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = config.get('walk_forward', {}).get('db_path', './data/wf_sim.db')

    if args.list_runs:
        list_runs(db_path)
        return

    if args.summary:
        print_summary(args.summary, db_path)
        return

    run_id = str(uuid.uuid4())
    runner = WalkForwardRunner(
        config=config,
        run_id=run_id,
        start_date=args.start,
        end_date=args.end,
        starting_portfolio_value=args.portfolio,
        db_path=db_path,
        dry_run=args.dry_run,
    )
    runner.run()

if __name__ == '__main__':
    main()
```

---

## 12. Config additions

Add to `config.yaml`:

```yaml
walk_forward:
  db_path: './data/wf_sim.db'
  # start_date and end_date are CLI args, not config — keep them explicit per run

# Update existing:
data_fetcher:
  history_days: 700    # was 300 — extended to support WF warmup + 1 year simulation
```

---

## 13. Build order

Build and test in this sequence. Each step is independently verifiable before moving to the next.

**Step 1 — pm_math.py**
Extract pure functions from live PM. Write unit tests for each function with known inputs
and expected outputs. No dependencies on other new code.

**Step 2 — Signal Engine constructor injection**
Make the one-line change to engine.py. Run existing signal engine tests to confirm
nothing broke.

**Step 3 — DataFrameWalker**
Implement walker.py. Write the unit test: known ticker, mid-history cutoff date,
assert last index equals cutoff. Verify None passthrough works.

**Step 4 — Storage layer**
Implement storage.py schema creation and all insert/update functions. Test by creating
an in-memory SQLite, inserting a row, reading it back.

**Step 5 — Signal ranking**
Implement ranking.py. Write a unit test with a mixed list of signals and assert the
output order.

**Step 6 — SimPositionManager**
Implement sim_pm.py. Write unit tests for each exit condition in isolation:
- Stop hit on gap open
- Stop hit intraday
- Trail trigger activation
- Trail update (new high)
- Time exit (all three conditions met)
- Time exit suppressed (trail active)
- Time exit suppressed (no signal queue)

**Step 7 — WalkForwardRunner**
Implement runner.py. At this point all dependencies exist. Run a short simulation
(e.g. --start one month ago --end today) and inspect wf_sim.db manually.

**Step 8 — Summary and CLI**
Implement summary.py and __main__.py. Run a full simulation and verify the printed
output matches what you see in the database.

**Step 9 — Full history refresh**
Run `python -m data_fetcher --full-refresh` to extend the cache to 700 days.
Then run a full simulation over the available date range.

---

## 14. Known limitations to document in module README

- Entry slippage not modelled — all entries fill at D-1 close price
- Intraday signals not simulated — EOD only
- Transaction costs use flat TOB estimate (0.35%), not actual IBKR commission schedule
- Market impact of simulated trades not modelled
- EMA200 warmup reduces effective simulation window by ~200 bars (~10 months)
- time_exit uses running_high as proxy for current price in stop proximity check —
  this is a simplification; a more accurate implementation would use bar_D.close
