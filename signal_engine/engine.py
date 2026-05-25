# ---------------------------------------------------------------------------
# engine.py
#
# Core of the Signal Engine.  Defines the Signal dataclass (the output
# contract with downstream components) and SignalEngine, which scans the
# watchlist against two independent trading strategies and emits structured
# signals when conditions are met.
#
# Architecture position:
#   Data Fetcher (Parquet cache)
#       → SignalEngine.scan()     ← this module
#           → Risk Layer
#               → Order Executor
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from datetime import datetime, timezone
from logging import Logger
from typing import Optional

import pandas as pd

from data_fetcher import cache as cache_store
from . import indicators as ind


# ---------------------------------------------------------------------------
# Signal — the output contract
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """Structured trade signal emitted when a setup passes all conditions.

    Every field in this dataclass must be populated before the Risk Layer
    will accept the signal.  See Section 13A of trade_assistant_design.md
    for the full interface specification.

    Fields that depend on IBKR integration (instrument_id, earnings_flag)
    are stubbed for Phase 1: instrument_id mirrors the ticker string, and
    earnings_flag is always None until reqFundamentalData is wired up.
    """

    # --- Core contract fields (required by Risk Layer / Order Executor) ---
    instrument_id: str      # IBKR conid — stubbed as ticker until IBKR integration
    ticker: str             # Human-readable Yahoo Finance ticker (e.g. "ASML.AS")
    direction: str          # Always 'long' — shorting out of scope for v1
    entry_price: float      # Last close; the intended entry level
    stop_price: float       # Technical stop — fixed at signal time, not trailing
    target_price: float     # Minimum 2:1 R:R target (entry + 2 × risk)
    signal_type: str        # 'pullback' | 'breakout' | 'pullback+breakout'
    liquidity_class: str    # 'liquid' | 'thin' — affects order type downstream
    conviction: str         # 'standard' | 'elevated'
    signal_timestamp: datetime  # UTC timestamp of signal generation
    earnings_flag: Optional[bool]  # True = binary event within N days; None = unknown (stub)

    # --- Internal annotation fields (logged, stored, but not part of the
    #     minimal Risk Layer contract) ---
    strategy_a_fired: bool  # Did EMA Pullback strategy trigger?
    strategy_b_fired: bool  # Did Breakout strategy trigger?
    near_52wk_high: bool    # Is price within near_52wk_high_pct% of 52-week high?
    market_regime: str      # 'bull' | 'bear' | 'unknown'
    rs_value: Optional[float]  # Stock price / benchmark price at signal time


# ---------------------------------------------------------------------------
# SignalEngine
# ---------------------------------------------------------------------------

