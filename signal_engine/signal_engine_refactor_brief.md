# Signal Engine Refactor — Coding Brief
# Trade Assistant · VS Code Implementation Handoff

*This document covers two tightly coupled changes:*
*1. Architectural refactor of the Signal Engine (IndicatorLibrary + strategy registry)*
*2. Walk-Forward Simulator performance fix (pre-computation before the day loop)*

*Read fully before writing any code. All design decisions are final unless explicitly marked open.*

---

## 1. Why this change is being made

### Problem: Walk-Forward Simulator is too slow

The WF runner currently calls `engine.scan(tickers)` on every simulation day.
Inside `scan()`, each call to `_scan_one()` loads OHLCV from the Walker and
recomputes every indicator (EMAs, MACD, ATR, RS) from scratch — for all 2,000
tickers — just to extend the window by one bar. For a 250-day simulation that
is ~500,000 full indicator recomputations. Runs take minutes.

**Fix:** split the Signal Engine into two explicit phases:
- `engine.prepare(tickers)` — computes all indicators once across the full
  history; returns an enriched dataset
- `engine.scan(prepared_data, as_of_date)` — slices to `as_of_date` and
  applies strategy logic; pure lookups, no recomputation

The WF runner calls `prepare()` once before the day loop. Each loop iteration
calls `scan()` with the pre-computed data and advances the date. Indicator
computation cost drops from O(days × tickers) to O(tickers).

### Problem: the SE will not scale cleanly to new strategies

Currently `engine.py` hard-codes indicator computation (EMAs, MACD, ATR) and
instantiates `StrategyA` and `StrategyB` directly. Adding Strategy C requires
editing the engine itself, risks breaking existing strategies, and creates no
clear pattern for future additions.

**Fix:** introduce two supporting structures:
- `IndicatorLibrary` — a registry of named, pure indicator functions
- Strategy registry — active strategies declared in config; engine discovers
  and loads them dynamically

Each strategy declares which indicators it needs. The engine computes the
union once and passes each strategy only what it requires.

---

## 2. New file structure

```
signal_engine/
├── __main__.py             unchanged
├── engine.py               refactored — orchestrator only; no hard-coded indicators
├── indicators.py           extended — becomes the IndicatorLibrary registry
├── strategy_a.py           updated — declares required indicators; evaluate() receives them
├── strategy_b.py           updated — declares required indicators; evaluate() receives them
├── ranking.py              unchanged
├── db.py                   unchanged
└── signal_engine.md        update file structure section after implementation
```

No new files required. The changes are contained to `engine.py`,
`indicators.py`, `strategy_a.py`, and `strategy_b.py`.

---

## 3. IndicatorLibrary (indicators.py)

Extend the existing `indicators.py` with a named registry. All existing
indicator functions (`ema`, `macd`, `atr`, `rs_line`) stay exactly as they
are. Add a `REGISTRY` dict that maps canonical string names to callables.

```python
# indicators.py — add at the bottom, after existing functions

from typing import Callable
import pandas as pd

# Each entry: canonical_name → callable(df: pd.DataFrame, **kwargs) → pd.Series
# The engine calls these by name, passing the full OHLCV DataFrame.
# kwargs come from the strategy's indicator_requirements declaration.

REGISTRY: dict[str, Callable] = {
    "ema_21":        lambda df, **_: ema(df["close"], 21),
    "ema_50":        lambda df, **_: ema(df["close"], 50),
    "ema_100":       lambda df, **_: ema(df["close"], 100),
    "ema_200":       lambda df, **_: ema(df["close"], 200),
    "macd_line":     lambda df, **_: macd(df["close"], 12, 26, 9)[0],
    "macd_signal":   lambda df, **_: macd(df["close"], 12, 26, 9)[1],
    "macd_hist":     lambda df, **_: macd(df["close"], 12, 26, 9)[2],
    "atr_14":        lambda df, **_: atr(df, 14),
    "rs_line":       lambda df, benchmark_df=None, **_: (
                         rs_line(df["close"], benchmark_df["close"])
                         if benchmark_df is not None else pd.Series(dtype=float)
                     ),
    "high_50d":      lambda df, **_: df["high"].rolling(50).max(),
    "high_52wk":     lambda df, **_: df["high"].rolling(252).max(),
    "vol_20d_avg":   lambda df, **_: df["volume"].rolling(20).mean(),
}


def compute(name: str, df: pd.DataFrame, **kwargs) -> pd.Series:
    """Compute a named indicator. Raises KeyError if name not in REGISTRY."""
    if name not in REGISTRY:
        raise KeyError(f"Indicator '{name}' not found in REGISTRY. "
                       f"Available: {sorted(REGISTRY.keys())}")
    return REGISTRY[name](df, **kwargs)
```

