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
# Boolean fields (strategy_a_fired, strategy_b_fired, near_52wk_high) are
# stored as INTEGER (0/1) because SQLite has no native boolean type.
# ---------------------------------------------------------------------------

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

# Avoid a circular import at runtime — Signal is only needed for type hints
if TYPE_CHECKING:
    from .engine import Signal

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_timestamp    TEXT    NOT NULL,   -- ISO-8601 UTC, e.g. '2026-05-25T12:10:22+00:00'
    ticker              TEXT    NOT NULL,   -- Yahoo Finance ticker, e.g. 'ASML.AS'
    instrument_id       TEXT,               -- IBKR conid (same as ticker until IBKR integration)
    direction           TEXT,               -- 'long' (short out of scope for v1)
    entry_price         REAL,               -- last close at signal time
    stop_price          REAL,               -- technical stop, fixed at signal time
    target_price        REAL,               -- minimum 2:1 R:R target
    signal_type         TEXT,               -- 'pullback' | 'breakout' | 'pullback+breakout'
    liquidity_class     TEXT,               -- 'liquid' | 'thin'
    conviction          TEXT,               -- 'standard' | 'elevated'
    earnings_flag       INTEGER,            -- 1 = binary event imminent; NULL = unknown (stub)
    strategy_a_fired    INTEGER,            -- 1 if EMA Pullback strategy triggered
    strategy_b_fired    INTEGER,            -- 1 if Breakout strategy triggered
    near_52wk_high      INTEGER,            -- 1 if price within near_52wk_high_pct% of 52-wk high
    market_regime       TEXT,               -- 'bull' | 'bear' | 'unknown' at scan time
    rs_value            REAL                -- stock / benchmark ratio at signal time
)
"""

_INSERT_SIGNAL = """
INSERT INTO signals (
    signal_timestamp, ticker, instrument_id, direction,
    entry_price, stop_price, target_price, signal_type,
    liquidity_class, conviction, earnings_flag,
    strategy_a_fired, strategy_b_fired, near_52wk_high,
    market_regime, rs_value
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> None:
    """Create the signals table if it does not already exist.

    Safe to call on every startup — CREATE TABLE IF NOT EXISTS is idempotent.
    Also creates the parent directory if it does not exist yet (e.g. ./data/).
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE_SIGNALS)


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
            int(s.strategy_a_fired),   # bool → 1/0
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