class SignalEngine:
    """Scans a list of tickers and emits Signal objects for qualifying setups.

    Two strategies run independently for every ticker:
        Strategy A — EMA Pullback: uptrending stock that pulls back to the
                     21 EMA with MACD histogram recovering from negative territory.
        Strategy B — Breakout: stock closes above the 50-day highest high
                     with MACD line above zero and rising.

    OR logic: a signal fires if Strategy A OR Strategy B passes.  Both firing
    simultaneously triggers an elevated conviction annotation.  AND logic was
    rejected because a pullback and a breakout are nearly mutually exclusive
    at the same moment.

    Before scanning individual tickers the engine checks the market regime
    (^STOXX50E vs its 200 EMA).  If the benchmark is in a bear regime, the
    entire scan is skipped and an empty list is returned — individual stock
    setups are unreliable when the broad market is in a downtrend.
    """

    def __init__(self, config: dict, logger: Logger) -> None:
        self._logger = logger
        se = config["signal_engine"]

        # Strategy parameters — values come from config.yaml; defaults are
        # the canonical values agreed in trade_assistant_design.md
        self._ema_period: int = se["ema_period"]                        # 21 (Fibonacci, institutional swing traders)
        self._breakout_period: int = se["breakout_period"]              # 50 (balances frequency vs conviction)
        self._near_52wk_pct: float = se["near_52wk_high_pct"]          # 5  (within 5% of 52-wk high)
        self._benchmark: str = se["benchmark"]                          # '^STOXX50E'
        self._macd_fast: int = se.get("macd_fast", 12)
        self._macd_slow: int = se.get("macd_slow", 26)
        self._macd_signal_period: int = se.get("macd_signal_period", 9)
        self._atr_period: int = se.get("atr_period", 14)

        # Stop placement parameters
        self._swing_low_period: int = se.get("swing_low_period", 10)   # bars to look back for structural stop
        self._stop_atr_mult: float = se.get("stop_atr_multiplier", 1.5)  # volatility floor for stop distance
        self._stop_hard_cap_pct: float = se.get("stop_hard_cap_pct", 8.0)  # never risk more than 8% of entry

        # Target and liquidity parameters
        self._min_rr: float = se.get("min_rr_ratio", 2.0)              # minimum reward:risk = 2:1
        self._pullback_pct: float = se.get("pullback_tolerance_pct", 2.0)  # close within 2% of EMA21
        self._liq_min_turnover: float = se.get("liquidity_min_turnover", 1_000_000)  # €1M/day threshold
        self._liq_avg_days: int = se.get("liquidity_avg_days", 20)

        self._cache_dir: str = config["data_fetcher"]["cache_dir"]

        # Minimum number of bars required before any indicator is reliable.
        # Derived from the longest lookback in use across all strategies.
        self._min_bars: int = max(
            self._macd_slow + self._macd_signal_period + 10,  # MACD warm-up
            100 + 10,                                          # EMA100 warm-up
            self._breakout_period + 5,                         # breakout lookback
            self._swing_low_period,
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def scan(self, tickers: list[str]) -> list[Signal]:
        """Scan a list of tickers and return all qualifying signals.

        Returns an empty list if the market is in a bear regime.
        Each ticker is evaluated independently; signals are never ranked
        or filtered against each other here — that is the Risk Layer's job.
        """
        # --- Step 1: determine market regime ---
        # The benchmark must be loaded first because it drives two things:
        #   a) the regime gate (bear → skip everything)
        #   b) the RS line annotation on each signal
        benchmark_df = cache_store.load(self._benchmark, self._cache_dir)
        if benchmark_df is None or len(benchmark_df) < 200:
            self._logger.warning({
                "event": "benchmark_missing_or_short",
                "benchmark": self._benchmark,
                "bars": len(benchmark_df) if benchmark_df is not None else 0,
            })
            regime = "unknown"
        else:
            regime = self._market_regime(benchmark_df)

        # Bear regime gate: pause all signals when the broad market index is
        # below its 200 EMA.  Individual breakouts and pullbacks in a bear
        # market have much lower follow-through rates.
        if regime == "bear":
            self._logger.info({
                "event": "regime_filter_active",
                "regime": "bear",
                "tickers_skipped": len(tickers),
            })
            return []

        # --- Step 2: scan each ticker ---
        signals: list[Signal] = []
        for ticker in tickers:
            result = self._scan_one(ticker, benchmark_df, regime)
            if result is not None:
                signals.append(result)

        self._logger.info({
            "event": "scan_complete",
            "tickers_scanned": len(tickers),
            "signals_fired": len(signals),
            "regime": regime,
        })
        return signals

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _market_regime(self, df: pd.DataFrame) -> str:
        """Return 'bull' if benchmark close >= 200 EMA, else 'bear'.

        A benchmark price below its 200 EMA is the simplest robust definition
        of a bear market for the broad index.  More complex definitions (e.g.
        drawdown from ATH, 50/200 death cross) were considered but add noise
        without meaningfully improving the filter.
        """
        ema200 = ind.ema(df["close"], 200)
        return "bull" if df["close"].iloc[-1] >= ema200.iloc[-1] else "bear"

    def _scan_one(
        self,
        ticker: str,
        benchmark_df: Optional[pd.DataFrame],
        regime: str,
    ) -> Optional[Signal]:
        """Evaluate a single ticker against both strategies.

        Returns a Signal if either strategy fires, otherwise None.
        Every skip is logged with a reason code so the daily log acts as a
        full audit trail of why each ticker did or did not produce a signal.
        """
        df = cache_store.load(ticker, self._cache_dir)

        # Guard: require enough bars for all indicators to be meaningful
        if df is None or len(df) < self._min_bars:
            self._logger.debug({
                "event": "signal_skipped",
                "ticker": ticker,
                "reason": "insufficient_data",
                "bars": len(df) if df is not None else 0,
                "required": self._min_bars,
            })
            return None

        # --- Compute all indicators once, share across both strategies ---
        ema21  = ind.ema(df["close"], self._ema_period)   # 21-period EMA (pullback anchor)
        ema50  = ind.ema(df["close"], 50)                 # trend filter
        ema100 = ind.ema(df["close"], 100)                # trend filter (intermediate)
        ema200 = ind.ema(df["close"], 200)                # trend filter (long-term)
        macd_line, _, histogram = ind.macd(
            df["close"], self._macd_fast, self._macd_slow, self._macd_signal_period
        )
        atr_series = ind.atr(df, self._atr_period)

        close     = float(df["close"].iloc[-1])
        low_today = float(df["low"].iloc[-1])

        # --- Run both strategies ---
        a_fired, a_reason = self._strategy_a(
            close, low_today, ema21, ema50, ema100, ema200, histogram
        )
        b_fired, b_reason = self._strategy_b(df, close, macd_line)

        if not a_fired and not b_fired:
            self._logger.debug({
                "event": "signal_skipped",
                "ticker": ticker,
                "strategy_a_reason": a_reason,
                "strategy_b_reason": b_reason,
            })
            return None

        # Determine signal type — 'pullback+breakout' when both strategies fire
        # simultaneously (rare but valid; gets elevated conviction automatically)
        if a_fired and b_fired:
            signal_type = "pullback+breakout"
        elif a_fired:
            signal_type = "pullback"
        else:
            signal_type = "breakout"

        # --- Stop price calculation (3-step) ---
        # Step 1: structural stop — lowest low of the past N bars.
        #         This is the primary stop: placing it below the most recent
        #         swing low means the trade is invalidated if price undercuts
        #         a level that institutions were defending.
        entry    = close
        swing_low = float(df["low"].iloc[-self._swing_low_period:].min())

        # Step 2: ATR floor — entry minus 1.5 × ATR14.
        #         If the structural swing low is closer to entry than 1.5×ATR,
        #         the stop is too tight and will be triggered by normal intraday
        #         noise.  Taking the lower (wider) of the two ensures we always
        #         give the trade at least 1.5 ATR of breathing room.
        atr_val  = float(atr_series.iloc[-1])
        atr_stop = entry - self._stop_atr_mult * atr_val
        stop = min(swing_low, atr_stop)   # lower price = wider stop

        # Step 3: hard cap — never risk more than stop_hard_cap_pct (8%) of entry.
        #         Prevents extreme stops on very volatile or thinly traded names.
        max_risk = entry * (self._stop_hard_cap_pct / 100)
        if (entry - stop) > max_risk:
            stop = entry - max_risk
        stop = round(stop, 4)

        # --- Target price (minimum 2:1 R:R) ---
        # The Order Executor re-validates R:R after adding transaction costs
        # (TOB tax).  This is the raw technical target; cost-inclusive R:R
        # is checked downstream.
        risk   = entry - stop
        target = round(entry + self._min_rr * risk, 4)

        # --- Conviction annotation ---
        # Elevated conviction is assigned when:
        #   a) Both strategies fire simultaneously (pullback + breakout), OR
        #   b) Price is within near_52wk_high_pct% of the 52-week high,
        #      indicating the stock is in a leadership position in the market.
        bars_for_52wk = min(252, len(df))   # use all available data if < 1 year cached
        near_52wk = close >= float(df["high"].iloc[-bars_for_52wk:].max()) * (
            1 - self._near_52wk_pct / 100
        )
        elevated   = (a_fired and b_fired) or near_52wk
        conviction = "elevated" if elevated else "standard"

        # --- Liquidity classification ---
        liquidity_class = self._classify_liquidity(df)

        # --- RS line (annotation only, not a filter) ---
        rs_val: Optional[float] = None
        if benchmark_df is not None:
            rs = ind.rs_line(df["close"], benchmark_df["close"])
            last_rs = rs.iloc[-1]
            rs_val = round(float(last_rs), 6) if not pd.isna(last_rs) else None

        signal = Signal(
            instrument_id=ticker,    # placeholder until IBKR conid lookup is integrated
            ticker=ticker,
            direction="long",
            entry_price=round(entry, 4),
            stop_price=stop,
            target_price=target,
            signal_type=signal_type,
            liquidity_class=liquidity_class,
            conviction=conviction,
            signal_timestamp=datetime.now(timezone.utc),
            earnings_flag=None,      # stub — always None until IBKR reqFundamentalData is wired
            strategy_a_fired=a_fired,
            strategy_b_fired=b_fired,
            near_52wk_high=near_52wk,
            market_regime=regime,
            rs_value=rs_val,
        )

        # Log the full signal payload so the daily log is a complete audit trail
        self._logger.info({
            "event": "signal_fired",
            "ticker": ticker,
            "signal_type": signal_type,
            "conviction": conviction,
            "entry": signal.entry_price,
            "stop": signal.stop_price,
            "target": signal.target_price,
            "risk_pct": round((entry - stop) / entry * 100, 2),
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

    def _strategy_a(
        self,
        close: float,
        low_today: float,
        ema21: pd.Series,
        ema50: pd.Series,
        ema100: pd.Series,
        ema200: pd.Series,
        histogram: pd.Series,
    ) -> tuple[bool, str]:
        """Strategy A — EMA Pullback.

        All conditions must be True (AND logic within the strategy).

        Condition 1 — Uptrend stack (4 checks):
            close  > EMA50           price is above the medium-term trend line
            EMA21  > EMA50           fast line above slow line (momentum up)
            EMA50  > EMA100 > EMA200  all major averages in bullish order

        Rationale: the 21 EMA is the Fibonacci-derived level used by institutional
        swing traders (Minervini / IBD methodology).  Enough market participants
        watch it to create real support on pullbacks.  EMA20 has less institutional
        backing; EMA10 is too noisy for the swing timeframe.

        Condition 2 — Pullback to 21 EMA (either sub-condition is sufficient):
            a) Close within 2% of EMA21 (tight approach)
            b) Intraday wick touches EMA21 but close is above it
               (low <= EMA21 < close)

        Condition 3 — MACD histogram recovering:
            Histogram is negative (still in pullback territory — avoids chasing)
            AND the last two bars are consecutively rising (h[-1] > h[-2] > h[-3])

        The two-bar rising requirement filters out single-bar noise bounces.
        The 'still negative' requirement means momentum is recovering from
        weakness, not already extended — this is the 'dark-red to light-red'
        transition (approaching zero from below, not crossing above it).

        Returns (True, '') on success, or (False, reason_code) on failure.
        The reason_code is the first failing condition — useful for debugging
        which condition is blocking signals on a given ticker.
        """
        e21  = float(ema21.iloc[-1])
        e50  = float(ema50.iloc[-1])
        e100 = float(ema100.iloc[-1])
        e200 = float(ema200.iloc[-1])

        # --- Condition 1: Uptrend stack ---
        if close <= e50:
            return False, "strategy_a:close_below_ema50"
        if e21 <= e50:
            return False, "strategy_a:ema21_not_above_ema50"
        if not (e50 > e100 > e200):
            return False, "strategy_a:ema_stack_not_aligned"

        # --- Condition 2: Pullback to 21 EMA ---
        within_2pct = abs(close - e21) / e21 <= (self._pullback_pct / 100)
        wick_touch  = low_today <= e21 < close   # wick through EMA, close above
        if not (within_2pct or wick_touch):
            return False, "strategy_a:no_pullback_to_ema21"

        # --- Condition 3: MACD histogram turning up from negative ---
        if len(histogram) < 3:
            return False, "strategy_a:insufficient_macd_bars"
        h1 = float(histogram.iloc[-1])
        h2 = float(histogram.iloc[-2])
        h3 = float(histogram.iloc[-3])
        if h1 >= 0:
            return False, "strategy_a:histogram_not_negative"
        if not (h1 > h2 > h3):
            return False, "strategy_a:histogram_not_turning_up"

        return True, ""

    def _strategy_b(
        self,
        df: pd.DataFrame,
        close: float,
        macd_line: pd.Series,
    ) -> tuple[bool, str]:
        """Strategy B — 50-Day Breakout.

        All conditions must be True (AND logic within the strategy).

        Condition 1 — Breakout above 50-day highest high:
            Today's close > max(high) over the prior 50 trading days
            (the 50-day window excludes today to avoid look-ahead bias).

        The 50-day lookback was chosen over:
            20-day — fires too frequently with lower conviction
            52-week — fires too rarely as a daily scanner primary trigger
        50 days balances signal frequency against conviction.

        Condition 2 — MACD line above zero and rising:
            macd_line[-1] > 0        trend momentum is positive overall
            macd_line[-1] > macd_line[-2]  momentum is accelerating

        Using the MACD *line* (not histogram) for Strategy B confirms the
        sustained upward trend behind the breakout, as opposed to a single
        volatile bar piercing an old high.

        Returns (True, '') on success, or (False, reason_code) on failure.
        """
        if len(df) < self._breakout_period + 1:
            return False, "strategy_b:insufficient_data"

        # Exclude today's bar from the lookback so we test 'close above prior high'
        prior_high = float(df["high"].iloc[-(self._breakout_period + 1):-1].max())
        if close <= prior_high:
            return False, "strategy_b:no_50d_breakout"

        if len(macd_line) < 2:
            return False, "strategy_b:insufficient_macd_bars"
        ml1 = float(macd_line.iloc[-1])
        ml2 = float(macd_line.iloc[-2])
        if ml1 <= 0:
            return False, "strategy_b:macd_line_below_zero"
        if ml1 <= ml2:
            return False, "strategy_b:macd_line_not_rising"

        return True, ""

    def _classify_liquidity(self, df: pd.DataFrame) -> str:
        """Classify an instrument as 'liquid' or 'thin' based on average daily turnover.

        Turnover = close price × volume, averaged over the last 20 trading days.
        Threshold: < €1,000,000/day = 'thin'.

        Instruments classified as 'thin' receive different treatment downstream:
        the Order Executor uses limit orders instead of market orders, and the
        stop type changes to avoid slippage in low-volume names.
        """
        last_n = df.iloc[-self._liq_avg_days:]
        avg_turnover = float((last_n["close"] * last_n["volume"]).mean())
        return "thin" if avg_turnover < self._liq_min_turnover else "liquid"
