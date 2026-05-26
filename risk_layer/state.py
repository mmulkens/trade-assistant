# ---------------------------------------------------------------------------
# state.py — SQLite persistence for the Risk Layer
#
# Two tables:
#   risk_positions  — one row per trade, status 'open' or 'closed'
#   system_state    — key/value store for flags (e.g. trading_paused_date)
#
# Schema migration follows the same forward-only pattern as signal_engine/db.py:
# init_db() adds any columns present in _COLUMNS but missing from an older
# on-disk table via ALTER TABLE ADD COLUMN.
# ---------------------------------------------------------------------------

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_POSITION_COLUMNS: list[tuple[str, str]] = [
    ("id",                      "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("ticker",                  "TEXT    NOT NULL"),
    ("instrument_id",           "TEXT    NOT NULL"),
    ("entry_price",             "REAL    NOT NULL"),
    ("stop_price",              "REAL    NOT NULL"),
    ("shares",                  "INTEGER NOT NULL"),
    ("risk_per_share",          "REAL    NOT NULL"),
    ("risk_amount",             "REAL    NOT NULL"),
    ("position_risk_pct",       "REAL    NOT NULL"),
    ("portfolio_value_at_open", "REAL    NOT NULL"),
    ("liquidity_class",         "TEXT"),
    ("signal_type",             "TEXT"),
    ("conviction",              "TEXT"),
    ("status",                  "TEXT    NOT NULL"),   # 'open' | 'closed'
    ("opened_at",               "TEXT    NOT NULL"),   # ISO-8601 UTC
    ("closed_at",               "TEXT"),
    ("close_price",             "REAL"),
    ("realized_pnl",            "REAL"),
    ("close_reason",            "TEXT"),               # 'stop' | 'target' | 'trail' | 'manual'
]

_MIGRATABLE_POSITION_COLS = [c for c in _POSITION_COLUMNS if "PRIMARY KEY" not in c[1]]

_CREATE_POSITIONS = (
    "CREATE TABLE IF NOT EXISTS risk_positions (\n"
    + ",\n".join(f"    {name:28s} {sql_type}" for name, sql_type in _POSITION_COLUMNS)
    + "\n)"
)

_CREATE_SYSTEM_STATE = """
CREATE TABLE IF NOT EXISTS system_state (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""

_INSERT_POSITION = """
INSERT INTO risk_positions (
    ticker, instrument_id, entry_price, stop_price, shares,
    risk_per_share, risk_amount, position_risk_pct, portfolio_value_at_open,
    liquidity_class, signal_type, conviction, status, opened_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> None:
    """Create tables if absent; add any new columns to existing tables.

    Safe to call on every startup.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE_POSITIONS)
        conn.execute(_CREATE_SYSTEM_STATE)

        existing = {row[1] for row in conn.execute("PRAGMA table_info(risk_positions)")}
        for name, sql_type in _MIGRATABLE_POSITION_COLS:
            if name not in existing:
                base_type = sql_type.split()[0]
                conn.execute(f"ALTER TABLE risk_positions ADD COLUMN {name} {base_type}")


def add_position(
    ticker: str,
    instrument_id: str,
    entry_price: float,
    stop_price: float,
    shares: int,
    risk_per_share: float,
    risk_amount: float,
    position_risk_pct: float,
    portfolio_value_at_open: float,
    liquidity_class: str,
    signal_type: str,
    conviction: str,
    db_path: str,
) -> int:
    """Insert a new open position. Returns the new row id."""
    opened_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            _INSERT_POSITION,
            (
                ticker, instrument_id, entry_price, stop_price, shares,
                risk_per_share, risk_amount, position_risk_pct,
                portfolio_value_at_open, liquidity_class, signal_type,
                conviction, "open", opened_at,
            ),
        )
        return cur.lastrowid


def close_position(
    ticker: str,
    close_price: float,
    reason: str,
    db_path: str,
) -> bool:
    """Mark the open position for `ticker` as closed and record realized P&L.

    P&L = (close_price - entry_price) × shares.
    Returns True if a row was updated, False if no open position was found.
    """
    closed_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, entry_price, shares FROM risk_positions "
            "WHERE ticker = ? AND status = 'open' ORDER BY opened_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if row is None:
            return False

        pos_id, entry_price, shares = row
        realized_pnl = round((close_price - entry_price) * shares, 2)

        conn.execute(
            """UPDATE risk_positions
               SET status = 'closed', closed_at = ?, close_price = ?,
                   realized_pnl = ?, close_reason = ?
               WHERE id = ?""",
            (closed_at, close_price, realized_pnl, reason, pos_id),
        )
    return True


def get_open_positions(db_path: str) -> list[dict]:
    """Return all open positions as a list of dicts."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM risk_positions WHERE status = 'open' ORDER BY opened_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_risk_amount(db_path: str) -> float:
    """Sum of risk_amount across all open positions."""
    with sqlite3.connect(db_path) as conn:
        result = conn.execute(
            "SELECT COALESCE(SUM(risk_amount), 0.0) FROM risk_positions WHERE status = 'open'"
        ).fetchone()
    return float(result[0])


def has_open_position(ticker: str, db_path: str) -> bool:
    """Return True if there is already an open position for this ticker."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM risk_positions WHERE ticker = ? AND status = 'open' LIMIT 1",
            (ticker,),
        ).fetchone()
    return row is not None


def get_daily_realized_pnl(db_path: str, for_date: Optional[date] = None) -> float:
    """Sum of realized P&L for positions closed on `for_date` (defaults to today)."""
    target = (for_date or date.today()).isoformat()
    with sqlite3.connect(db_path) as conn:
        result = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0.0) FROM risk_positions "
            "WHERE status = 'closed' AND DATE(closed_at) = ?",
            (target,),
        ).fetchone()
    return float(result[0])


# ---------------------------------------------------------------------------
# Trading pause (RL-06)
# ---------------------------------------------------------------------------

_PAUSE_KEY = "trading_paused_date"


def is_trading_paused(db_path: str) -> bool:
    """Return True if a pause was set today (daily loss limit reached)."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (_PAUSE_KEY,)
        ).fetchone()
    if row is None:
        return False
    return row[0] == date.today().isoformat()


def set_trading_pause(db_path: str) -> None:
    """Record today's date as the pause trigger date."""
    today = date.today().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO system_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_PAUSE_KEY, today),
        )


def clear_trading_pause(db_path: str) -> None:
    """Remove the pause flag (call at session start or manually via CLI)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM system_state WHERE key = ?", (_PAUSE_KEY,))
