# ---------------------------------------------------------------------------
# summary.py — Walk-Forward run statistics calculator
#
# Responsibilities:
#   - Compute return, win rate, profit factor, max drawdown, holding period
#     from a completed wf_sim.db run
#   - Print a formatted text summary to stdout
#
# All calculations are read-only against wf_sim.db.  This module never writes.
# ---------------------------------------------------------------------------

import sqlite3
from datetime import datetime
from typing import Optional


def calculate_summary(db_path: str, run_id: str) -> dict:
    """Compute high-level simulation statistics for a completed run.

    Returns a flat dict suitable for printing or serialising to JSON.
    Raises ValueError if run_id is not found in db_path.
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        run_row = conn.execute(
            "SELECT * FROM wf_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            raise ValueError(f"Run {run_id!r} not found in {db_path}")
        run = dict(run_row)

        pos_rows = conn.execute(
            """SELECT net_pnl, entry_date, exit_date, exit_reason
                 FROM wf_positions
                WHERE run_id = ? AND exit_date IS NOT NULL""",
            (run_id,),
        ).fetchall()

        equity_rows = conn.execute(
            """SELECT date, portfolio_value
                 FROM wf_equity_curve
                WHERE run_id = ?
                ORDER BY date ASC""",
            (run_id,),
        ).fetchall()

    positions = [dict(r) for r in pos_rows]
    equity    = [dict(r) for r in equity_rows]

    portfolio_start = float(run["portfolio_start"])
    portfolio_end   = float(run["portfolio_end"] or portfolio_start)
    total_return_pct = (portfolio_end - portfolio_start) / portfolio_start * 100

    # Trade statistics
    net_pnls = [float(p["net_pnl"]) for p in positions if p["net_pnl"] is not None]
    wins   = [v for v in net_pnls if v > 0]
    losses = [v for v in net_pnls if v <= 0]

    n_trades     = len(net_pnls)
    win_rate_pct = len(wins) / n_trades * 100 if n_trades > 0 else 0.0
    avg_win      = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss     = sum(losses) / len(losses) if losses else 0.0
    profit_factor: Optional[float] = (
        round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None
    )

    # Average holding period (calendar days)
    holding_days = []
    for p in positions:
        if p["entry_date"] and p["exit_date"]:
            try:
                d0 = datetime.fromisoformat(p["entry_date"])
                d1 = datetime.fromisoformat(p["exit_date"])
                holding_days.append((d1 - d0).days)
            except ValueError:
                pass
    avg_holding_days = sum(holding_days) / len(holding_days) if holding_days else 0.0

    # Maximum peak-to-trough drawdown from the equity curve
    max_drawdown_pct = _calc_max_drawdown(equity)

    # Exit reason breakdown
    exit_reasons: dict[str, int] = {}
    for p in positions:
        reason = p.get("exit_reason") or "unknown"
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    return {
        "run_id":            run_id,
        "sim_start":         run["sim_start"],
        "sim_end":           run["sim_end"],
        "portfolio_start":   portfolio_start,
        "portfolio_end":     round(portfolio_end, 2),
        "total_return_pct":  round(total_return_pct, 2),
        "n_trades":          n_trades,
        "win_rate_pct":      round(win_rate_pct, 1),
        "avg_win":           round(avg_win, 2),
        "avg_loss":          round(avg_loss, 2),
        "profit_factor":     profit_factor,
        "max_drawdown_pct":  round(max_drawdown_pct, 2),
        "avg_holding_days":  round(avg_holding_days, 1),
        "exit_reasons":      exit_reasons,
    }


def print_summary(s: dict) -> None:
    """Print a formatted text summary of a walk-forward run to stdout."""
    sep = "─" * 72
    pnl_sign = "+" if s["total_return_pct"] >= 0 else ""

    print(f"\n{sep}")
    print(f"  Walk-Forward Summary  ·  {s['sim_start']} → {s['sim_end']}")
    print(f"  Run: {s['run_id']}")
    print(sep)
    print(
        f"\n  Portfolio:   ${s['portfolio_start']:>12,.2f}  →  "
        f"${s['portfolio_end']:>12,.2f}   "
        f"({pnl_sign}{s['total_return_pct']:.2f}%)"
    )
    print(f"  Max drawdown:  {s['max_drawdown_pct']:.2f}%")
    print(f"\n  Trades:        {s['n_trades']}")
    print(f"  Win rate:      {s['win_rate_pct']:.1f}%")
    print(f"  Avg win:      ${s['avg_win']:>10,.2f}")
    print(f"  Avg loss:     ${s['avg_loss']:>10,.2f}")
    pf_str = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "N/A"
    print(f"  Profit factor: {pf_str}")
    print(f"  Avg hold:      {s['avg_holding_days']:.1f} calendar days")

    if s["exit_reasons"]:
        print(f"\n  Exit reasons:")
        for reason, count in sorted(s["exit_reasons"].items()):
            print(f"    {reason:<20s} {count}")

    print(f"\n{sep}\n")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _calc_max_drawdown(equity: list[dict]) -> float:
    """Compute maximum peak-to-trough drawdown % from the equity curve."""
    if len(equity) < 2:
        return 0.0
    values = [float(e["portfolio_value"]) for e in equity]
    peak   = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd
