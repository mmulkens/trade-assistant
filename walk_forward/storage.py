# ---------------------------------------------------------------------------
# storage.py — SQLite schema and CRUD for the Walk-Forward Simulator
#
# Database: wf_sim.db (path from config walk_forward.wf_db_path)
#
# Tables created here (WF-specific):
#   wf_runs         — one row per simulation run (metadata + outcome)
#   wf_positions    — one row per trade (entry → exit)
#   wf_signals      — one row per signal processed, with action taken
#   wf_equity_curve — daily portfolio value snapshot
#
# Risk Layer tables (risk_positions, system_state) are created in the same
# wf_sim.db by calling risk_layer.state.init_db(wf_db_path) at runner startup.
# This keeps the whole simulation in a single file (Option C isolation design).
#
# All writes use explicit transactions; callers do not manage connections.
# ---------------------------------------------------------------------------

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

_CREATE_WF_RUNS = """
CREATE TABLE IF NOT EXISTS wf_runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    sim_start       TEXT NOT NULL,
    sim_end         TEXT NOT NULL,
    portfolio_start REAL NOT NULL,
    portfolio_end   REAL,
    total_trades    INTEGER,
    notes           TEXT
)
"""

_CREATE_WF_POSITIONS = """
CREATE TABLE IF NOT EXISTS wf_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    entry_date      TEXT NOT NULL,
    exit_date       TEXT,
    entry_price     REAL NOT NULL,
    exit_price      REAL,
    stop_price      REAL NOT NULL,
    shares          INTEGER NOT NULL,
    risk_amount     REAL NOT NULL,
    entry_commission REAL,
    exit_commission  REAL,
    gross_pnl       REAL,
    net_pnl         REAL,
    exit_reason     TEXT,
    gap_filled      INTEGER DEFAULT 0,
    signal_type     TEXT,
    conviction      TEXT,
    FOREIGN KEY (run_id) REFERENCES wf_runs(run_id)
)
"""

_CREATE_WF_SIGNALS = """
CREATE TABLE IF NOT EXISTS wf_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    conviction  TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_price  REAL NOT NULL,
    stop_pct    REAL,
    target_price REAL,
    stop_type   TEXT,
    signal_rank INTEGER NOT NULL,
    action      TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES wf_runs(run_id)
)
"""

_CREATE_WF_EQUITY = """
CREATE TABLE IF NOT EXISTS wf_equity_curve (
    run_id          TEXT NOT NULL,
    date            TEXT NOT NULL,
    portfolio_value REAL NOT NULL,
    open_positions  INTEGER NOT NULL,
    PRIMARY KEY (run_id, date),
    FOREIGN KEY (run_id) REFERENCES wf_runs(run_id)
)
"""


