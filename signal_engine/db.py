# ---------------------------------------------------------------------------
# db.py
#
# SQLite persistence layer for the Signal Engine.
#
# Why SQLite (vs a flat file)?
#   - Signals are the primary audit trail of trading decisions.  A queryable
#     database lets you answer questions like "how many breakout signals fired
#     in Q1?" or "which tickers had elevated conviction last week?" without
#     parsing log files.
#   - Atomic writes via SQLite's WAL mode prevent partial writes if the
#     process crashes mid-scan.
#   - The file (signals.db) is kept separate from the main trading.db so
#     the signal history can be archived or queried independently.
#
# Boolean fields are stored as INTEGER (0/1) because SQLite has no native
# boolean type.
#
# Schema migration: init_db() creates the table on first run and adds any
# columns that exist in the schema but are missing from an older table on
# disk (forward-only migration via ALTER TABLE ADD COLUMN).
# ---------------------------------------------------------------------------

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Signal

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Full column definition used both for CREATE TABLE and for migration checks
_COLUMNS: list[tuple[str, str, str]] = [
    # (column_name, sql_type, inline_comment)
    ("id",                  "INTEGER PRIMARY KEY AUTOINCREMENT", ""),
    ("signal_timestamp",    "TEXT    NOT NULL",   "ISO-8601 UTC"),
    ("ticker",              "TEXT    NOT NULL",   "Yahoo Finance ticker, e.g. 'ASML.AS'"),
    ("instrument_id",       "TEXT",               "IBKR conid (same as ticker until IBKR integration)"),
    ("direction",           "TEXT",               "'long' (short out of scope for v1)"),
    ("entry_price",         "REAL",               "last close at signal time"),
    ("stop_price",          "REAL",               "technical stop, fixed at signal time"),
    ("target_price",        "REAL",               "minimum 2:1 R:R target"),
    ("signal_type",         "TEXT",               "'pullback' | 'breakout' | 'pullback+breakout'"),
    ("liquidity_class",     "TEXT",               "'liquid' | 'thin'"),
    ("conviction",          "TEXT",               "'standard' | 'elevated'"),
    ("earnings_flag",       "INTEGER",            "1 = binary event imminent; NULL = unknown (stub)"),
    ("stop_capped",         "INTEGER",            "1 when hard cap was applied to the stop distance"),
    ("strategy_a_fired",    "INTEGER",            "1 if EMA Pullback strategy triggered"),
    ("strategy_b_fired",    "INTEGER",            "1 if Breakout strategy triggered"),
    ("near_52wk_high",      "INTEGER",            "1 if price within near_52wk_high_pct% of 52-wk high"),
    ("market_regime",       "TEXT",               "'bull' | 'bear' | 'unknown' at scan time"),
    ("rs_value",            "REAL",               "stock / benchmark ratio at signal time"),
]

# Derive the CREATE TABLE statement from _COLUMNS so schema and migration
# are always kept in sync from a single source of truth
_MIGRATABLE_COLS = [c for c in _COLUMNS if "PRIMARY KEY" not in c[1]]

_CREATE_SIGNALS = (
    "CREATE TABLE IF NOT EXISTS signals (\n"
    + ",\n".join(
        f"    {name:20s} {sql_type}"
        for name, sql_type, _ in _COLUMNS
    )
    + "\n)"
)

_INSERT_SIGNAL = """
INSERT INTO signals (
    signal_timestamp, ticker, instrument_id, direction,
    entry_price, stop_price, target_price, signal_type,
    liquidity_class, conviction, earnings_flag, stop_capped,
    strategy_a_fired, strategy_b_fired, near_52wk_high,
    market_regime, rs_value
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> None:
    """Create the signals table if it does not exist, then add any new columns.

    Safe to call on every startup.  New columns (e.g. stop_capped added in v2)
    are added via ALTER TABLE ADD COLUMN so existing data is preserved across
    schema versions.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE_SIGNALS)

        # Forward migration: add any column present in the schema but missing
        # from an older on-disk table (e.g. upgrading from v1 to v2)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
        for name, sql_type, _ in _MIGRATABLE_COLS:
            if name not in existing:
                # Strip NOT NULL / default constraints — ALTER TABLE ADD COLUMN
                # does not support them in SQLite; new rows get NULL by default
                base_type = sql_type.split()[0]
                conn.execute(f"ALTER TABLE signals ADD COLUMN {name} {base_type}")


def save_signals(signals: list["Signal"], db_path: str) -> int:
    """Persist a list of Signal objects to the database.

    Uses executemany for efficiency — all rows are inserted in a single
    transaction so either all succeed or none do (atomicity).

    Returns the number of rows inserted (0 if signals list is empty).
    """
    if not signals:
        return 0

    rows = [
        (
            s.signal_timestamp.isoformat(),
            s.ticker,
            s.instrument_id,
            s.direction,
            s.entry_price,
            s.stop_price,
            s.target_price,
            s.signal_type,
            s.liquidity_class,
            s.conviction,
            None if s.earnings_flag is None else int(s.earnings_flag),
            int(s.stop_capped),
            int(s.strategy_a_fired),
            int(s.strategy_b_fired),
            int(s.near_52wk_high),
            s.market_regime,
            s.rs_value,
        )
        for s in signals
    ]

    with sqlite3.connect(db_path) as conn:
        conn.executemany(_INSERT_SIGNAL, rows)

    return len(rows)
