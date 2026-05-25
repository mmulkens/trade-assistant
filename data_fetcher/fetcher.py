"""
fetcher.py — Orchestrates parallel OHLCV fetching for the full watchlist.

Design
------
DataFetcher.run() is the single public entry point.  It:
  1. Prepends the benchmark ticker (^STOXX50E) so it is always up to date.
  2. Splits the ticker list into fixed-size batches (config: batch_size).
  3. Runs each batch concurrently using a ThreadPoolExecutor
     (config: workers threads per batch).
  4. Sleeps between batches (config: batch_pause_seconds) to stay within
     Yahoo Finance's informal rate limits.

For each ticker the fetch decision is:
  - Cache hit, already up to date → skip (status='skipped')
  - Cache hit, stale              → delta fetch from last_date+1 to today
  - Cache miss OR --full-refresh  → full fetch of history_days calendar days

Retry logic
-----------
Each provider call is wrapped in _fetch_with_retry(), which retries up to
max_retries times with exponential-ish back-off (attempt × retry_backoff_seconds).
Transient Yahoo errors (rate-limit, network blip) are handled here; permanent
failures (ticker delisted, no data) fall through to status='error'.

Thread safety
-------------
The cache module functions (load/save/merge/validate) each operate on a
single ticker's file, so concurrent calls for different tickers are safe.
All logger calls use structured dicts — the underlying file handler is
opened in append mode and is safe for concurrent writes on CPython.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from logging import Logger
from typing import Optional

from . import cache as cache_store
from .providers import BaseProvider


@dataclass
class TickerResult:
    """Result for a single ticker fetch attempt."""
    ticker: str
    status: str          # 'full_fetch' | 'delta' | 'skipped' | 'error'
    rows_added: int
    error: Optional[str] = None


@dataclass
class FetchSummary:
    """
    Aggregate outcome of a DataFetcher.run() call.

    Properties are computed lazily from the flat results list so the caller
    can inspect whichever metrics it cares about.
    """
    results: list[TickerResult]
    duration_seconds: float

    @property
    def attempted(self) -> int:
        """Total number of tickers processed (including skipped)."""
        return len(self.results)

    @property
    def succeeded(self) -> int:
        """Tickers that did not end in error (includes skipped and fetched)."""
        return sum(1 for r in self.results if r.status != "error")

    @property
    def failed(self) -> int:
        """Tickers that returned an error after all retries."""
        return sum(1 for r in self.results if r.status == "error")

    @property
    def total_rows_added(self) -> int:
        """Net new rows written across all tickers."""
        return sum(r.rows_added for r in self.results)

    @property
    def failed_tickers(self) -> list[str]:
        """Symbols that failed, for logging and re-try reporting."""
        return [r.ticker for r in self.results if r.status == "error"]


class DataFetcher:
    """
    Fetches and caches daily OHLCV data for a list of tickers.

    Usage:
        config   = yaml.safe_load(open("config.yaml"))
        provider = get_provider(config)
        logger   = get_logger("data_fetcher", config["logging"]["log_dir"])
        fetcher  = DataFetcher(config, provider, logger)
        summary  = fetcher.run(tickers, full_refresh=False)
    """

    def __init__(self, config: dict, provider: BaseProvider, logger: Logger) -> None:
        self._provider = provider
        self._logger = logger

        df_cfg = config["data_fetcher"]
        # How many calendar days of history to fetch on the first run for a ticker
        self._history_days: int = df_cfg["history_days"]
        # Thread pool size per batch
        self._workers: int = df_cfg["workers"]
        # Number of tickers per concurrent batch
        self._batch_size: int = df_cfg["batch_size"]
        # Sleep between batches (seconds) to respect Yahoo rate limits
        self._batch_pause: float = df_cfg["batch_pause_seconds"]
        self._cache_dir: str = df_cfg["cache_dir"]
        self._max_retries: int = df_cfg.get("max_retries", 3)
        self._retry_backoff: float = df_cfg.get("retry_backoff_seconds", 1.0)
        # Benchmark is always fetched first so the signal engine always has it
        self._benchmark: str = config["signal_engine"]["benchmark"]

    def run(self, tickers: list[str], full_refresh: bool = False) -> FetchSummary:
        """
        Fetch OHLCV data for all tickers, respecting the local Parquet cache.

        full_refresh=True ignores existing cache and re-downloads the full
        history window for every ticker.  Use this after a config change to
        history_days, or to repair a corrupted cache.
        """
        # Deduplicate while preserving order; benchmark goes first
        all_tickers = list(dict.fromkeys([self._benchmark] + tickers))

        if full_refresh:
            self._logger.info({"event": "full_refresh_triggered", "ticker_count": len(all_tickers)})

        self._logger.info({
            "event": "fetch_initiated",
            "ticker_count": len(all_tickers),
            "full_refresh": full_refresh,
        })

        # Split into batches so we can pause between them
        batches = [
            all_tickers[i: i + self._batch_size]
            for i in range(0, len(all_tickers), self._batch_size)
        ]

        results: list[TickerResult] = []
        start_time = time.monotonic()

        for batch_idx, batch in enumerate(batches):
            batch_results = self._run_batch(batch, full_refresh)
            results.extend(batch_results)
            # Pause between batches but not after the last one
            if batch_idx < len(batches) - 1:
                time.sleep(self._batch_pause)

        duration = time.monotonic() - start_time
        summary = FetchSummary(results=results, duration_seconds=duration)

        self._logger.info({
            "event": "fetch_complete",
            "attempted": summary.attempted,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "rows_added": summary.total_rows_added,
            "duration_seconds": round(duration, 2),
            "failed_tickers": summary.failed_tickers,
        })

        return summary

    def _run_batch(self, batch: list[str], full_refresh: bool) -> list[TickerResult]:
        """
        Fetch a single batch of tickers concurrently.

        Uses as_completed() so results are processed as they arrive rather
        than waiting for the slowest ticker in the batch.  Any uncaught
        exception from a thread is caught here and recorded as an error result
        so one bad ticker never crashes the whole batch.
        """
        results: list[TickerResult] = []
        with ThreadPoolExecutor(max_workers=self._workers) as executor:
            future_to_ticker = {
                executor.submit(self._fetch_one, ticker, full_refresh): ticker
                for ticker in batch
            }
            for future in as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    self._logger.error({
                        "event": "fetch_error",
                        "ticker": ticker,
                        "error": str(exc),
                    })
                    results.append(TickerResult(ticker=ticker, status="error", rows_added=0, error=str(exc)))
        return results

    def _fetch_one(self, ticker: str, full_refresh: bool) -> TickerResult:
        """
        Decide what to fetch for one ticker, call the provider, and update cache.

        Decision logic:
          - Cache exists and is already current (last_date >= today) → skip.
          - Cache exists and is stale → delta fetch from last_date+1.
          - No cache or full_refresh requested → full fetch of history_days.

        After fetching, the new data is merged with the existing cache (delta
        mode) or replaces it (full_fetch mode), then written back to Parquet.
        Validation issues are logged as warnings but do not block the write.
        """
        existing = cache_store.load(ticker, self._cache_dir)

        if existing is not None and not full_refresh:
            self._logger.debug({
                "event": "cache_hit",
                "ticker": ticker,
                "last_date": cache_store.get_last_date(existing).isoformat(),
                "cached_rows": len(existing),
            })
        else:
            self._logger.debug({"event": "cache_miss", "ticker": ticker})

        # Determine the date range to request from the provider
        if existing is not None and not full_refresh:
            last_date = cache_store.get_last_date(existing)
            if last_date >= date.today():
                # Cache already contains today's bar — nothing to do
                return TickerResult(ticker=ticker, status="skipped", rows_added=0)
            fetch_start = last_date + timedelta(days=1)
            mode = "delta"
        else:
            fetch_start = date.today() - timedelta(days=self._history_days)
            mode = "full_fetch"

        # end is exclusive in the Yahoo API; +1 day ensures today's bar is included
        fetch_end = date.today() + timedelta(days=1)

        self._logger.debug({
            "event": "fetch_start",
            "ticker": ticker,
            "mode": mode,
            "start": fetch_start.isoformat(),
            "end": fetch_end.isoformat(),
        })

        new_data = self._fetch_with_retry(ticker, fetch_start, fetch_end)

        if new_data is None or new_data.empty:
            # Provider returned nothing even after retries — mark as error
            return TickerResult(ticker=ticker, status="error", rows_added=0, error="no data returned")

        # Merge new rows into existing cache (delta) or use new data as-is (full)
        if existing is not None and not full_refresh:
            merged, rows_added = cache_store.merge(existing, new_data)
        else:
            merged = new_data.sort_index()
            rows_added = len(merged)

        # Validate before persisting; issues are warnings, not hard failures
        validation = cache_store.validate(merged)
        if not validation.valid:
            self._logger.warning({
                "event": "validation_warning",
                "ticker": ticker,
                "issues": validation.issues,
            })

        cache_store.save(ticker, merged, self._cache_dir)

        self._logger.debug({
            "event": "fetch_success",
            "ticker": ticker,
            "mode": mode,
            "rows_added": rows_added,
        })

        return TickerResult(ticker=ticker, status=mode, rows_added=rows_added)

    def _fetch_with_retry(self, ticker: str, start: date, end: date) -> Optional[pd.DataFrame]:
        """
        Call the provider with exponential back-off retry.

        Sleeps attempt × retry_backoff_seconds between retries so a brief
        Yahoo rate-limit window is survived without hammering the API.
        Returns None (not raises) after all retries are exhausted so the
        caller can record a clean error result.
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return self._provider.fetch(ticker, start, end)
            except Exception as exc:
                last_error = exc
                self._logger.warning({
                    "event": "fetch_retry",
                    "ticker": ticker,
                    "attempt": attempt,
                    "error": str(exc),
                })
                if attempt < self._max_retries:
                    time.sleep(self._retry_backoff * attempt)

        self._logger.error({
            "event": "fetch_error",
            "ticker": ticker,
            "attempts": self._max_retries,
            "error": str(last_error),
        })
        return None
