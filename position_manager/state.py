# ---------------------------------------------------------------------------
# state.py — SQLite persistence for the Position Manager
#
# Responsibilities:
#   - Update stop_price, risk_per_share, and risk_amount when the active stop
#     rises (implements the RL-10 contract — see note below)
#   - Record peak_price advances (highest close seen since entry)
#   - Mark trail_triggered = 1 and store trail_trigger_price at activation
#   - Close a position with the full set of PM fields: gross_pnl, net_pnl,
#     exit_commission, bot_initiated, exit_note (PM-15)
#   - Count unprocessed signals (time-exit gate PM-10)
#   - Return current open risk % (time-exit gate PM-10)
#
# Why is close_position_full() here rather than in risk_layer/state.py?
#   risk_layer.state.close_position() is a minimal function that sets
#   status, close_price, realized_pnl, and close_reason — enough for the
#   daily loss limit check.  The Position Manager needs a full close that
#   also populates exit_commission, gross_pnl, net_pnl, bot_initiated, and
#   exit_note in a single atomic transaction.  Adding PM concerns to the Risk
#   Layer would violate its single-responsibility boundary.
#
#   The Risk Layer's get_daily_realized_pnl() reads realized_pnl WHERE
#   status='closed', so closes performed by this module feed correctly into
#   the RL-06 daily loss limit check — no cross-module callback required.
#
# RL-10 contract:
#   When the active stop rises, update_position_stop() writes the new
#   stop_price AND recalculates risk_amount = max(0, shares × (entry − stop)).
#   Once the stop is at or above entry + costs, risk_amount becomes 0.
#   risk_layer.state.get_open_risk_amount() sums risk_amount WHERE status='open',
#   so the freed risk budget is reflected automatically on the next evaluate()
#   call — the Risk Layer needs no explicit notification.
#
# Schema dependency:
#   All columns written here are defined in risk_layer/state.py's
#   _POSITION_COLUMNS and are forward-migrated by risk_layer.state.init_db().
#   This module does not own or migrate the schema.
# ---------------------------------------------------------------------------

import sqlite3
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Stop / peak / trail update operations
# ---------------------------------------------------------------------------

def update_position_stop(
    ticker: str,
    new_stop: float,
    new_risk_per_share: float,
    new_risk_amount: float,
    db_path: str,
) -> bool:
    """Raise the active stop and update the risk figures in one DB write.

    Called every time the trail moves up.  Updating risk_amount here is the
    RL-10 implementation: the Risk Layer reads get_open_risk_amount() (SUM of
    risk_amount WHERE status='open') on every evaluate() call, so the freed
    risk budget is picked up automatically with no explicit callback.

    new_risk_amount is floored at 0.0 before writing.  Once the stop is above
    the entry price the position contributes nothing to the 6% open risk cap —
    all capital risk on this trade has been eliminated.

    Returns True if a matching open position was found and updated.
    """
    with sqlite3.connect(db_path) as conn:
        rowcount = conn.execute(
            """UPDATE risk_positions
               SET stop_price = ?, risk_per_share = ?, risk_amount = ?
               WHERE ticker = ? AND status = 'open'""",
            (new_stop, new_risk_per_share, max(0.0, new_risk_amount), ticker),
        ).rowcount
    return rowcount > 0


def update_peak_price(ticker: str, peak_price: float, db_path: str) -> bool:
    """Advance the all-time high close price seen since position entry (PM-16).

    The caller (manager.py) is responsible for the max() comparison before
    calling this function — it only writes when peak_price is a genuine new
    high.  This function applies the value blindly and unconditionally.

    Returns True if a matching open position was found and updated.
    """
    with sqlite3.connect(db_path) as conn:
        rowcount = conn.execute(
            "UPDATE risk_positions SET peak_price = ? WHERE ticker = ? AND status = 'open'",
            (peak_price, ticker),
        ).rowcount
    return rowcount > 0


def activate_trail(ticker: str, trail_trigger_price: float, db_path: str) -> bool:
    """Mark the trailing stop as active and record the trigger price (PM-03).

    Called exactly once per position, at the moment the position first crosses
    the trail_trigger_r × R profit threshold.  After this write, manager.py
    treats the position as trail-managed on every subsequent EOD run.

    trail_trigger_price is the actual daily close that crossed the threshold —
    stored for the trading diary to show exactly when the trail activated.

    Returns True if a matching open position was found and updated.
    """
    with sqlite3.connect(db_path) as conn:
        rowcount = conn.execute(
            """UPDATE risk_positions
               SET trail_triggered = 1, trail_trigger_price = ?
               WHERE ticker = ? AND status = 'open'""",
            (trail_trigger_price, ticker),
        ).rowcount
    return rowcount > 0


