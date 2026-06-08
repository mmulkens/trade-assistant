# ---------------------------------------------------------------------------
# engine.py — Signal Engine orchestrator
#
# Responsibilities:
#   - Load the benchmark and determine the market regime
#   - For each ticker: compute shared indicators once (prepare), then slice
#     to the scan date and call each active strategy (scan)
#   - Assemble the Signal dataclass from strategy results + stop/target math
#   - Log every fired signal and every skip with a reason code
#
# Two-phase design:
#   prepare(tickers)             — loads full OHLCV history and pre-computes
#                                  all required indicators once; returns a dict
#   scan(prepared, as_of_date)   — slices to as_of_date and applies strategy
#                                  logic; no indicator recomputation
#
# This split allows the Walk-Forward runner to call prepare() once before its
# day loop and scan() cheaply on each iteration, reducing indicator cost from
# O(days × tickers) to O(tickers).
#
# Strategy logic lives in separate modules:
#   strategy_a.py — EMA Pullback
#   strategy_b.py — 50-Day Breakout
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from datetime import datetime, timezone
from logging import Logger
from typing import Any, Optional

import pandas as pd

from data_fetcher import cache as cache_store
from . import indicators as ind
from . import ranking as rk
from .strategy_a import StrategyA
from .strategy_b import StrategyB


# ---------------------------------------------------------------------------
# Strategy registry
#
# Maps config names to strategy classes. Adding a new strategy requires:
#   1. Create strategy_c.py with required_indicators and evaluate()
#   2. Import and register it here
#   3. Add 'strategy_c' to active_strategies in config.yaml
# No other files need to change.
# ---------------------------------------------------------------------------

_STRATEGY_MODULES: dict[str, type] = {
    "strategy_a": StrategyA,
    "strategy_b": StrategyB,
}


# ---------------------------------------------------------------------------
# Signal — the output contract
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """Structured trade signal emitted when a setup passes all conditions.

    Every field must be populated before the Risk Layer will accept the signal.
    See Section 13A of trade_assistant_design.md for the full specification.

    IBKR-dependent fields (instrument_id, earnings_flag) are stubbed for Phase 1:
    instrument_id mirrors the ticker string, earnings_flag is always None until
    reqFundamentalData is wired up.
    """

    # --- Core contract fields (required by Risk Layer / Order Executor) ---
    instrument_id: str          # IBKR conid — stubbed as ticker until IBKR integration
    ticker: str                 # Yahoo Finance ticker, e.g. "ASML.AS"
    direction: str              # Always 'long' — shorting out of scope for v1
    entry_price: float          # Last close; the intended entry level
    stop_price: float           # Technical stop — fixed at signal time, not trailing
    target_price: float         # Minimum 2:1 R:R target (entry + 2 × risk)
    signal_type: str            # 'pullback' | 'breakout' | 'pullback+breakout'
    liquidity_class: str        # 'liquid' | 'thin' — affects order type downstream
    conviction: str             # 'standard' | 'elevated'
    signal_timestamp: datetime  # UTC timestamp of signal generation
    earnings_flag: Optional[bool]   # True = binary event within N days; None = stub

    # --- Stop quality flag and component breakdown ---
    stop_capped: bool           # True when stop_hard_cap_pct was applied
    swing_low_stop: float       # Structural stop: min low of last swing_low_period bars
    atr_stop: float             # ATR floor: entry − stop_atr_multiplier × ATR14
    stop_method: str            # Which component set the final stop: 'swing_low' | 'atr_floor' | 'hard_cap'

    # --- Annotation fields (logged and stored, not part of the Risk Layer contract) ---
    strategy_a_fired: bool      # Did EMA Pullback fire?
    strategy_b_fired: bool      # Did Breakout fire?
    near_52wk_high: bool        # Price within near_52wk_high_pct% of 52-week high?
    market_regime: str          # 'bull' | 'bear' | 'unknown' at scan time
    rs_value: Optional[float]   # Stock / benchmark ratio at signal time
    run_type: str = 'eod'       # 'eod' | 'intraday' — execution context for the signal
    signal_rank: int = 0        # 1 = highest priority; assigned by scan() after ranking


