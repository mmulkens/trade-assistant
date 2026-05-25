"""
CLI entry point for the Signal Engine.

Usage:
    python -m signal_engine                         # scan full watchlist
    python -m signal_engine ASML.AS SIE.DE          # scan specific tickers
    python -m signal_engine --config /path/to.yaml  # custom config file

What this does:
    1. Loads config.yaml and resolves the ticker list (args or watchlist files)
    2. Initialises the SQLite signals database (creates table if absent)
    3. Runs SignalEngine.scan() — checks market regime, then evaluates every
       ticker against Strategy A (EMA Pullback) and Strategy B (Breakout)
    4. Persists all fired signals to ./data/signals.db
    5. Prints a formatted summary table to stdout

Logs are written as JSON-lines to ./logs/signal_engine_YYYY-MM-DD.jsonl.
Every fired signal AND every skipped ticker is logged with a reason code.
"""
import argparse
import sys
from pathlib import Path

import yaml


def _load_config(path: str) -> dict:
    """Load and parse config.yaml into a plain dict."""
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _tickers_from_tsv(path: str) -> list[str]:
    """Extract the ticker column from a tab-separated watchlist file.

    The first column is the ticker.  The first row is a header and is skipped.
    Empty lines and lines with an empty first column are ignored.
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    tickers = []
    for line in lines[1:]:          # skip header row
        parts = line.split("\t")
        if parts and parts[0].strip():
            tickers.append(parts[0].strip())
    return tickers


def _load_watchlist(config: dict) -> list[str]:
    """Build the full ticker list from all watchlist sources in config.yaml.

    Sources (applied in order, duplicates removed):
        1. eurostoxx600_file — static STOXX 600 constituents TSV
        2. custom_file       — manually curated supplemental list TSV
        3. watchlist.custom  — inline list of tickers in the YAML itself

    dict.fromkeys preserves insertion order while deduplicating.
    """
    wl = config.get("watchlist", {})
    tickers: list[str] = []

    if wl.get("eurostoxx600_file"):
        tickers.extend(_tickers_from_tsv(wl["eurostoxx600_file"]))

    if wl.get("custom_file"):
        path = wl["custom_file"]
        if Path(path).exists():
            tickers.extend(_tickers_from_tsv(path))

    tickers.extend(wl.get("custom", []))
    return list(dict.fromkeys(tickers))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trade Assistant — Signal Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m signal_engine                  # full watchlist\n"
            "  python -m signal_engine ASML.AS SIE.DE   # specific tickers\n"
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "tickers",
        nargs="*",
        metavar="TICKER",
        help="Tickers to scan (overrides watchlist if provided)",
    )
    args = parser.parse_args()

    config = _load_config(args.config)

    # Positional args take priority over the watchlist; fall back to watchlist
    tickers = args.tickers or _load_watchlist(config)

    if not tickers:
        print(
            "No tickers specified. Pass tickers as arguments or populate the watchlist in config.yaml.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Deferred imports keep startup fast and avoid circular-import issues
    # when this module is loaded before the package is fully initialised
    from utils.json_logger import get_logger
    from signal_engine.engine import SignalEngine
    from signal_engine import db

    log_dir = config.get("logging", {}).get("log_dir", "./logs")
    logger = get_logger("signal_engine", log_dir)

    # Ensure the signals table exists before the scan runs
    db_path = config.get("signal_engine", {}).get("db_path", "./data/signals.db")
    db.init_db(db_path)

    engine = SignalEngine(config, logger)
    signals = engine.scan(tickers)

    # --- Print results ---
    if signals:
        db.save_signals(signals, db_path)
        capped = sum(1 for s in signals if s.stop_capped)
        print(f"\nSignals fired: {len(signals)}  —  saved to {db_path}")
        if capped:
            print(f"⚠  {capped} signal(s) have stop at hard cap — wide stop, verify setup geometry")
        print(
            f"\n{'Ticker':<14} {'Type':<22} {'Conv':<10} "
            f"{'Entry':>8} {'Stop':>8} {'Target':>8} {'Risk%':>6}  {'Flag'}"
        )
        print("-" * 90)
        for s in signals:
            risk_pct = (s.entry_price - s.stop_price) / s.entry_price * 100
            flag = "⚠ CAP" if s.stop_capped else ""
            print(
                f"{s.ticker:<14} {s.signal_type:<22} {s.conviction:<10} "
                f"{s.entry_price:>8.2f} {s.stop_price:>8.2f} {s.target_price:>8.2f} "
                f"{risk_pct:>5.1f}%  {flag}"
            )
    else:
        print("\nNo signals generated.")

    sys.exit(0)


if __name__ == "__main__":
    main()
