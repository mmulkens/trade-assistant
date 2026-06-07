# ---------------------------------------------------------------------------
# engine.py — Signal Engine orchestrator
#
# Responsibilities:
#   - Load the benchmark and determine the market regime
#   - For each ticker: load cache, compute shared indicators, call strategies
#   - Assemble the Signal dataclass from strategy results + stop/target math
#   - Log every fired signal and every skip with a reason code
#
# Strategy logic lives in separate modules:
#   strategy_a.py — EMA Pullback
#   strategy_b.py — 50-Day Breakout
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from datetime import datetime, timezone
from logging import Logger
from typing import Optional

import pandas as pd

from data_fetcher import cache as cache_store
from . import indicators as ind
from . import ranking as rk
from .strategy_a import StrategyA
from .strategy_b import StrategyB


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

    Loads shared indicator data, delegates to StrategyA and StrategyB, then
    assembles and persists Signal objects for every qualifying setup.

    Flow per ticker:
        1. Load Parquet cache
        2. Compute shared indicators (EMAs, MACD, ATR, RS)
        3. Call StrategyA.evaluate() and StrategyB.evaluate()
        4. If either fires: calculate stop/target, annotate, return Signal
        5. Log fired signal or skip reason

    OR logic: a signal fires if Strategy A OR Strategy B passes.  Both firing
    simultaneously is rare (a pullback and a breakout are nearly mutually
    exclusive) and triggers an elevated conviction annotation.
    """

    def __init__(self, config: dict, logger: Logger, cache=cache_store) -> None:
        self._logger = logger
        self._cache = cache
        se = config["signal_engine"]

        # Shared indicator parameters — used to compute series passed to both strategies
        self._ema_period: int = se["ema_period"]                        # 21 — EMA for pullback anchor
        self._macd_fast: int = se.get("macd_fast", 12)
        self._macd_slow: int = se.get("macd_slow", 26)
        self._macd_signal_period: int = se.get("macd_signal_period", 9)
        self._atr_period: int = se.get("atr_period", 14)

        # General trend filters (applied before any strategy is evaluated)
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

        # Instantiate strategy modules
        self._strat_a = StrategyA(config)
        self._strat_b = StrategyB(config)

        # Minimum bars required before any indicator or strategy produces a reliable result
        self._min_bars: int = max(
            self._macd_slow + self._macd_signal_period + 10,   # MACD warm-up
            100 + 10,                                           # EMA100 warm-up
            self._swing_low_period,
            self._strat_a.min_bars_required,
            self._strat_b.min_bars_required,
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def scan(self, tickers: list[str]) -> list[Signal]:
        """Scan a list of tickers and return all qualifying signals.

        Returns an empty list if the market regime is bear.  Each ticker is
        evaluated independently.  Returned signals are ranked: elevated
        conviction first, then tightest stop% within tier (signal_rank=1
        is highest priority).
        """
        # Determine market regime before scanning any individual ticker.
        # The benchmark DataFrame is also reused for RS line annotations.
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

        # Bear regime gate: individual setups have much lower follow-through when
        # the broad index is below its 200 EMA — pause all signals.
        # Disabled when signal_engine.regime_filter: false (e.g. for WF experiments).
        if self._regime_filter and regime == "bear":
            self._logger.info({
                "event": "regime_filter_active",
                "regime": "bear",
                "tickers_skipped": len(tickers),
            })
            return []

        signals: list[Signal] = []
        for ticker in tickers:
            result = self._scan_one(ticker, benchmark_df, regime)
            if result is not None:
                signals.append(result)

        ranked = rk.rank_signals(signals)
        for i, signal in enumerate(ranked):
            signal.signal_rank = i + 1

        self._logger.info({
            "event": "scan_complete",
            "tickers_scanned": len(tickers),
            "signals_fired": len(ranked),
            "regime": regime,
        })
        return ranked

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _market_regime(self, df: pd.DataFrame) -> str:
        """Return 'bull' if benchmark close >= 200 EMA, else 'bear'."""
        ema200 = ind.ema(df["close"], 200)
        return "bull" if df["close"].iloc[-1] >= ema200.iloc[-1] else "bear"

    def _scan_one(
        self,
        ticker: str,
        benchmark_df: Optional[pd.DataFrame],
        regime: str,
    ) -> Optional[Signal]:
        """Evaluate a single ticker. Returns a Signal or None."""
        df = self._cache.load(ticker, self._cache_dir)
        if df is None or len(df) < self._min_bars:
            self._logger.debug({
                "event": "signal_skipped",
                "ticker": ticker,
                "reason": "insufficient_data",
                "bars": len(df) if df is not None else 0,
                "required": self._min_bars,
            })
            return None

        # --- Shared indicators (computed once, passed to both strategies) ---
        ema21  = ind.ema(df["close"], self._ema_period)
        ema50  = ind.ema(df["close"], 50)
        ema100 = ind.ema(df["close"], 100)
        ema200 = ind.ema(df["close"], 200)
        macd_line, _, histogram = ind.macd(
            df["close"], self._macd_fast, self._macd_slow, self._macd_signal_period
        )
        atr_series = ind.atr(df, self._atr_period)

        close = float(df["close"].iloc[-1])

        # --- General trend filters (guard both strategies) ---
        # Guard A: EMA chain must be in full bullish order.
        # Applies to breakouts too — a stock breaking out of a downtrend on a
        # single bar while EMAs are still bearish is not the setup we want.
        e21  = float(ema21.iloc[-1])
        e50  = float(ema50.iloc[-1])
        e100 = float(ema100.iloc[-1])
        e200 = float(ema200.iloc[-1])
        if not (e21 > e50 > e100 > e200):
            self._logger.debug({
                "event": "signal_skipped",
                "ticker": ticker,
                "reason": "trend_filter:ema_chain_not_aligned",
            })
            return None

        # Guard B: reject freefalls before lagging EMAs have adjusted.
        # A stock down >30% in 20 days can still show a bullish EMA stack
        # because the slower MAs have not yet caught up to the price collapse.
        recent_high = float(df["high"].iloc[-self._drawdown_period:].max())
        drawdown = (recent_high - close) / recent_high
        if drawdown > self._drawdown_guard_pct / 100:
            self._logger.debug({
                "event": "signal_skipped",
                "ticker": ticker,
                "reason": f"trend_filter:drawdown_too_large_{round(drawdown * 100, 1)}pct",
            })
            return None

        # --- Delegate to strategy modules ---
        a_fired, a_reason = self._strat_a.evaluate(df, close, ema21, ema50, ema100, ema200, histogram)
        b_fired, b_reason = self._strat_b.evaluate(df, close, macd_line)

        if not a_fired and not b_fired:
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
        atr_val      = float(atr_series.iloc[-1])
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
        if benchmark_df is not None:
            rs = ind.rs_line(df["close"], benchmark_df["close"])
            last_rs = rs.iloc[-1]
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
            "ema21": round(float(ema21.iloc[-1]), 4),
            "macd_hist": round(float(histogram.iloc[-1]), 6),
            "atr": round(atr_val, 4),
        })
        return signal

    def _classify_liquidity(self, df: pd.DataFrame) -> str:
        """Return 'liquid' or 'thin' based on average daily price×volume turnover."""
        last_n = df.iloc[-self._liq_avg_days:]
        avg_turnover = float((last_n["close"] * last_n["volume"]).mean())
        return "thin" if avg_turnover < self._liq_min_turnover else "liquid"