**Rules for registry entries:**
- Every function takes a full OHLCV DataFrame as its first argument
- Returns a `pd.Series` aligned to the DataFrame index
- `**kwargs` allows passing extra context (e.g. `benchmark_df` for RS line)
- MACD returns three series; the registry exposes them as three separate named
  indicators to avoid the engine needing to unpack tuples

---

## 4. Strategy interface changes (strategy_a.py, strategy_b.py)

Each strategy must now declare which indicators it needs and accept them as a
pre-computed dict rather than computing them internally.

### New interface contract

```python
class StrategyA:

    # --- NEW: indicator declaration ---
    required_indicators: list[str] = [
        "ema_21", "ema_50", "ema_100", "ema_200",
        "macd_hist",
        "atr_14",
    ]

    def __init__(self, config: dict) -> None:
        # unchanged — reads own config params as before
        ...

    def evaluate(
        self,
        df: pd.DataFrame,           # OHLCV slice (already truncated to as_of_date)
        indicators: dict[str, pd.Series],  # pre-computed; keys match required_indicators
    ) -> bool:
        """Return True if setup conditions are met on the last bar of df."""
        # Access indicators by name:
        #   ema21  = indicators["ema_21"]
        #   hist   = indicators["macd_hist"]
        # Do NOT compute indicators here. Trust what was passed in.
        ...
```

Apply the same pattern to `StrategyB`:

```python
class StrategyB:

    required_indicators: list[str] = [
        "ema_21", "ema_50", "ema_100", "ema_200",
        "macd_line",
        "atr_14",
        "high_50d",
        "vol_20d_avg",
    ]

    def evaluate(
        self,
        df: pd.DataFrame,
        indicators: dict[str, pd.Series],
    ) -> bool:
        ...
```

**Important:** `evaluate()` receives the already-sliced DataFrame and the
already-sliced indicator Series (both truncated to `as_of_date` by the
engine before the call). Strategies never slice themselves — that is the
engine's responsibility.

---

## 5. SignalEngine refactor (engine.py)

### 5a. Strategy registry

Replace direct instantiation of `StrategyA` and `StrategyB` with config-
driven loading.

```python
# config.yaml — new key under signal_engine:
# active_strategies: [strategy_a, strategy_b]   ← default

# engine.py __init__ — replace:
#   self._strat_a = StrategyA(config)
#   self._strat_b = StrategyB(config)
# with:

from signal_engine import strategy_a as _mod_a
from signal_engine import strategy_b as _mod_b

_STRATEGY_MODULES = {
    "strategy_a": _mod_a.StrategyA,
    "strategy_b": _mod_b.StrategyB,
    # strategy_c will be added here when built
}

# In __init__:
active_names = se.get("active_strategies", ["strategy_a", "strategy_b"])
self._strategies: list = [
    _STRATEGY_MODULES[name](config)
    for name in active_names
    if name in _STRATEGY_MODULES
]
```

Unknown strategy names should log a warning and be skipped — not crash.

### 5b. prepare() method

