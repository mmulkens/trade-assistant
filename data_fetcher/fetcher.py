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
    ticker: str
    status: str          # 'full_fetch' | 'delta' | 'skipped' | 'error'
    rows_added: int
    error: Optional[str] = None


@dataclass
class FetchSummary:
    results: list[TickerResult]
    duration_seconds: float

    @property
    def attempted(self) -> int:
        return len(self.results)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.status != "error")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "error")

    @property
    def total_rows_added(self) -> int:
        return sum(r.rows_added for r in self.results)

    @property
    def failed_tickers(self) -> list[str]:
        return [r.ticker for r in self.results if r.status == "error"]


class DataFetcher:
    def __init__(self, config: dict, provider: BaseProvider, logger: Logger) -> None:
        self._provider = provider
        self._logger = logger

        df_cfg = config["data_fetcher"]
        self._history_days: int = df_cfg["history_days"]
        self._workers: int = df_cfg["workers"]
        self._batch_size: int = df_cfg["batch_size"]
        self._batch_pause: float = df_cfg["batch_pause_seconds"]
        self._cache_dir: str = df_cfg["cache_dir"]
        self._max_retries: int = df_cfg.get("max_retries", 3)
        self._retry_backoff: float = df_cfg.get("retry_backoff_seconds", 1.0)
        self._benchmark: str = config["signal_engine"]["benchmark"]

    def run(self, tickers: list[str], full_refresh: bool = False) -> FetchSummary:
        all_tickers = list(dict.fromkeys([self._benchmark] + tickers))

        if full_refresh:
            self._logger.info({"event": "full_refresh_triggered", "ticker_count": len(all_tickers)})

        self._logger.info({
            "event": "fetch_initiated",
            "ticker_count": len(all_tickers),
            "full_refresh": full_refresh,
        })

        batches = [
            all_tickers[i: i + self._batch_size]
            for i in range(0, len(all_tickers), self._batch_size)
        ]

        results: list[TickerResult] = []
        start_time = time.monotonic()

        for batch_idx, batch in enumerate(batches):
            batch_results = self._run_batch(batch, full_refresh)
            results.extend(batch_results)
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

        if existing is not None and not full_refresh:
            last_date = cache_store.get_last_date(existing)
            if last_date >= date.today():
                return TickerResult(ticker=ticker, status="skipped", rows_added=0)
            fetch_start = last_date + timedelta(days=1)
            mode = "delta"
        else:
            fetch_start = date.today() - timedelta(days=self._history_days)
            mode = "full_fetch"

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
            return TickerResult(ticker=ticker, status="error", rows_added=0, error="no data returned")

        if existing is not None and not full_refresh:
            merged, rows_added = cache_store.merge(existing, new_data)
        else:
            merged = new_data.sort_index()
            rows_added = len(merged)

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

    def _fetch_with_retry(self, ticker: str, start: date, end: date):
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
