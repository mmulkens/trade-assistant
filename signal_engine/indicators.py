# ---------------------------------------------------------------------------
# indicators.py
#
# Pure technical-indicator functions that operate on pandas Series/DataFrames.
# No side effects, no logging, no config access — every function takes data in
# and returns data out. This makes them trivially testable and reusable by
# future components (e.g. Position Manager also needs ATR).
# ---------------------------------------------------------------------------

from typing import Callable

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average using pandas EWM (span form).

    'adjust=False' uses the recursive definition:
        EMA[t] = alpha * price[t] + (1 - alpha) * EMA[t-1]
    where alpha = 2 / (span + 1).

    This matches the standard trading-platform convention (TradingView, MT5).
    'adjust=True' (the pandas default) uses a weighted sum of all past
    observations, which gives slightly different early values.
    """
    return series.ewm(span=period, adjust=False).mean()


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Standard MACD indicator (12/26/9 by default).

    MACD is used here purely as an internal confirmation layer, not as a
    standalone strategy.  The design explicitly rejected MACD as a peer
    strategy because it is derived entirely from price and lags in trending
    markets.

    Returns a 3-tuple:
        macd_line   — difference between the fast and slow EMA
        signal_line — 9-period EMA of the MACD line
        histogram   — macd_line minus signal_line

    Strategy A uses the histogram (momentum direction of the pullback).
    Strategy B uses the MACD line itself (trend confirmation above zero).
    """
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal_period)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range using Wilder's smoothing (EWM with alpha = 1/period).

    True Range is the largest of:
        - Current high minus current low  (intraday range)
        - |Current high minus previous close|  (gap-up scenario)
        - |Current low  minus previous close|  (gap-down scenario)

    Wilder's original smoothing is equivalent to EWM with alpha = 1/period
    rather than the more common span formula.  This gives a slower-decaying
    average that more accurately reflects sustained volatility changes.

    Used for two purposes in this system:
        1. Stop placement: ensures the stop is at least 1.5×ATR below entry,
           so normal daily noise doesn't trigger it.
        2. Position sizing in the Risk Layer (not in this module).
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    # Compute all three True Range components, then take the max row-wise
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    return tr.ewm(alpha=1 / period, adjust=False).mean()


def rs_line(close: pd.Series, benchmark_close: pd.Series) -> pd.Series:
    """Relative Strength line: stock price divided by benchmark price.

    An RS line that is rising means the stock is outperforming the index.
    An RS line that is falling means it is underperforming, even if the
    stock itself is going up.

    Benchmark is ^STOXX50E (Euro Stoxx 50).  The benchmark index may not
    trade on every day the individual stock trades (different exchange
    calendars), so we forward-fill the benchmark to align the two series
    before dividing.

    The RS value is an annotation on the signal — it is not a filter.
    """
    # Forward-fill handles calendar mismatches between stock and index
    aligned = benchmark_close.reindex(close.index, method="ffill")
    return close / aligned


# ---------------------------------------------------------------------------
# IndicatorLibrary — named registry for pre-computation in SignalEngine
#
# Maps canonical string names to callables: (df: DataFrame, **kwargs) → Series.
# The engine collects the union of required indicators across all active
# strategies and calls compute() once per ticker in prepare() — before any
# day loop — so each series is available for fast lookups throughout the sim.
#
# MACD is decomposed into three separate entries so callers never need to
# unpack tuples; the engine calls each by name and stores the Series directly.
# ---------------------------------------------------------------------------

REGISTRY: dict[str, Callable] = {
    "ema_21":      lambda df, **_: ema(df["close"], 21),
    "ema_50":      lambda df, **_: ema(df["close"], 50),
    "ema_100":     lambda df, **_: ema(df["close"], 100),
    "ema_200":     lambda df, **_: ema(df["close"], 200),
    "macd_line":   lambda df, **_: macd(df["close"], 12, 26, 9)[0],
    "macd_signal": lambda df, **_: macd(df["close"], 12, 26, 9)[1],
    "macd_hist":   lambda df, **_: macd(df["close"], 12, 26, 9)[2],
    "atr_14":      lambda df, **_: atr(df, 14),
    "rs_line":     lambda df, benchmark_df=None, **_: (
                       rs_line(df["close"], benchmark_df["close"])
                       if benchmark_df is not None else pd.Series(dtype=float)
                   ),
    "high_50d":    lambda df, **_: df["high"].rolling(50).max(),
    "high_52wk":   lambda df, **_: df["high"].rolling(252).max(),
    "vol_20d_avg": lambda df, **_: df["volume"].rolling(20).mean(),
}


def compute(name: str, df: pd.DataFrame, **kwargs) -> pd.Series:
    """Compute a named indicator by looking it up in REGISTRY.

    Raises KeyError if the name is not registered.
    kwargs are forwarded to the indicator function (e.g. benchmark_df for rs_line).
    """
    if name not in REGISTRY:
        raise KeyError(
            f"Indicator '{name}' not found in REGISTRY. "
            f"Available: {sorted(REGISTRY.keys())}"
        )
    return REGISTRY[name](df, **kwargs)
