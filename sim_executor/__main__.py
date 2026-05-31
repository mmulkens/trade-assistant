"""
CLI entry point for the Sim Executor.

Usage:
    python -m sim_executor                        # process all pending signals once
    python -m sim_executor --watch                # poll for new signals continuously
    python -m sim_executor --dry-run              # evaluate and log without writing
    python -m sim_executor --config /path/to.yaml # custom config file
"""

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
    parser.add_argument("--watch", action="store_true", help="Poll for new signals continuously")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without writing or notifying")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    log_dir = config.get("logging", {}).get("log_dir", "./logs")
    logger = get_logger("sim_executor", log_dir)

    # Ensure the signals.db schema is up to date (adds processed/run_type columns
    # to any existing table via forward migration)
    se_db.init_db(config["signal_engine"]["db_path"])

    executor = SimExecutor(config, logger, dry_run=args.dry_run)

    if args.watch:
        executor.run_watch()
    else:
        executor.run_batch()

    sys.exit(0)


if __name__ == "__main__":
    main()
