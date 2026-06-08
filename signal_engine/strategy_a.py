# ---------------------------------------------------------------------------
# strategy_a.py — EMA Pullback Strategy
#
# Detects stocks in a confirmed uptrend that have pulled back to their 21 EMA
# and show early signs of momentum recovery via the MACD histogram.
#
# All conditions are AND-logic: every check must pass for the strategy to fire.
# ---------------------------------------------------------------------------

import pandas as pd


class StrategyA:
    """EMA Pullback strategy.

    Setup in plain English:
        The stock is in a clear uptrend (all EMAs in bullish order, price above
        EMA50, no recent freefall).  Price has pulled back to touch the 21 EMA
        and closed back above it — either as a wick on today's bar, or as a
        recovery candle after yesterday closed at/below the EMA.  The MACD
        histogram was getting more negative and has now ticked up for the first
        time — momentum is rotating from selling pressure to buying interest,
        but the histogram is still negative, confirming we are catching the
        early recovery rather than chasing a move already in progress.

    Why the 21 EMA?
        Fibonacci number; widely observed by institutional swing traders
        (Minervini / IBD methodology).  Enough market participants watch this
        level to generate real support on pullbacks.  EMA20 has less
        institutional backing; EMA10 is too noisy for swing timeframes.
    """

    # Indicators the engine must pre-compute and pass to evaluate()
    required_indicators: list[str] = [
        "ema_21", "ema_50", "ema_100", "ema_200",
        "macd_hist",
        "atr_14",
    ]

    def __init__(self, config: dict) -> None:
        pass  # All parameters come from the engine; strategy_a has no config of its own

    @property
    def min_bars_required(self) -> int:
        """Minimum bars needed before this strategy can produce a valid result."""
        return 3  # Needs 3 histogram bars for the MACD shape check, plus 2 for yesterday

    def evaluate(
        self,
        df: pd.DataFrame,
        indicators: dict[str, pd.Series],
    ) -> tuple[bool, str]:
        """Evaluate the EMA Pullback setup on the most recent bar.

        Returns (True, '') if all conditions pass.
        Returns (False, reason_code) at the first failing condition.
        The reason_code identifies exactly which gate blocked the signal,
        and is logged by the engine for every skipped ticker.

        df and indicators are both pre-sliced to the scan date by the engine.
        Do not compute indicators here — use what was passed in.
        """
        e21   = float(indicators["ema_21"].iloc[-1])
        e50   = float(indicators["ema_50"].iloc[-1])
        close = float(df["close"].iloc[-1])

        # Price must still be above EMA50 — the engine's Guard A confirms the full
        # EMA chain is aligned, but we also require close > EMA50 to ensure price
        # has not already broken down through its medium-term trend line.
        if close <= e50:
            return False, "strategy_a:close_below_ema50"

        # ---------------------------------------------------------------
        # Pullback condition — EMA21 must have been touched and recovered
        #
        # Replaces the old "close within 2% of EMA21" proximity check, which
        # fired on approach without actual contact.  Two valid cases:
        #
        # Case A — same-bar wick rejection:
        #   today.low <= EMA21 AND today.close > EMA21
        #   The candle touched or pierced the EMA but closed back above it.
        #
        # Case B — prior-bar breach + today's recovery candle:
        #   (yesterday.low < EMA21 OR yesterday.close < EMA21)
        #   AND today.close > EMA21
        #   Price went through or under the EMA on the prior bar; today it
        #   has reclaimed the level — a one-day-delayed recovery.
        #
        # What must NOT fire:
        #   - Price approaching EMA from above without touching it
        #   - Price below EMA with no recovery (ongoing breakdown)
        # ---------------------------------------------------------------
        low_today       = float(df["low"].iloc[-1])
        low_yesterday   = float(df["low"].iloc[-2])
        close_yesterday = float(df["close"].iloc[-2])

        same_bar_recovery  = low_today <= e21 < close
        prior_breach       = (low_yesterday < e21) or (close_yesterday < e21)
        prior_bar_recovery = prior_breach and (close > e21)

        if not (same_bar_recovery or prior_bar_recovery):
            return False, "strategy_a:no_ema21_touch_or_recovery"

        # ---------------------------------------------------------------
        # MACD histogram confirmation — "dark red to light red"
        #
        # Required shape across the last three histogram bars:
        #   h[-2] < h[-3]  — prior bar: histogram was getting more negative
        #                    (confirms there was real selling pressure to recover from)
        #   h[-1] > h[-2]  — current bar: histogram is now ticking up
        #                    (one improving bar is sufficient)
        #   h[-1] < 0      — histogram still negative (not yet extended;
        #                    we are catching the early rotation, not chasing)
        #
        # The prior-decline requirement prevents this from firing on random
        # single-bar noise or on histograms that were never negative.
        # ---------------------------------------------------------------
        histogram = indicators["macd_hist"]
        if len(histogram) < 3:
            return False, "strategy_a:insufficient_macd_bars"

        h1 = float(histogram.iloc[-1])   # today
        h2 = float(histogram.iloc[-2])   # yesterday
        h3 = float(histogram.iloc[-3])   # two days ago

        if h1 >= 0:
            return False, "strategy_a:histogram_not_negative"
        if h2 >= h3:
            return False, "strategy_a:histogram_no_prior_decline"
        if h1 <= h2:
            return False, "strategy_a:histogram_not_improving"

        return True, ""
