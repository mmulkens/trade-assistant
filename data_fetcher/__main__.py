"""
__main__.py — CLI entry point for the Data Fetcher.

Usage
-----
    python -m data_fetcher                          # fetch full watchlist (delta)
    python -m data_fetcher --full-refresh           # re-download everything
    python -m data_fetcher ASML.AS SAP.DE MC.PA     # fetch specific tickers
    python -m data_fetcher --ticker-file my.txt     # load tickers from file

Ticker resolution order (first match wins):
  1. Positional ticker arguments on the command line.
  2. --ticker-file: plain text file, one ticker per line, # lines ignored.
  3. Watchlist files configured in config.yaml (eurostoxx600_file, custom_file)
     plus the inline watchlist.custom list.

Exit codes:
  0 — all tickers succeeded (or were skipped as already up to date)
  1 — one or more tickers failed after retries, or no tickers were found
"""

import argparse
import sys
from pathlib import Path

import yaml


def _load_config(path: str) -> dict:
    """Load and parse config.yaml (or any YAML file passed via --config)."""
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _tickers_from_file(path: str) -> list[str]:
    """One ticker per line, no header. Lines starting with '#' are ignored."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _load_watchlist(config: dict) -> list[str]:
    """
    Build the full ticker list from config.yaml watchlist settings.

    Any key ending in '_file' in the watchlist section is treated as a ticker
    file path and loaded automatically — adding a new source is a config-only
    change.  Files that don't exist on disk are skipped silently.
    The inline 'custom' list is appended last.

    The benchmark ticker (^GSPC) is NOT included here — DataFetcher.run()
    always prepends it automatically.
    """
    wl = config.get("watchlist", {})
    tickers: list[str] = []

    for key, path in wl.items():
        if key.endswith("_file") and path and Path(path).exists():
            tickers.extend(_tickers_from_file(path))

    tickers.extend(wl.get("custom", []))

    # dict.fromkeys preserves insertion order while removing cross-file duplicates
    return list(dict.fromkeys(tickers))


def main() -> None:
    parser = argparse.ArgumentParser(description="Trade Assistant — Data Fetcher")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Force complete re-fetch of all tickers, ignoring existing cache",
    )
    parser.add_argument(
        "--ticker-file",
        metavar="FILE",
        help="Path to a plain-text file with one ticker per line",
    )
    parser.add_argument(
        "tickers",
        nargs="*",
        metavar="TICKER",
        help="Explicit tickers to fetch; overrides the watchlist when provided",
    )
    args = parser.parse_args()

    config = _load_config(args.config)

    # Resolve ticker list according to the priority order described above
    if args.tickers:
        tickers = args.tickers
    elif args.ticker_file:
        tickers = Path(args.ticker_file).read_text(encoding="utf-8").splitlines()
        tickers = [t.strip() for t in tickers if t.strip() and not t.startswith("#")]
    else:
        tickers = _load_watchlist(config)

    if not tickers:
        print(
            "No tickers specified. Pass tickers as arguments, use --ticker-file, "
            "or populate watchlist.nasdaq_file / watchlist.nyse_file / watchlist.custom in config.yaml.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Defer heavy imports until after argument validation so --help is instant
    from utils.json_logger import get_logger
    from data_fetcher.providers import get_provider
    from data_fetcher.fetcher import DataFetcher

    log_dir = config.get("logging", {}).get("log_dir", "./logs")
    logger = get_logger("data_fetcher", log_dir)
    provider = get_provider(config)
    fetcher = DataFetcher(config, provider, logger)

    summary = fetcher.run(tickers, full_refresh=args.full_refresh)

    print(
        f"\nFetch complete — "
        f"attempted: {summary.attempted}, "
        f"succeeded: {summary.succeeded}, "
        f"failed: {summary.failed}, "
        f"rows added: {summary.total_rows_added}, "
        f"duration: {summary.duration_seconds:.1f}s"
    )
    if summary.failed_tickers:
        print(f"Failed tickers: {', '.join(summary.failed_tickers)}")

    sys.exit(0 if summary.failed == 0 else 1)


if __name__ == "__main__":
    main()

