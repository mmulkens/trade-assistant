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

    def __init__(self, config: dict) -> None:
        se = config["signal_engine"]
        self._breakout_period: int = se["breakout_period"]                        # 50
        self._volume_multiplier: float = se.get("breakout_volume_multiplier", 1.5)
        self._volume_avg_days: int = se.get("liquidity_avg_days", 20)

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
        close: float,
        macd_line: pd.Series,
    ) -> tuple[bool, str]:
        """Evaluate the 50-Day Breakout setup on the most recent bar.

        Returns (True, '') if all conditions pass.
        Returns (False, reason_code) at the first failing condition.
        """
        if len(df) < self.min_bars_required:
            return False, "strategy_b:insufficient_data"

        # ---------------------------------------------------------------
        # Condition 1 — Fresh breakout above 50-day high
        #
        # Today's close must exceed the highest high of the prior N bars
        # (today's bar is excluded from the window to avoid look-ahead bias).
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
        prior_high_today = float(df["high"].iloc[-(self._breakout_period + 1):-1].max())
        if close <= prior_high_today:
            return False, "strategy_b:no_50d_breakout"

        # Freshness: was yesterday already a breakout day?
        # prior_high_yesterday = the N-day high as it stood at yesterday's close
        # (window ends two days ago, same N-day length, shifted back one bar)
        prior_high_yesterday = float(df["high"].iloc[-(self._breakout_period + 2):-2].max())
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
        avg_volume   = float(df["volume"].iloc[-self._volume_avg_days:].mean())
        today_volume = float(df["volume"].iloc[-1])

        # Guard against zero-volume edge case in illiquid instruments
        if avg_volume > 0 and today_volume < self._volume_multiplier * avg_volume:
            ratio = round(today_volume / avg_volume, 2)
            return False, f"strategy_b:low_volume_{ratio}x"

        return True, ""
