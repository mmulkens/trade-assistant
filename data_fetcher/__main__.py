"""
CLI entry point: python -m data_fetcher [options] [TICKER ...]

Reads config from config.yaml (or --config path). Tickers can be supplied as
positional args or via --ticker-file (one ticker per line). If neither is
provided the watchlist custom list from config is used.
"""
import argparse
import sys
from pathlib import Path

import yaml


def _load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


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
        help="Path to a file with one ticker per line",
    )
    parser.add_argument(
        "tickers",
        nargs="*",
        metavar="TICKER",
        help="Tickers to fetch (overrides watchlist if provided)",
    )
    args = parser.parse_args()

    config = _load_config(args.config)

    if args.tickers:
        tickers = args.tickers
    elif args.ticker_file:
        tickers = Path(args.ticker_file).read_text(encoding="utf-8").splitlines()
        tickers = [t.strip() for t in tickers if t.strip() and not t.startswith("#")]
    else:
        tickers = config.get("watchlist", {}).get("custom", [])

    if not tickers:
        print(
            "No tickers specified. Pass tickers as arguments, use --ticker-file, "
            "or populate watchlist.custom in config.yaml.",
            file=sys.stderr,
        )
        sys.exit(1)

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