# ---------------------------------------------------------------------------
# Full position close (PM-15: bot_initiated, exit_note, gross/net P&L)
# ---------------------------------------------------------------------------

def close_position_full(
    ticker: str,
    close_price: float,
    reason: str,
    tob_pct: float,
    bot_initiated: bool,
    db_path: str,
    exit_note: Optional[str] = None,
    closed_at: Optional[str] = None,
) -> Optional[dict]:
    """Close a position and populate all PM financial fields atomically.

    Calculates in one transaction:
        gross_pnl        = (close_price − entry_price) × shares
        exit_commission  = close_price × shares × tob_pct / 100  (TOB on notional)
        net_pnl          = gross_pnl − entry_commission − exit_commission
        realized_pnl     = gross_pnl  (matches risk_layer convention; used by
                           get_daily_realized_pnl() for the RL-06 loss limit)

    Parameters:
        close_price    — actual exit price
        reason         — 'trail_hit' | 'stop_hit' | 'time_exit' | 'manual'
        tob_pct        — from config.costs.tob_pct (0.35 for Belgian TOB)
        bot_initiated  — False for user-initiated manual closes, True otherwise
        exit_note      — optional free-text annotation (PM-15 trading diary)
        closed_at      — ISO-8601 UTC timestamp; defaults to utcnow() if None

    Returns a dict with the calculated P&L figures, or None if no open
    position exists for this ticker.
    """
    ts = closed_at or datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """SELECT id, entry_price, shares, entry_commission
               FROM risk_positions
               WHERE ticker = ? AND status = 'open'
               ORDER BY opened_at DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
        if row is None:
            return None

        pos_id, entry_price, shares, entry_commission = row

        # --- P&L calculations ---
        gross_pnl = round((close_price - entry_price) * shares, 2)
        exit_commission = round(close_price * shares * tob_pct / 100.0, 2)
        entry_comm = entry_commission or 0.0
        net_pnl = round(gross_pnl - entry_comm - exit_commission, 2)

        # realized_pnl uses gross (matching risk_layer.state.close_position)
        # so get_daily_realized_pnl() produces consistent numbers regardless
        # of which close path was used.
        conn.execute(
            """UPDATE risk_positions
               SET status = 'closed',
                   closed_at = ?,
                   close_price = ?,
                   realized_pnl = ?,
                   close_reason = ?,
                   gross_pnl = ?,
                   net_pnl = ?,
                   exit_commission = ?,
                   bot_initiated = ?,
                   exit_note = ?
               WHERE id = ?""",
            (
                ts, close_price, gross_pnl, reason,
                gross_pnl, net_pnl,
                exit_commission, int(bot_initiated), exit_note,
                pos_id,
            ),
        )

    return {
        "pos_id": pos_id,
        "entry_price": entry_price,
        "close_price": close_price,
        "shares": shares,
        "gross_pnl": gross_pnl,
        "exit_commission": exit_commission,
        "net_pnl": net_pnl,
        "closed_at": ts,
    }


# ---------------------------------------------------------------------------
# Query helpers for time-exit gate (PM-10)
# ---------------------------------------------------------------------------

def count_pending_signals(signals_db: str) -> int:
    """Return the number of signals not yet processed by the Sim/Order Executor.

    PM-10: the time-based exit only fires when there are pending signals —
    i.e. there is an opportunity cost to holding a stalled position.  If
    the signal queue is empty, the position should be held regardless of
    how long it has been open.

    Handles the case where signals.db does not yet exist (first run with no
    signals scanned): returns 0 so the time-exit gate opens only when real
    opportunity cost exists.
    """
    try:
        with sqlite3.connect(signals_db) as conn:
            result = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE processed IS NOT 1"
            ).fetchone()
        return int(result[0])
    except sqlite3.OperationalError:
        # Table does not exist (no signals have ever been generated)
        return 0


def get_open_risk_pct(risk_db: str, portfolio_value: float) -> float:
    """Return current total open risk as a percentage of portfolio value.

    PM-10: the time-based exit only fires when open risk has reached the
    max_open_risk_pct cap, meaning the stalled position is consuming risk
    budget that a better setup in the signal queue could use.

    Returns 0.0 if portfolio_value is zero or negative (guards against
    division by zero on a misconfigured stub value).
    """
    if portfolio_value <= 0.0:
        return 0.0
    with sqlite3.connect(risk_db) as conn:
        result = conn.execute(
            "SELECT COALESCE(SUM(risk_amount), 0.0) FROM risk_positions WHERE status = 'open'"
        ).fetchone()
    return round(float(result[0]) / portfolio_value * 100.0, 4)
