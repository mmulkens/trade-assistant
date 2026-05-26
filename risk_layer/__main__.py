"""
CLI entry point for the Risk Layer.

Usage:
    python -m risk_layer status                       # show open positions and risk summary
    python -m risk_layer close TICKER PRICE REASON    # manually close a position
    python -m risk_layer unpause                       # clear the daily loss pause flag
    python -m risk_layer --config /path/to.yaml ...   # custom config file

The 'status' command is useful for verifying state during development and for
inspecting the system when IBKR integration is not yet live.
"""

import argparse
import sys
from pathlib import Path

import yaml


def _load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _cmd_status(config: dict) -> None:
    from utils.json_logger import get_logger
    from risk_layer.layer import RiskLayer
    from risk_layer import state as st

    log_dir = config.get("logging", {}).get("log_dir", "./logs")
    logger = get_logger("risk_layer", log_dir)
    rl = RiskLayer(config, logger)

    summary = rl.get_open_risk_summary()
    db_path = config["risk"]["db_path"]
    daily_pnl = st.get_daily_realized_pnl(db_path)
    paused = st.is_trading_paused(db_path)

    print(f"\n{'=' * 60}")
    print(f"  Risk Layer — Status")
    print(f"{'=' * 60}")
    print(f"  Portfolio value (stub): €{summary['portfolio_value']:,.2f}")
    print(f"  Open positions:         {summary['open_positions']}")
    print(f"  Total open risk:        €{summary['total_risk_amount']:,.2f}  "
          f"({summary['total_open_risk_pct']:.2f}%)")
    print(f"  Remaining risk budget:  {summary['remaining_risk_budget_pct']:.2f}%")
    print(f"  Daily realised P&L:     €{daily_pnl:+,.2f}")
    print(f"  Trading paused:         {'YES — daily loss limit reached' if paused else 'No'}")

    if summary["positions"]:
        print(f"\n  {'Ticker':<14} {'Shares':>6} {'Entry':>8} {'Stop':>8} "
              f"{'Risk €':>9} {'Risk%':>6}  Opened")
        print(f"  {'-' * 70}")
        for p in summary["positions"]:
            print(
                f"  {p['ticker']:<14} {p['shares']:>6} {p['entry']:>8.2f} "
                f"{p['stop']:>8.2f} {p['risk_amount']:>9.2f} "
                f"{p['position_risk_pct']:>5.2f}%  {p['opened_at'][:10]}"
            )
    print()


def _cmd_close(config: dict, ticker: str, price: float, reason: str) -> None:
    from utils.json_logger import get_logger
    from risk_layer.layer import RiskLayer

    log_dir = config.get("logging", {}).get("log_dir", "./logs")
    logger = get_logger("risk_layer", log_dir)
    rl = RiskLayer(config, logger)

    updated = rl.close_position(ticker, price, reason)
    if updated:
        print(f"Position closed: {ticker} at {price:.4f} ({reason})")
    else:
        print(f"No open position found for {ticker}.", file=sys.stderr)
        sys.exit(1)


def _cmd_unpause(config: dict) -> None:
    from risk_layer import state as st

    db_path = config["risk"]["db_path"]
    st.clear_trading_pause(db_path)
    print("Trading pause cleared.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trade Assistant — Risk Layer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m risk_layer status\n"
            "  python -m risk_layer close ASML.AS 720.50 stop\n"
            "  python -m risk_layer unpause\n"
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Show open positions and risk summary")

    close_p = subparsers.add_parser("close", help="Manually close a position")
    close_p.add_argument("ticker", help="Ticker symbol, e.g. ASML.AS")
    close_p.add_argument("price", type=float, help="Close price")
    close_p.add_argument(
        "reason",
        choices=["stop", "target", "trail", "manual"],
        help="Close reason",
    )

    subparsers.add_parser("unpause", help="Clear the daily loss limit pause flag")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    config = _load_config(args.config)

    from risk_layer import state as st
    st.init_db(config["risk"]["db_path"])

    if args.command == "status":
        _cmd_status(config)
    elif args.command == "close":
        _cmd_close(config, args.ticker, args.price, args.reason)
    elif args.command == "unpause":
        _cmd_unpause(config)

    sys.exit(0)


if __name__ == "__main__":
    main()