```python
def prepare(
    self,
    tickers: list[str],
    benchmark_df: Optional[pd.DataFrame] = None,
) -> dict[str, dict]:
    """Pre-compute all indicators for every ticker over full history.

    Returns a dict keyed by ticker. Each value is a dict with:
        'df':         the full OHLCV DataFrame
        'indicators': dict[indicator_name → pd.Series] (full history, no slice)

    Call this once before a WF day loop. Pass the result to scan().
    In live mode, scan(tickers) calls prepare() internally — no external change.
    """
    # Collect the union of required indicators across all active strategies
    required = set()
    for strategy in self._strategies:
        required.update(strategy.required_indicators)

    # Always include rs_line if benchmark is available
    if benchmark_df is not None:
        required.add("rs_line")

    prepared = {}
    for ticker in tickers:
        df = self._cache.load(ticker, self._cache_dir)
        if df is None or len(df) < self._min_bars:
            continue  # skip; scan() will handle missing tickers gracefully

        ind_dict = {}
        for name in required:
            kwargs = {"benchmark_df": benchmark_df} if name == "rs_line" else {}
            try:
                ind_dict[name] = indicators.compute(name, df, **kwargs)
            except KeyError:
                self._logger.warning({
                    "event": "indicator_not_found",
                    "indicator": name,
                    "ticker": ticker,
                })

        prepared[ticker] = {"df": df, "indicators": ind_dict}

    return prepared
```

### 5c. scan() — two signatures

The public `scan()` method supports both calling patterns:

```python
def scan(
    self,
    tickers_or_prepared,           # list[str] OR dict (from prepare())
    as_of_date: Optional[date] = None,
) -> list[Signal]:
    """
    Live mode:   engine.scan(tickers)
                 → calls prepare() internally, scans as of today

    WF mode:     engine.scan(prepared_data, as_of_date=sim_date)
                 → slices pre-computed data to as_of_date, no recomputation
    """
    if isinstance(tickers_or_prepared, list):
        # Live path: prepare + scan in one call (unchanged external behaviour)
        benchmark_df = self._cache.load(self._benchmark, self._cache_dir)
        regime = self._market_regime(benchmark_df) if benchmark_df is not None else "unknown"
        if self._regime_filter and regime == "bear":
            self._logger.info({"event": "regime_filter_active", "regime": "bear"})
            return []
        prepared = self.prepare(tickers_or_prepared, benchmark_df=benchmark_df)
        return self._scan_prepared(prepared, as_of_date=None, regime=regime)

    else:
        # WF path: prepared_data already passed in
        prepared = tickers_or_prepared
        # Regime check uses benchmark slice up to as_of_date
        benchmark_df = self._cache.load(self._benchmark, self._cache_dir)
        if benchmark_df is not None and as_of_date is not None:
            cutoff = pd.Timestamp(as_of_date)
            benchmark_slice = benchmark_df[benchmark_df.index <= cutoff]
        else:
            benchmark_slice = benchmark_df
        regime = self._market_regime(benchmark_slice) if benchmark_slice is not None else "unknown"
        if self._regime_filter and regime == "bear":
            return []
        return self._scan_prepared(prepared, as_of_date=as_of_date, regime=regime)
```

### 5d. _scan_prepared() — internal implementation

```python
def _scan_prepared(
    self,
    prepared: dict[str, dict],
    as_of_date: Optional[date],
    regime: str,
) -> list[Signal]:
    """Evaluate all tickers in prepared dict. Slice to as_of_date if provided."""
    signals = []
    for ticker, data in prepared.items():
        df = data["df"]
        ind_dict = data["indicators"]

        # Slice to as_of_date (WF path); use full data if None (live path)
        if as_of_date is not None:
            cutoff = pd.Timestamp(as_of_date)
            df = df[df.index <= cutoff]
            ind_dict = {k: v[v.index <= cutoff] for k, v in ind_dict.items()}

        if len(df) < self._min_bars:
            continue

        result = self._scan_one(ticker, df, ind_dict, regime)
        if result is not None:
            signals.append(result)

    ranked = rk.rank_signals(signals)
    for i, sig in enumerate(ranked):
        sig.signal_rank = i + 1

    self._logger.info({
        "event": "scan_complete",
        "tickers_scanned": len(prepared),
        "signals_fired": len(ranked),
        "regime": regime,
    })
    return ranked
```

### 5e. _scan_one() updated signature

```python
def _scan_one(
    self,
    ticker: str,
    df: pd.DataFrame,               # already sliced
    indicators: dict[str, pd.Series],  # already sliced
    regime: str,
) -> Optional[Signal]:
    """Evaluate a single ticker against pre-computed, pre-sliced data."""
    # Trend filters still live here — they use indicator values, not raw OHLCV
    # Read from indicators dict:
    #   e21  = float(indicators["ema_21"].iloc[-1])
    #   e50  = float(indicators["ema_50"].iloc[-1])
    #   ...
    # Then call each active strategy:
    for strategy in self._strategies:
        fired = strategy.evaluate(df, indicators)
        ...
```

