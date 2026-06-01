# ---------------------------------------------------------------------------
# __main__.py — CLI entry point for the Sim Executor
#
# Usage:
#   python -m sim_executor                        # process all pending signals once
#   python -m sim_executor --watch                # poll for new signals continuously
#   python -m sim_executor --dry-run              # evaluate and log without writing
#   python -m sim_executor --config /path/to.yaml # custom config file
#
# Normal daily workflow:
#   1. Run the Signal Engine:   python -m signal_engine
#   2. Run the Sim Executor:    python -m sim_executor
#   The two steps are independent processes; SX reads the output of SE from
#   signals.db rather than calling SE directly.  This matches the production
#   architecture where the real Order Executor also reads from signals.db.
# ---------------------------------------------------------------------------

import argparse
import sys

import yaml

from signal_engine import db as se_db
from utils.json_logger import get_logger
from .executor import SimExecutor


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trade Assistant — Sim Executor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m sim_executor\n"
            "  python -m sim_executor --watch\n"
            "  python -m sim_executor --dry-run\n"
        ),
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll signals.db continuously instead of processing once and exiting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Evaluate signals through the Risk Layer and log outcomes, "
            "but do not write positions, send notifications, or mark signals processed"
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    log_dir = config.get("logging", {}).get("log_dir", "./logs")
    logger = get_logger("sim_executor", log_dir)

    # Run the Signal Engine's schema migration before reading signals.db.
    # This ensures the processed and run_type columns exist on any existing
    # database that was created before SX was built.  The migration is
    # forward-only (ALTER TABLE ADD COLUMN) and safe to call on every startup.
    se_db.init_db(config["signal_engine"]["db_path"])

    executor = SimExecutor(config, logger, dry_run=args.dry_run)

    if args.watch:
        executor.run_watch()
    else:
        executor.run_batch()

    sys.exit(0)


if __name__ == "__main__":
    main()