# ---------------------------------------------------------------------------
# SignalEngine
# ---------------------------------------------------------------------------

class SignalEngine:
    """Orchestrates the daily scan across the full watchlist.

    Supports two calling patterns:

    Live mode (unchanged external behaviour):
        signals = engine.scan(tickers)
        Calls prepare() internally and scans as of today.

    Walk-Forward mode (performance-optimised):
        prepared = engine.prepare(tickers, benchmark_df=benchmark_df)
        # ... then in a day loop:
        signals = engine.scan(prepared, as_of_date=sim_date)
        Indicator computation runs once; each loop iteration is a cheap slice.
    """

    def __init__(self, config: dict, logger: Logger, cache=cache_store) -> None:
        self._logger = logger
        self._cache = cache        # walker in WF mode; real cache in live mode
        se = config["signal_engine"]

        # General trend filter parameters (applied before any strategy is evaluated)
        # Guard A: EMA chain — EMA21 > EMA50 > EMA100 > EMA200
        # Guard B: freefall rejection — price must not be >X% below its N-day high
        self._drawdown_guard_pct: float = se.get("trend_drawdown_guard_pct", 30.0)
        self._drawdown_period: int = se.get("trend_drawdown_period", 20)

        # Stop placement parameters
        self._swing_low_period: int = se.get("swing_low_period", 10)
        self._stop_atr_mult: float = se.get("stop_atr_multiplier", 1.5)
        self._stop_hard_cap_pct: float = se.get("stop_hard_cap_pct", 8.0)

        # Target and conviction parameters
        self._min_rr: float = se.get("min_rr_ratio", 2.0)
        self._near_52wk_pct: float = se["near_52wk_high_pct"]

        # Liquidity classification
        self._liq_min_turnover: float = se.get("liquidity_min_turnover", 1_000_000)
        self._liq_avg_days: int = se.get("liquidity_avg_days", 20)

        # Market regime benchmark
        self._benchmark: str = se["benchmark"]
        self._regime_filter: bool = bool(se.get("regime_filter", True))
        self._cache_dir: str = config["data_fetcher"]["cache_dir"]

        # --- Strategy registry ---
        # Active strategies are declared in config; unknown names are warned and skipped.
        # Each entry is (registry_name, strategy_instance) so _scan_one() can map
        # results back to the Signal annotation fields (strategy_a_fired, strategy_b_fired).
        active_names: list[str] = se.get("active_strategies", ["strategy_a", "strategy_b"])
        self._strategies: list[tuple[str, Any]] = []
        for name in active_names:
            if name in _STRATEGY_MODULES:
                self._strategies.append((name, _STRATEGY_MODULES[name](config)))
            else:
                self._logger.warning({
                    "event": "unknown_strategy",
                    "name": name,
                    "available": sorted(_STRATEGY_MODULES.keys()),
                })

        # Minimum bars required before any indicator or strategy produces a reliable result
        strat_min = max((s.min_bars_required for _, s in self._strategies), default=0)
        macd_slow: int = se.get("macd_slow", 26)
        macd_signal_period: int = se.get("macd_signal_period", 9)
        self._min_bars: int = max(
            macd_slow + macd_signal_period + 10,   # MACD warm-up
            100 + 10,                               # EMA100 warm-up
            self._swing_low_period,
            strat_min,
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def prepare(
        self,
        tickers: list[str],
        benchmark_df: Optional[pd.DataFrame] = None,
    ) -> dict[str, dict]:
        """Pre-compute all required indicators for every ticker over full history.

        Always reads from the real Parquet cache (cache_store), never from
        self._cache (the walker). The walker is a scanning-time concern: its
        date bound enforces lookahead safety during scan(). prepare() answers
        "give me everything you have on this ticker" — the full dataset.
        Lookahead safety is enforced by the as_of_date slice in _scan_prepared().

        Returns a dict keyed by ticker. Each value is:
            'df':       full OHLCV DataFrame with indicator columns appended
            'ind_cols': list of indicator column names added to df

        Indicators are stored as extra columns on the df so that _scan_prepared()
        can slice the entire dataset (OHLCV + indicators) with a single index
        comparison instead of one per indicator series.

        Call once before a WF day loop and pass the result to scan().
        In live mode, scan(tickers) calls prepare() internally.
        """
        # Union of indicators required by all active strategies
        required: set[str] = set()
        for _, strategy in self._strategies:
            required.update(strategy.required_indicators)

        # RS line requires a benchmark; include it whenever one is provided
        if benchmark_df is not None:
            required.add("rs_line")

        prepared: dict[str, dict] = {}
        for ticker in tickers:
            # Read from real cache (full history, no date bound)
            df = cache_store.load(ticker, self._cache_dir)
            if df is None or len(df) < self._min_bars:
                continue

            # Copy so indicator columns don't contaminate the cache's own df object
            df = df.copy()
            ind_cols: list[str] = []
            for name in required:
                kwargs = {"benchmark_df": benchmark_df} if name == "rs_line" else {}
                try:
                    df[name] = ind.compute(name, df, **kwargs)
                    ind_cols.append(name)
                except KeyError:
                    self._logger.warning({
                        "event": "indicator_not_found",
                        "indicator": name,
                        "ticker": ticker,
                    })

            prepared[ticker] = {"df": df, "ind_cols": ind_cols}

        return prepared

    def scan(
        self,
        tickers_or_prepared,
        as_of_date=None,
    ) -> list[Signal]:
        """Scan tickers and return all qualifying signals.

        Live mode:   engine.scan(tickers: list[str])
                     Calls prepare() internally and scans as of today.

        WF mode:     engine.scan(prepared: dict, as_of_date=sim_date)
                     Slices pre-computed data to as_of_date; no recomputation.
                     as_of_date should be the last completed trading day (D-1),
                     not the execution day (D), to preserve lookahead safety.
        """
        if isinstance(tickers_or_prepared, list):
            # Live path: prepare + scan in one call (backward-compatible)
            benchmark_df = self._cache.load(self._benchmark, self._cache_dir)
            if benchmark_df is None or len(benchmark_df) < 200:
                self._logger.warning({
                    "event": "benchmark_missing_or_short",
                    "benchmark": self._benchmark,
                    "bars": len(benchmark_df) if benchmark_df is not None else 0,
                })
                regime = "unknown"
            else:
                regime = self._market_regime(benchmark_df)

            if self._regime_filter and regime == "bear":
                self._logger.info({
                    "event": "regime_filter_active",
                    "regime": "bear",
                    "tickers_skipped": len(tickers_or_prepared),
                })
                return []

            prepared = self.prepare(tickers_or_prepared, benchmark_df=benchmark_df)
            return self._scan_prepared(prepared, as_of_date=None, regime=regime, verbose=True)

        else:
            # WF path: prepared data supplied by the runner
            prepared = tickers_or_prepared

            # Regime check on benchmark slice up to as_of_date.
            # self._cache is the walker here — its current_date is D-1, so the
            # load already returns data bounded to the correct simulation date.
            benchmark_df = self._cache.load(self._benchmark, self._cache_dir)
            if benchmark_df is not None and as_of_date is not None:
                cutoff = pd.Timestamp(as_of_date)
                benchmark_slice = benchmark_df[benchmark_df.index <= cutoff]
            else:
                benchmark_slice = benchmark_df

            if benchmark_slice is None or len(benchmark_slice) == 0:
                regime = "unknown"
            else:
                regime = self._market_regime(benchmark_slice)

            if self._regime_filter and regime == "bear":
                return []

            # verbose=False: suppress per-ticker skip debug logs during the WF day
            # loop. With 500+ tickers/day over 1000+ days this would write hundreds
            # of thousands of JSON lines and dominate wall-clock time.
            return self._scan_prepared(prepared, as_of_date=as_of_date, regime=regime, verbose=False)

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _market_regime(self, df: pd.DataFrame) -> str:
        """Return 'bull' if benchmark close >= 200 EMA, else 'bear'."""
        ema200 = ind.ema(df["close"], 200)
        return "bull" if df["close"].iloc[-1] >= ema200.iloc[-1] else "bear"

    def _scan_prepared(
        self,
        prepared: dict[str, dict],
        as_of_date,
        regime: str,
        verbose: bool = True,
    ) -> list[Signal]:
        """Evaluate all tickers in the prepared dict.

        Slices each ticker's merged df (OHLCV + indicator columns) once per
        ticker to as_of_date, then extracts the indicators dict from columns.
        This costs one O(n) index comparison per ticker rather than one per
        indicator series — a ~10× reduction in slice operations for the WF loop.
        """
        cutoff = pd.Timestamp(as_of_date) if as_of_date is not None else None

        signals: list[Signal] = []
        for ticker, data in prepared.items():
            df = data["df"]
            ind_cols = data["ind_cols"]

            # Single slice covers OHLCV and all indicator columns simultaneously
            if cutoff is not None:
                df = df[df.index <= cutoff]

            if len(df) < self._min_bars:
                continue

            ind_dict = {col: df[col] for col in ind_cols}
            result = self._scan_one(ticker, df, ind_dict, regime, verbose=verbose)
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

    def _scan_one(
        self,
        ticker: str,
        df: pd.DataFrame,
        indicators: dict[str, pd.Series],
        regime: str,
        verbose: bool = True,
    ) -> Optional[Signal]:
        """Evaluate a single ticker against pre-computed, pre-sliced data.

        verbose=False suppresses per-ticker skip debug logs. Set by the WF path
        to avoid serialising hundreds of thousands of skip events to disk.
        """

        # --- General trend filters (guard all strategies) ---
        # Guard A: EMA chain must be in full bullish order.
        # Applies to breakouts too — a stock breaking out of a downtrend on a
        # single bar while EMAs are still bearish is not the setup we want.
        e21  = float(indicators["ema_21"].iloc[-1])
        e50  = float(indicators["ema_50"].iloc[-1])
        e100 = float(indicators["ema_100"].iloc[-1])
        e200 = float(indicators["ema_200"].iloc[-1])
        if not (e21 > e50 > e100 > e200):
            if verbose:
                self._logger.debug({
                    "event": "signal_skipped",
                    "ticker": ticker,
                    "reason": "trend_filter:ema_chain_not_aligned",
                })
            return None

        close = float(df["close"].iloc[-1])

        # Guard B: reject freefalls before lagging EMAs have adjusted.
        # A stock down >30% in 20 days can still show a bullish EMA stack
        # because the slower MAs have not yet caught up to the price collapse.
        recent_high = float(df["high"].iloc[-self._drawdown_period:].max())
        drawdown = (recent_high - close) / recent_high
        if drawdown > self._drawdown_guard_pct / 100:
            if verbose:
                self._logger.debug({
                    "event": "signal_skipped",
                    "ticker": ticker,
                    "reason": f"trend_filter:drawdown_too_large_{round(drawdown * 100, 1)}pct",
                })
            return None

        # --- Delegate to all active strategies ---
        # Collect per-strategy results so annotation fields (strategy_a_fired,
        # strategy_b_fired) can still be populated even with a dynamic registry.
        strategy_results: dict[str, tuple[bool, str]] = {}
        for name, strategy in self._strategies:
            fired, reason = strategy.evaluate(df, indicators)
            strategy_results[name] = (fired, reason)

        a_fired, a_reason = strategy_results.get("strategy_a", (False, "inactive"))
        b_fired, b_reason = strategy_results.get("strategy_b", (False, "inactive"))

        if not any(fired for fired, _ in strategy_results.values()):
            if verbose:
                self._logger.debug({
                    "event": "signal_skipped",
                    "ticker": ticker,
                    "strategy_a_reason": a_reason,
                    "strategy_b_reason": b_reason,
                })
            return None

        signal_type = (
            "pullback+breakout" if (a_fired and b_fired)
            else "pullback" if a_fired
            else "breakout"
        )

        # --- Stop price (3-step) ---
        # Step 1: structural stop — lowest low of the past swing_low_period bars.
        #         Placing the stop below the most recent swing low means the
        #         trade is invalidated only if price undercuts a level that was
        #         previously defended.
        entry     = close
        swing_low = float(df["low"].iloc[-self._swing_low_period:].min())

        # Step 2: ATR floor — entry minus stop_atr_multiplier × ATR14.
        #         If the swing low is closer to entry than 1.5×ATR, the stop
        #         is too tight and will be triggered by normal intraday noise.
        #         Taking the lower (wider) of the two gives the trade breathing room.
        atr_val      = float(indicators["atr_14"].iloc[-1])
        atr_stop_val = entry - self._stop_atr_mult * atr_val
        stop = min(swing_low, atr_stop_val)
        stop_method  = "swing_low" if swing_low <= atr_stop_val else "atr_floor"

        # Step 3: hard cap — never risk more than stop_hard_cap_pct of entry.
        #         Prevents extreme stops on volatile or thinly traded names.
        max_risk    = entry * (self._stop_hard_cap_pct / 100)
        stop_capped = (entry - stop) > max_risk
        if stop_capped:
            stop        = entry - max_risk
            stop_method = "hard_cap"
        stop = round(stop, 4)

        risk   = entry - stop
        target = round(entry + self._min_rr * risk, 4)

        # --- Conviction annotation ---
        # Elevated when both strategies fire simultaneously, or price is
        # within near_52wk_high_pct% of its 52-week high (leadership stock).
        bars_for_52wk = min(252, len(df))
        near_52wk  = close >= float(df["high"].iloc[-bars_for_52wk:].max()) * (
            1 - self._near_52wk_pct / 100
        )
        conviction = "elevated" if (a_fired and b_fired) or near_52wk else "standard"

        # --- Liquidity classification ---
        liquidity_class = self._classify_liquidity(df)

        # --- RS line (annotation — not a filter) ---
        rs_val: Optional[float] = None
        rs_series = indicators.get("rs_line")
        if rs_series is not None and len(rs_series) > 0:
            last_rs = rs_series.iloc[-1]
            rs_val = round(float(last_rs), 6) if not pd.isna(last_rs) else None

        signal = Signal(
            instrument_id=ticker,
            ticker=ticker,
            direction="long",
            entry_price=round(entry, 4),
            stop_price=stop,
            target_price=target,
            signal_type=signal_type,
            liquidity_class=liquidity_class,
            conviction=conviction,
            signal_timestamp=datetime.now(timezone.utc),
            earnings_flag=None,
            stop_capped=stop_capped,
            swing_low_stop=round(swing_low, 4),
            atr_stop=round(atr_stop_val, 4),
            stop_method=stop_method,
            strategy_a_fired=a_fired,
            strategy_b_fired=b_fired,
            near_52wk_high=near_52wk,
            market_regime=regime,
            rs_value=rs_val,
            run_type='eod',
        )

        macd_hist_series = indicators.get("macd_hist")
        self._logger.info({
            "event": "signal_fired",
            "ticker": ticker,
            "signal_type": signal_type,
            "conviction": conviction,
            "entry": signal.entry_price,
            "stop": signal.stop_price,
            "stop_method": stop_method,
            "swing_low_stop": signal.swing_low_stop,
            "atr_stop": signal.atr_stop,
            "target": signal.target_price,
            "risk_pct": round((entry - stop) / entry * 100, 2),
            "stop_capped": stop_capped,
            "liquidity_class": liquidity_class,
            "strategy_a": a_fired,
            "strategy_b": b_fired,
            "near_52wk_high": near_52wk,
            "rs_value": rs_val,
            "ema21": round(e21, 4),
            "macd_hist": round(float(macd_hist_series.iloc[-1]), 6) if macd_hist_series is not None else None,
            "atr": round(atr_val, 4),
        })
        return signal

    def _classify_liquidity(self, df: pd.DataFrame) -> str:
        """Return 'liquid' or 'thin' based on average daily price×volume turnover."""
        last_n = df.iloc[-self._liq_avg_days:]
        avg_turnover = float((last_n["close"] * last_n["volume"]).mean())
        return "thin" if avg_turnover < self._liq_min_turnover else "liquid"
