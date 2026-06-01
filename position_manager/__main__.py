# ---------------------------------------------------------------------------
# __main__.py — CLI entry point for the Position Manager
#
# Usage:
#   python -m position_manager              Run the daily EOD batch
#   python -m position_manager run          Same as above (explicit)
#   python -m position_manager status       Print open positions + trail state
#   python -m position_manager close TICKER PRICE [NOTE]
#                                           Record a manual position close
#
# Design intent:
#   The CLI mirrors the Risk Layer's __main__.py pattern: thin orchestrator,
#   all logic in manager.py, config and logger initialised once and passed in.
#
#   The 'close' command is the PM-14 fallback for manual exits that occurred
#   outside the bot (e.g. user closed via IBKR app, bot was offline).  The
#   optional NOTE argument is stored as exit_note for the trading diary.
#
# Examples:
#   python -m position_manager
#   python -m position_manager status
#   python -m position_manager close ASML.AS 72.50
#   python -m position_manager close ASML.AS 72.50 "closed early — earnings risk"
# ---------------------------------------------------------------------------

import sys
from pathlib import Path

import yaml

from utils.json_logger import get_logger
from risk_layer import state as rl_state

from .manager import PositionManager


# ---------------------------------------------------------------------------
# Config and logger helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config.yaml from the project root (one level above this package)."""
    root = Path(__file__).parent.parent
    config_path = root / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_logger(config: dict):
    log_dir = config.get("logging", {}).get("log_dir", "./logs")
    return get_logger("position_manager", log_dir)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _cmd_run(config: dict) -> None:
    """Run the daily EOD position management batch."""
    logger = _build_logger(config)
    rl_state.init_db(config["risk"]["db_path"])
    pm = PositionManager(config, logger)
    pm.run_eod()


def _cmd_status(config: dict) -> None:
    """Print a formatted summary of all open positions and their trail state."""
    rl_state.init_db(config["risk"]["db_path"])
    positions = rl_state.get_open_positions(config["risk"]["db_path"])

    if not positions:
        print("No open positions.")
        return

    print(f"\n{'─' * 72}")
    print(f"  Open positions ({len(positions)} total)")
    print(f"{'─' * 72}")

    for p in positions:
        trail_status = "TRAILING" if p.get("trail_triggered") else "monitoring"
        trail_info = ""
        if p.get("trail_triggered") and p.get("trail_trigger_price"):
            trail_info = f"  triggered @ €{p['trail_trigger_price']:,.2f}"

        peak = p.get("peak_price") or p.get("fill_price") or p["entry_price"]
        print(
            f"\n  {p['ticker']:<12} {trail_status}{trail_info}\n"
            f"    Entry:  €{p['entry_price']:>10,.2f}  |  Stop: €{p['stop_price']:>10,.2f}"
            f"  |  Target: €{(p.get('target_price') or 0):>10,.2f}\n"
            f"    Peak:   €{peak:>10,.2f}  |  Shares: {p['shares']}\n"
            f"    Risk:   €{p['risk_amount']:>10,.2f}  ({p['position_risk_pct']:.2f}%)"
            f"  |  Opened: {p['opened_at'][:10]}"
        )

    # Portfolio-level risk summary
    portfolio_value = config["risk"]["portfolio_value_stub"]
    total_risk = sum(p["risk_amount"] for p in positions)
    open_risk_pct = total_risk / portfolio_value * 100
    max_risk_pct = config["risk"]["max_open_risk_pct"]

    print(f"\n{'─' * 72}")
    print(
        f"  Total open risk: €{total_risk:,.2f}  ({open_risk_pct:.2f}% / {max_risk_pct:.1f}% cap)"
        f"  |  Portfolio: €{portfolio_value:,.0f}"
    )
    print(f"{'─' * 72}\n")


def _cmd_close(config: dict, args: list[str]) -> None:
    """Record a manually-executed position close (PM-14 fallback).

    Args: TICKER PRICE [NOTE]
    """
    if len(args) < 2:
        print("Usage: python -m position_manager close TICKER PRICE [NOTE]")
        print("  Example: python -m position_manager close ASML.AS 72.50 \"earnings risk\"")
        sys.exit(1)

    ticker = args[0]
    try:
        price = float(args[1])
    except ValueError:
        print(f"Error: PRICE must be a number, got {args[1]!r}")
        sys.exit(1)

    note = " ".join(args[2:]) if len(args) > 2 else None

    logger = _build_logger(config)
    rl_state.init_db(config["risk"]["db_path"])
    pm = PositionManager(config, logger)

    success = pm.close_manual(ticker, price, note=note)
    if success:
        print(f"Recorded manual close: {ticker} @ €{price:,.2f}")
        if note:
            print(f"Note: {note}")
    else:
        print(f"Error: no open position found for {ticker}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config = _load_config()
    argv = sys.argv[1:]

    # Default (no subcommand) or explicit 'run'
    if not argv or argv[0] == "run":
        _cmd_run(config)
        return

    cmd = argv[0].lower()

    if cmd == "status":
        _cmd_status(config)

    elif cmd == "close":
        _cmd_close(config, argv[1:])

    else:
        print(f"Unknown command: {cmd!r}")
        print("Usage:")
        print("  python -m position_manager [run]            Daily EOD batch")
        print("  python -m position_manager status           Open position summary")
        print("  python -m position_manager close TICKER PRICE [NOTE]  Manual close")
        sys.exit(1)


if __name__ == "__main__":
    main()
