# ---------------------------------------------------------------------------
# walker.py — DataFrameWalker: lookahead-safe cache replacement for simulation
#
# Responsibilities:
#   - Pre-load all ticker Parquet files at initialisation (one read per ticker)
#   - Expose a .load(ticker, cache_dir) method that matches data_fetcher.cache.load()
#   - Return df[df.index <= current_date] — the single enforcement point for
#     no-lookahead guarantee across the entire simulation
#   - advance(date) advances the simulation window; called once per day in runner
#
# Drop-in contract:
#   walker.load(ticker, cache_dir) is signature-compatible with
#   data_fetcher.cache.load(ticker, cache_dir), so the walker can be passed
#   directly as the 'cache' parameter to SignalEngine (SE-25).
#
# Why pre-load instead of reading on each call:
#   A simulation run processes hundreds of tickers × hundreds of days.
#   Reading Parquet from disk on every call would be ~40k disk reads per run.
#   Pre-loading into a dict reduces that to one read per ticker at startup.
# ---------------------------------------------------------------------------

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd


class DataFrameWalker:
    """Drop-in replacement for data_fetcher.cache with time-bounded reads.

    Typical usage inside the runner day loop:
        walker = DataFrameWalker(all_tickers, cache_dir)
        for date in simulation_dates:
            walker.advance(date)
            # All downstream load() calls now see data only up to 'date'
            signals = signal_engine.scan(tickers)   # uses walker internally
            df = walker.load("AAPL", cache_dir)     # direct ATR computation
    """

    def __init__(self, tickers: list[str], cache_dir: str) -> None:
        self._current_date: Optional[pd.Timestamp] = None
        self._store: dict[str, pd.DataFrame] = {}
        self._load_all(tickers, cache_dir)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def advance(self, date: pd.Timestamp) -> None:
        """Set the current simulation date. Must be called before each day's load() calls."""
        if date.tzinfo is not None:
            date = date.tz_localize(None)
        self._current_date = date

    def load(self, ticker: str, cache_dir: str) -> Optional[pd.DataFrame]:
        """Return Parquet data for ticker sliced to current_date (inclusive).

        Matches the signature of data_fetcher.cache.load() so the walker can be
        injected into SignalEngine as a drop-in cache replacement.

        Returns None if the ticker is not pre-loaded or has no rows on or before
        current_date (mirrors the real cache's None-on-missing behaviour).
        """
        if self._current_date is None:
            raise RuntimeError("DataFrameWalker.advance() must be called before load()")

        df = self._store.get(ticker)
        if df is None:
            return None

        # tz-normalize the index if needed before comparison
        idx = df.index
        if idx.tz is not None:
            idx = idx.tz_localize(None)
            df = df.copy()
            df.index = idx

        sliced = df[idx <= self._current_date]
        return sliced if len(sliced) > 0 else None

    @property
    def loaded_tickers(self) -> list[str]:
        """Tickers that were successfully pre-loaded (Parquet file existed)."""
        return list(self._store.keys())

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _load_all(self, tickers: list[str], cache_dir: str) -> None:
        """Pre-load Parquet files for all tickers. Missing or unreadable files are skipped."""
        cache_path = Path(cache_dir)
        for ticker in tickers:
            safe = ticker.replace("^", "_").replace("/", "_").replace(":", "_")
            path = cache_path / f"{safe}.parquet"
            if path.exists():
                try:
                    self._store[ticker] = pd.read_parquet(path)
                except Exception:
                    pass
