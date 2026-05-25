from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
_LARGE_GAP_DAYS = 5


@dataclass
class ValidationResult:
    valid: bool
    issues: list[str] = field(default_factory=list)


def _cache_path(ticker: str, cache_dir: str) -> Path:
    safe_name = ticker.replace("^", "_").replace("/", "_").replace(":", "_")
    return Path(cache_dir) / f"{safe_name}.parquet"


def load(ticker: str, cache_dir: str) -> Optional[pd.DataFrame]:
    path = _cache_path(ticker, cache_dir)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    return df


def save(ticker: str, df: pd.DataFrame, cache_dir: str) -> None:
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    df.to_parquet(_cache_path(ticker, cache_dir))


def validate(df: pd.DataFrame) -> ValidationResult:
    issues: list[str] = []

    for col in OHLCV_COLUMNS:
        if col not in df.columns:
            issues.append(f"missing column: {col}")

    if "close" in df.columns:
        null_count = int(df["close"].isna().sum())
        if null_count > 0:
            issues.append(f"{null_count} null close price(s)")

    dupe_count = int(df.index.duplicated().sum())
    if dupe_count > 0:
        issues.append(f"{dupe_count} duplicate date(s)")

    if len(df) > 1:
        deltas = df.index.to_series().diff().dropna()
        large_gaps = deltas[deltas > pd.Timedelta(days=_LARGE_GAP_DAYS)]
        if len(large_gaps) > 0:
            issues.append(f"{len(large_gaps)} date gap(s) > {_LARGE_GAP_DAYS} calendar days")

    return ValidationResult(valid=len(issues) == 0, issues=issues)


def get_last_date(df: pd.DataFrame) -> date:
    return df.index.max().date()


def merge(existing: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Append new rows, deduplicate, sort. Returns (merged_df, rows_added)."""
    before = len(existing)
    combined = pd.concat([existing, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    rows_added = max(len(combined) - before, 0)
    return combined, rows_added