def init_db(db_path: str) -> None:
    """Create all WF-specific tables if they do not exist, then forward-migrate."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE_WF_RUNS)
        conn.execute(_CREATE_WF_POSITIONS)
        conn.execute(_CREATE_WF_SIGNALS)
        conn.execute(_CREATE_WF_EQUITY)

        # Forward migration: add new columns to existing tables without touching data
        existing_pos = {row[1] for row in conn.execute("PRAGMA table_info(wf_positions)")}
        if "gap_filled" not in existing_pos:
            conn.execute("ALTER TABLE wf_positions ADD COLUMN gap_filled INTEGER DEFAULT 0")

        existing_sig = {row[1] for row in conn.execute("PRAGMA table_info(wf_signals)")}
        if "stop_pct" not in existing_sig:
            conn.execute("ALTER TABLE wf_signals ADD COLUMN stop_pct REAL")
        if "target_price" not in existing_sig:
            conn.execute("ALTER TABLE wf_signals ADD COLUMN target_price REAL")
        if "stop_type" not in existing_sig:
            conn.execute("ALTER TABLE wf_signals ADD COLUMN stop_type TEXT")

        conn.commit()


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

def create_run(
    db_path: str,
    sim_start: str,
    sim_end: str,
    portfolio_start: float,
    notes: Optional[str] = None,
) -> str:
    """Insert a new simulation run record. Returns the generated run_id (UUID)."""
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO wf_runs (run_id, started_at, sim_start, sim_end,
                                 portfolio_start, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, started_at, sim_start, sim_end, portfolio_start, notes),
        )
        conn.commit()
    return run_id


def close_run(
    db_path: str,
    run_id: str,
    portfolio_end: float,
    total_trades: int,
) -> None:
    """Update a run record when the simulation completes."""
    ended_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE wf_runs
               SET ended_at = ?, portfolio_end = ?, total_trades = ?
             WHERE run_id = ?
            """,
            (ended_at, portfolio_end, total_trades, run_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Position records
# ---------------------------------------------------------------------------

def record_position_open(
    db_path: str,
    run_id: str,
    ticker: str,
    entry_date: str,
    entry_price: float,
    stop_price: float,
    shares: int,
    risk_amount: float,
    entry_commission: float,
    signal_type: str,
    conviction: str,
) -> int:
    """Insert an open position record. Returns the row id for later update."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO wf_positions
                (run_id, ticker, entry_date, entry_price, stop_price, shares,
                 risk_amount, entry_commission, signal_type, conviction)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, ticker, entry_date, entry_price, stop_price, shares,
             risk_amount, entry_commission, signal_type, conviction),
        )
        conn.commit()
        return cur.lastrowid


def record_position_close(
    db_path: str,
    position_db_id: int,
    exit_date: str,
    exit_price: float,
    exit_commission: float,
    gross_pnl: float,
    net_pnl: float,
    exit_reason: str,
    gap_filled: bool = False,
) -> None:
    """Update a position record when it closes."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE wf_positions
               SET exit_date = ?, exit_price = ?, exit_commission = ?,
                   gross_pnl = ?, net_pnl = ?, exit_reason = ?, gap_filled = ?
             WHERE id = ?
            """,
            (exit_date, exit_price, exit_commission, gross_pnl, net_pnl,
             exit_reason, int(gap_filled), position_db_id),
        )
        conn.commit()


def update_position_stop(db_path: str, position_db_id: int, new_stop: float) -> None:
    """Advance the stop price on an open wf_positions record (trail update)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE wf_positions SET stop_price = ? WHERE id = ?",
            (new_stop, position_db_id),
        )
        conn.commit()


def get_open_positions(db_path: str, run_id: str) -> list[dict]:
    """Return all open (exit_date IS NULL) positions for a run."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM wf_positions WHERE run_id = ? AND exit_date IS NULL",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Signal records
# ---------------------------------------------------------------------------

def record_signal(
    db_path: str,
    run_id: str,
    signal_date: str,
    ticker: str,
    signal_type: str,
    conviction: str,
    entry_price: float,
    stop_price: float,
    stop_pct: float | None,
    target_price: float | None,
    stop_type: str | None,
    signal_rank: int,
    action: str,
) -> None:
    """Record a signal and the action taken (entered, skipped, etc.)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO wf_signals
                (run_id, signal_date, ticker, signal_type, conviction,
                 entry_price, stop_price, stop_pct, target_price, stop_type,
                 signal_rank, action)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, signal_date, ticker, signal_type, conviction,
             entry_price, stop_price, stop_pct, target_price, stop_type,
             signal_rank, action),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------

def record_equity(
    db_path: str,
    run_id: str,
    date: str,
    portfolio_value: float,
    open_positions: int,
) -> None:
    """Append a daily equity curve snapshot."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO wf_equity_curve
                (run_id, date, portfolio_value, open_positions)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, date, portfolio_value, open_positions),
        )
        conn.commit()


def get_equity_curve(db_path: str, run_id: str) -> list[dict]:
    """Return the full equity curve for a run, ordered by date."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT date, portfolio_value, open_positions
              FROM wf_equity_curve
             WHERE run_id = ?
             ORDER BY date ASC
            """,
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_closed_positions(db_path: str, run_id: str) -> list[dict]:
    """Return all closed positions for a run, ordered by exit_date."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM wf_positions
             WHERE run_id = ? AND exit_date IS NOT NULL
             ORDER BY exit_date ASC
            """,
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]
