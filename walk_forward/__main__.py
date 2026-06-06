# ---------------------------------------------------------------------------
# __main__.py — CLI entry point for the Walk-Forward Simulator
#
# Usage:
#   python -m walk_forward              Run simulation on configured watchlist
#   python -m walk_forward run          Same as above (explicit subcommand)
#   python -m walk_forward runs         List all simulation runs in wf_sim.db
#   python -m walk_forward summary <run_id>
#                                       Print metrics for a completed run
#
# The simulation uses all tickers found in config watchlist source files
# (nasdaq100_file, sp500_file, custom_file).  Tickers with fewer than
# min_ticker_days (default 700) calendar days of Parquet cache are silently
# skipped; the run log shows how many were eligible.
#
# Design intent:
#   Thin orchestrator — all simulation logic is in runner.py.
#   Config and logger are initialised once and injected.
# ---------------------------------------------------------------------------

import sqlite3
import sys
from pathlib import Path

import yaml

from utils.json_logger import get_logger
from .runner import WalkForwardRunner
from .summary import calculate_summary, print_summary


# ---------------------------------------------------------------------------
# Config / logger helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    root = Path(__file__).parent.parent
    config_path = root / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_logger(config: dict):
    log_dir = config.get("logging", {}).get("log_dir", "./logs")
    return get_logger("walk_forward", log_dir)


# ---------------------------------------------------------------------------
# Ticker loading
# ---------------------------------------------------------------------------

def _load_tickers(config: dict) -> list[str]:
    """Load the simulation universe from watchlist source files."""
    wl = config.get("watchlist", {})
    tickers: set[str] = set()

    for key, path in wl.items():
        if not key.endswith("_file"):
            continue
        p = Path(path)
        if not p.exists():
            print(f"  [warn] watchlist file not found: {p}")
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                t = line.strip()
                if t and not t.startswith("#"):
                    tickers.add(t)

    # Inline custom list
    for t in wl.get("custom", []):
        if t:
            tickers.add(t)

    return sorted(tickers)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _cmd_run(config: dict) -> None:
    """Run the walk-forward simulation."""
    logger = _build_logger(config)
    tickers = _load_tickers(config)

    if not tickers:
        print("No tickers found in watchlist.  Check config.yaml watchlist section.")
        sys.exit(1)

    print(f"Walk-Forward Simulator starting with {len(tickers)} watchlist tickers …")

    runner = WalkForwardRunner(config, logger)
    run_id = runner.run(tickers)

    print(f"\nSimulation complete.  run_id: {run_id}")

    # Auto-print summary
    wf_db = config["walk_forward"]["wf_db_path"]
    try:
        s = calculate_summary(wf_db, run_id)
        print_summary(s)
    except Exception as exc:
        print(f"[warn] Could not compute summary: {exc}")


def _cmd_runs(config: dict) -> None:
    """List all simulation runs recorded in wf_sim.db."""
    wf_db = config["walk_forward"]["wf_db_path"]
    if not Path(wf_db).exists():
        print("No simulation database found.  Run a simulation first.")
        return

    with sqlite3.connect(wf_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT run_id, sim_start, sim_end, portfolio_start, portfolio_end,
                      total_trades, started_at
                 FROM wf_runs
                ORDER BY started_at DESC
                LIMIT 20"""
        ).fetchall()

    if not rows:
        print("No runs found in wf_sim.db.")
        return

    sep = "─" * 90
    print(f"\n{sep}")
    print(f"  {'run_id':<38} {'sim_start':<12} {'sim_end':<12} {'return':>8}  {'trades':>6}  started")
    print(sep)
    for r in rows:
        p_start = float(r["portfolio_start"] or 0)
        p_end   = float(r["portfolio_end"]   or p_start)
        ret_pct = (p_end - p_start) / p_start * 100 if p_start > 0 else 0.0
        sign    = "+" if ret_pct >= 0 else ""
        started = (r["started_at"] or "")[:19]
        print(
            f"  {r['run_id']:<38} {r['sim_start']:<12} {r['sim_end']:<12} "
            f"{sign}{ret_pct:>6.2f}%  {r['total_trades'] or 0:>6}  {started}"
        )
    print(f"{sep}\n")


def _cmd_summary(config: dict, args: list[str]) -> None:
    """Print detailed summary for a specific run_id."""
    if not args:
        print("Usage: python -m walk_forward summary <run_id>")
        sys.exit(1)

    run_id = args[0]
    wf_db  = config["walk_forward"]["wf_db_path"]

    try:
        s = calculate_summary(wf_db, run_id)
        print_summary(s)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config = _load_config()
    argv   = sys.argv[1:]

    if not argv or argv[0] == "run":
        _cmd_run(config)
        return

    cmd = argv[0].lower()

    if cmd == "runs":
        _cmd_runs(config)

    elif cmd == "summary":
        _cmd_summary(config, argv[1:])

    else:
        print(f"Unknown command: {cmd!r}")
        print("Usage:")
        print("  python -m walk_forward [run]          Run simulation")
        print("  python -m walk_forward runs           List past runs")
        print("  python -m walk_forward summary <id>   Show run summary")
        sys.exit(1)


if __name__ == "__main__":
    main()
