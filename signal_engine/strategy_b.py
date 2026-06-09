# ---------------------------------------------------------------------------
# strategy_b.py — 50-Day Breakout Strategy
#
# Detects stocks that close above their 50-day highest high for the first time,
# with MACD momentum confirmation and elevated volume behind the move.
#
# All conditions are AND-logic: every check must pass for the strategy to fire.
# ---------------------------------------------------------------------------

import pandas as pd


class StrategyB:
    """50-Day Breakout strategy.

    Setup in plain English:
        The stock closes above the highest price it has traded at over the past
        50 days — a fresh breakout to new highs.  This is the first day it has
        done so (staleness filter prevents signalling on day 2, 3, etc. of an
        already-established move).  The MACD line is above zero and rising,
        confirming the broad trend is positive and accelerating.  Volume on the
        breakout bar is at least 1.5× the 20-day average, confirming institutional
        participation — without volume, breakouts frequently fail and reverse.

    Why 50 days?
        20 days fires too frequently with lower conviction (too many false breaks).
        52 weeks (252 bars) fires too rarely to be useful as a daily scanner.
        50 days is the practical sweet spot between frequency and quality.
    """

    # Indicators the engine must pre-compute and pass to evaluate()
    # rs_line is optional — only present when a benchmark_df was provided to prepare().
    # evaluate() degrades gracefully when it is absent.
    required_indicators: list[str] = [
        "ema_21", "ema_50", "ema_100", "ema_200",
        "macd_line",
        "atr_14",
        "high_50d",
        "vol_20d_avg",
        "rs_line",
    ]

    def __init__(self, config: dict) -> None:
        se = config["signal_engine"]
        self._breakout_period: int = se["breakout_period"]                        # 50
        self._volume_multiplier: float = se.get("breakout_volume_multiplier", 1.5)
        # Maximum % below the RS line's own 52-week high for the filter to pass.
        # If the RS line is further below its yearly peak than this threshold the
        # stock is no longer a relative-strength leader — skip the signal.
        self._rs_high_pct: float = se.get("rs_52wk_high_pct", 5.0)

    @property
    def min_bars_required(self) -> int:
        """Minimum bars needed before this strategy can produce a valid result.

        Needs breakout_period + 2:
          +1 because the breakout window excludes today's bar
          +1 for yesterday's close used in the freshness check
        """
        return self._breakout_period + 2

    def evaluate(
        self,
        df: pd.DataFrame,
        indicators: dict[str, pd.Series],
    ) -> tuple[bool, str]:
        """Evaluate the 50-Day Breakout setup on the most recent bar.

        Returns (True, '') if all conditions pass.
        Returns (False, reason_code) at the first failing condition.

        df and indicators are both pre-sliced to the scan date by the engine.
        Do not compute indicators here — use what was passed in.
        """
        if len(df) < self.min_bars_required:
            return False, "strategy_b:insufficient_data"

        close = float(df["close"].iloc[-1])

        # ---------------------------------------------------------------
        # Condition 1 — Fresh breakout above 50-day high
        #
        # Today's close must exceed the highest high of the prior N bars
        # (today's bar is excluded from the window to avoid look-ahead bias).
        #
        # high_50d is a rolling(50).max() series pre-computed over full history.
        # iloc[-2] is yesterday's value = max of the 50 bars ending yesterday,
        # which is equivalent to df["high"].iloc[-51:-1].max() — the prior
        # 50-day high at yesterday's close, excluding today's bar.
        #
        # Freshness check: this must be the FIRST day of the breakout.
        # If yesterday's close already exceeded the N-day high as it stood
        # at yesterday's close (the window ending two days ago), the breakout
        # is stale — price has already moved and the entry risk geometry has
        # deteriorated.  Skip and wait for the next fresh setup.
        #
        # Stale breakouts also cause inflated stop distances: the structural
        # swing low is fixed while entry has moved up, pushing risk % higher.
        # This explains why stale signals disproportionately hit the hard cap.
        # ---------------------------------------------------------------
        high_50d = indicators["high_50d"]
        prior_high_today = float(high_50d.iloc[-2])      # 50d high as of yesterday
        if close <= prior_high_today:
            return False, "strategy_b:no_50d_breakout"

        prior_high_yesterday = float(high_50d.iloc[-3])  # 50d high as of 2 days ago
        close_yesterday = float(df["close"].iloc[-2])
        if close_yesterday > prior_high_yesterday:
            return False, "strategy_b:stale_breakout"

        # ---------------------------------------------------------------
        # Condition 2 — MACD line above zero and rising
        #
        # Uses the MACD line (not histogram) to confirm sustained positive
        # momentum behind the breakout.  The line being above zero means the
        # fast EMA is above the slow EMA — broad trend is bullish.  The line
        # rising means that trend is currently accelerating.
        # ---------------------------------------------------------------
        macd_line = indicators["macd_line"]
        if len(macd_line) < 2:
            return False, "strategy_b:insufficient_macd_bars"

        ml_today     = float(macd_line.iloc[-1])
        ml_yesterday = float(macd_line.iloc[-2])

        if ml_today <= 0:
            return False, "strategy_b:macd_line_below_zero"
        if ml_today <= ml_yesterday:
            return False, "strategy_b:macd_line_not_rising"

        # ---------------------------------------------------------------
        # Condition 3 — Volume confirmation
        #
        # The breakout bar must have volume at least volume_multiplier×
        # (default 1.5×) the 20-day average volume.
        #
        # A breakout on average or below-average volume has significantly
        # lower follow-through probability — it signals that institutions are
        # not participating, and the move is likely to stall or reverse.
        # This is a core principle of IBD / O'Neil breakout methodology.
        # ---------------------------------------------------------------
        avg_volume   = float(indicators["vol_20d_avg"].iloc[-1])
        today_volume = float(df["volume"].iloc[-1])

        # Guard against zero-volume edge case in illiquid instruments
        if avg_volume > 0 and today_volume < self._volume_multiplier * avg_volume:
            ratio = round(today_volume / avg_volume, 2)
            return False, f"strategy_b:low_volume_{ratio}x"

        # ---------------------------------------------------------------
        # Condition 4 — RS line at or near its 52-week high
        #
        # The Relative Strength line (stock price / benchmark price) must be
        # within rs_52wk_high_pct% of its own 52-week high at the time of the
        # breakout.  A declining RS line means institutional money has been
        # rotating out of this stock even while the absolute price rose —
        # these breakouts have significantly lower follow-through probability.
        #
        # A stock with an RS line at a new high is already leading the market
        # before the price breakout fires.  This is the "RS line leading"
        # concept: the signal confirms an acceleration of existing leadership,
        # not a last-gasp move by an extended stock.
        #
        # The threshold (default 5%) is lenient enough to tolerate normal
        # day-to-day fluctuation in the price/benchmark ratio, while still
        # excluding stocks whose RS line peaked months ago and has since
        # been trending lower.
        #
        # Graceful degradation: if rs_line was not pre-computed (no benchmark
        # provided to prepare()), this condition is skipped rather than
        # rejecting all signals in that mode.
        # ---------------------------------------------------------------
        rs_series = indicators.get("rs_line")
        if rs_series is None or len(rs_series) < 2:
            # benchmark_df is mandatory; if rs_line is absent the engine was
            # called without one — reject rather than silently skip the check.
            return False, "strategy_b:rs_line_missing"

        bars_for_rs_52wk = min(252, len(rs_series))
        rs_52wk_high = float(rs_series.iloc[-bars_for_rs_52wk:].max())
        rs_today = float(rs_series.iloc[-1])
        if pd.isna(rs_today):
            return False, "strategy_b:rs_line_nan"
        threshold = rs_52wk_high * (1 - self._rs_high_pct / 100)
        if rs_today < threshold:
            pct_below = round((rs_52wk_high - rs_today) / rs_52wk_high * 100, 1)
            return False, f"strategy_b:rs_line_not_leading_{pct_below}pct_below_peak"

        return True, ""