Remove all direct calls to `ind.ema(...)`, `ind.macd(...)`, `ind.atr(...)` from
`_scan_one()`. All indicator values now come from the pre-computed `indicators` dict.

---

## 6. Walk-Forward Runner changes (runner.py)

The runner must call `engine.prepare()` once before the day loop and pass the
result into `engine.scan()` on each iteration.

### Before the day loop

```python
# After warmup skip logic, before the day loop:

# Pre-load benchmark for regime checks and RS computation
benchmark_df = real_cache.load(config['signal_engine']['benchmark'], cache_dir)

# Pre-compute all indicators for all tickers — this is the performance fix
self._logger.info({"event": "wf_preparing_indicators", "tickers": len(tickers)})
prepared_data = engine.prepare(tickers, benchmark_df=benchmark_df)
self._logger.info({
    "event": "wf_indicators_ready",
    "tickers_prepared": len(prepared_data),
})
```

### Inside the day loop

```python
for sim_date in trading_days_between(effective_start, end_date):

    # Step 1: scan using pre-computed data, sliced to sim_date
    signals = engine.scan(prepared_data, as_of_date=sim_date)

    # Steps 2–5 unchanged (RL evaluation, SimPM, equity curve)
    ...
```

The Walker is no longer needed for indicator computation. It is still used for
any other cache reads that happen inside the loop (e.g. SimPM loading OHLCV for
a specific bar). The Walker's `current_date` should still be updated each
iteration as before.

> **Note on the Walker:** with this refactor, the Walker's primary
> lookahead-prevention role shifts from indicator computation (now handled by
> the `as_of_date` slice in `_scan_prepared`) to raw OHLCV reads by SimPM.
> Both mechanisms enforce the same guarantee: no future data is visible.

---

## 7. config.yaml changes

Add one new key under `signal_engine`:

```yaml
signal_engine:
  active_strategies: [strategy_a, strategy_b]   # default; override per run or WF CLI flag
  # ... all existing keys unchanged ...
```

The WF CLI should support `--strategies strategy_a` (or comma-separated list)
to override `active_strategies` for a specific run. This allows isolated
per-strategy backtests without editing config.

---

## 8. What does NOT change

- `Signal` dataclass — no field changes
- `ranking.py` — unchanged
- `db.py` — unchanged
- `risk_layer/` — unchanged
- `walk_forward/sim_pm.py` — unchanged
- `walk_forward/storage.py` — unchanged
- All CLI entry points — `engine.scan(tickers)` still works identically from
  `__main__.py`; the refactor is backward-compatible

---

## 9. Implementation order

1. Extend `indicators.py` with REGISTRY and `compute()` function
2. Update `strategy_a.py` — add `required_indicators`, update `evaluate()` signature
3. Update `strategy_b.py` — same pattern
4. Refactor `engine.py` — strategy registry, `prepare()`, updated `scan()`,
   `_scan_prepared()`, updated `_scan_one()`
5. Update `runner.py` — call `prepare()` before loop, pass to `scan()`
6. Add `active_strategies` to `config.yaml`
7. Smoke test: `python -m signal_engine` should produce identical output to
   pre-refactor (same signals, same ranks)
8. WF smoke test: run a short date range and verify it completes significantly
   faster than before

---

## 10. Definition of done

- [ ] `python -m signal_engine` runs cleanly and produces the same signals as
      before the refactor
- [ ] `python -m walk_forward` completes a 3-month run without error
- [ ] WF run time is materially faster than before (target: under 30 seconds
      for a 250-day simulation over 500 tickers)
- [ ] A new strategy can be added by: (a) creating `strategy_c.py` with
      `required_indicators` and `evaluate()`, (b) registering it in
      `_STRATEGY_MODULES` in engine.py, and (c) adding `strategy_c` to
      `active_strategies` in config — no other files change
- [ ] No indicator computation happens inside any `evaluate()` method
- [ ] No indicator computation happens inside the WF day loop
