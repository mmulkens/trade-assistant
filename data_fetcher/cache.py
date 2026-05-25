"""
cache.py — Parquet-based local OHLCV cache.

Each ticker gets its own file: ./cache/<TICKER>.parquet
Special characters in ticker symbols (^ / :) are replaced with underscores
to produce valid filenames across all operating systems.

Why Parquet?
  - Columnar storage is fast for the read patterns the signal engine uses
    (read a single column such as 'close' across the full history).
  - Built-in compression keeps the cache small (~12 KB per ticker for 300 days).
  - pandas ↔ Parquet round-trips are lossless for float64 and datetime index.

Cache lifecycle (managed by DataFetcher, not this module):
  1. load()       — read existing rows on startup; returns None if no file yet.
  2. merge()      — append new rows to existing data, deduplicate, sort.
  3. validate()   — sanity-check the merged frame before writing.
  4. save()       — atomically overwrite the Parquet file with the final frame.
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

# Columns that must be present; any extra columns returned by a provider are stripped.
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

# Gaps longer than this many calendar days are flagged as suspicious.
# Legitimate causes: multi-day market holidays (e.g. Easter, Christmas week).
# Illegitimate causes: missing data, provider outage, bad date range.
_LARGE_GAP_DAYS = 5


@dataclass
class ValidationResult:
    """Outcome of a validate() call.  valid=True only when issues is empty."""
    valid: bool
    issues: list[str] = field(default_factory=list)


def _cache_path(ticker: str, cache_dir: str) -> Path:
    """
    Return the Parquet file path for a given ticker.

    Tickers like '^STOXX50E' or 'BRK/B' contain characters that are illegal
    in Windows filenames, so we sanitise them first.
    """
    safe_name = ticker.replace("^", "_").replace("/", "_").replace(":", "_")
    return Path(cache_dir) / f"{safe_name}.parquet"


def load(ticker: str, cache_dir: str) -> Optional[pd.DataFrame]:
    """
    Load cached OHLCV data for a ticker.

    Returns a DataFrame with a tz-naive DatetimeIndex named 'date', or None
    if no cache file exists yet (first run for this ticker).

    The tz_localize(None) call strips any timezone that Parquet may have
    re-attached during deserialisation; we always work in naive local dates.
    """
    path = _cache_path(ticker, cache_dir)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    return df


def save(ticker: str, df: pd.DataFrame, cache_dir: str) -> None:
    """
    Persist a DataFrame to Parquet.

    Creates the cache directory if it doesn't exist yet.
    Overwrites any existing file for this ticker (full replace, not append —
    the caller is responsible for merging before calling save).
    """
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    df.to_parquet(_cache_path(ticker, cache_dir))


def validate(df: pd.DataFrame) -> ValidationResult:
    """
    Check a DataFrame for common data quality issues.

    Checks performed:
      1. All five OHLCV columns are present.
      2. No null close prices (nulls in volume are tolerated for index tickers).
      3. No duplicate dates in the index.
      4. No gaps > _LARGE_GAP_DAYS between consecutive trading days.

    Returns a ValidationResult; issues are logged as warnings by DataFetcher
    but do NOT prevent the data from being saved — the downstream signal engine
    is responsible for handling gaps gracefully.
    """
    issues: list[str] = []

    # Check all required columns are present
    for col in OHLCV_COLUMNS:
        if col not in df.columns:
            issues.append(f"missing column: {col}")

    # Null close prices make every indicator undefined for those rows
    if "close" in df.columns:
        null_count = int(df["close"].isna().sum())
        if null_count > 0:
            issues.append(f"{null_count} null close price(s)")

    # Duplicate dates would cause incorrect delta detection on the next run
    dupe_count = int(df.index.duplicated().sum())
    if dupe_count > 0:
        issues.append(f"{dupe_count} duplicate date(s)")

    # Large gaps suggest missing data rather than normal market closures
    if len(df) > 1:
        deltas = df.index.to_series().diff().dropna()
        large_gaps = deltas[deltas > pd.Timedelta(days=_LARGE_GAP_DAYS)]
        if len(large_gaps) > 0:
            issues.append(f"{len(large_gaps)} date gap(s) > {_LARGE_GAP_DAYS} calendar days")

    return ValidationResult(valid=len(issues) == 0, issues=issues)


def get_last_date(df: pd.DataFrame) -> date:
    """Return the most recent trading date present in the cache."""
    return df.index.max().date()


def merge(existing: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    Append new rows to an existing cache frame, deduplicate, and sort by date.

    Deduplication keeps the *last* occurrence of any duplicated date, so a
    corrected bar from the provider always wins over a stale cached value.

    Returns (merged_df, rows_added) where rows_added is the net increase
    in row count (never negative — we don't count overwrites as additions).
    """
    before = len(existing)
    combined = pd.concat([existing, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    rows_added = max(len(combined) - before, 0)
    return combined, rows_added
