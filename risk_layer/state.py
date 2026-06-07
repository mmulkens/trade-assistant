# ---------------------------------------------------------------------------
# state.py — SQLite persistence for the Risk Layer
#
# Why SQLite (not a flat file)?
#   The Risk Layer needs to survive process restarts.  If the bot crashes and
#   restarts mid-session, it must still know which positions are open and how
#   much risk is already committed — otherwise it could open duplicate trades
#   or breach the 6% hard cap without realising it.  SQLite gives us atomic
#   writes, queryable history, and a zero-dependency file-based store.
#
# Two tables:
#   risk_positions  — one row per trade; status 'open' or 'closed'
#   system_state    — key/value store for persistent flags (e.g. trading pause)
#
# The pause flag uses a date-scoped approach: the value stored is the date on
# which the pause was triggered.  If that date is today, trading is paused.
# If today is a new calendar day, the flag is automatically stale and is
# treated as "not paused" — no cron job or explicit reset is needed for the
# normal case (though the CLI offers 'unpause' for manual overrides).
#
# Schema migration follows the same forward-only pattern as signal_engine/db.py:
# init_db() adds any columns present in _COLUMNS but missing from an older
# on-disk table via ALTER TABLE ADD COLUMN, so existing data is preserved
# across schema updates without destructive migrations.
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
    ("risk_amount",             "REAL    NOT NULL"),    # shares × risk_per_share
    ("position_risk_pct",       "REAL    NOT NULL"),    # risk_amount / portfolio_value × 100
    ("portfolio_value_at_open", "REAL    NOT NULL"),    # snapshot used for sizing
    ("liquidity_class",         "TEXT"),                # 'liquid' | 'thin'
    ("signal_type",             "TEXT"),                # 'pullback' | 'breakout' | 'pullback+breakout'
    ("conviction",              "TEXT"),                # 'standard' | 'elevated'
    ("status",                  "TEXT    NOT NULL"),    # 'open' | 'closed'
    ("opened_at",               "TEXT    NOT NULL"),    # ISO-8601 UTC
    ("closed_at",               "TEXT"),
    ("close_price",             "REAL"),
    ("realized_pnl",            "REAL"),
    ("close_reason",            "TEXT"),                # 'stop' | 'target' | 'trail' | 'manual'
    # --- Fill details (set by Order Executor / SX at open time) ---
    ("target_price",            "REAL"),                # signal target; stored for PM reference
    ("isin",                    "TEXT"),                # ISIN — None until IBKR integration
    ("run_type",                "TEXT"),                # 'eod' | 'intraday'
    ("fill_price",              "REAL"),                # actual fill; = entry_price for SX
    ("fill_timestamp",          "TEXT"),                # UTC ISO-8601 timestamp of fill
    ("entry_commission",        "REAL"),                # TOB flat estimate at entry
    ("bot_initiated",           "INTEGER"),             # 1 = opened by the bot (SX or live executor)
    # --- Exit details (set by Position Manager or manual close) ---
    ("exit_commission",         "REAL"),                # TOB at exit
    ("exit_note",               "TEXT"),                # free-text close annotation
    # --- Position tracking (updated continuously by Position Manager) ---
    ("peak_price",              "REAL"),                # highest price seen; init = fill_price
    ("trail_triggered",         "INTEGER"),             # 1 once trailing stop is active
    ("trail_trigger_price",     "REAL"),                # price level that activated the trail
    ("gross_pnl",               "REAL"),                # (exit_price - entry_price) × shares
    ("net_pnl",                 "REAL"),                # gross_pnl - entry_commission - exit_commission
]

# Columns without a PRIMARY KEY constraint can be added by forward migration
_MIGRATABLE_POSITION_COLS = [c for c in _POSITION_COLUMNS if "PRIMARY KEY" not in c[1]]

_CREATE_POSITIONS = (
    "CREATE TABLE IF NOT EXISTS risk_positions (\n"
    + ",\n".join(f"    {name:28s} {sql_type}" for name, sql_type in _POSITION_COLUMNS)
    + "\n)"
)

# Key/value store for session-level flags.  Values are always TEXT.
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
    liquidity_class, signal_type, conviction, status, opened_at,
    target_price, run_type, fill_price, fill_timestamp,
    entry_commission, bot_initiated, peak_price, trail_triggered
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> None:
    """Create tables if absent; add any new columns to existing tables.

    Safe to call on every startup — idempotent.  New columns added to
    _POSITION_COLUMNS in future schema versions are automatically added to
    an existing on-disk table without touching any existing data.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE_POSITIONS)
        conn.execute(_CREATE_SYSTEM_STATE)

        # Forward migration: add any column in the schema but not yet on disk
        existing = {row[1] for row in conn.execute("PRAGMA table_info(risk_positions)")}
        for name, sql_type in _MIGRATABLE_POSITION_COLS:
            if name not in existing:
                # ALTER TABLE ADD COLUMN does not support NOT NULL or defaults in SQLite;
                # strip to bare type — new rows get NULL for the added column
                base_type = sql_type.split()[0]
                conn.execute(f"ALTER TABLE risk_positions ADD COLUMN {name} {base_type}")


# ---------------------------------------------------------------------------
# Position write operations
# ---------------------------------------------------------------------------

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
    target_price: Optional[float] = None,
    run_type: Optional[str] = None,
    fill_price: Optional[float] = None,
    fill_timestamp: Optional[str] = None,
    entry_commission: Optional[float] = None,
    bot_initiated: bool = False,
    peak_price: Optional[float] = None,
) -> int:
    """Insert a new open position. Returns the new row id.

    Called by RiskLayer.open_position() after the Order Executor confirms
    a fill — not on signal approval, because an approved signal may still
    fail to fill (order rejected, session closed, etc.).
    """
    opened_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            _INSERT_POSITION,
            (
                ticker, instrument_id, entry_price, stop_price, shares,
                risk_per_share, risk_amount, position_risk_pct,
                portfolio_value_at_open, liquidity_class, signal_type,
                conviction, "open", opened_at,
                target_price, run_type, fill_price, fill_timestamp,
                entry_commission, int(bot_initiated),
                peak_price if peak_price is not None else fill_price,
                0,  # trail_triggered: always False at open
            ),
        )
        return cur.lastrowid


def close_position(
    ticker: str,
    close_price: float,
    reason: str,
    db_path: str,
    closed_at: Optional[str] = None,
) -> bool:
    """Mark the most recent open position for `ticker` as closed.

    Calculates and stores realised P&L = (close_price − entry_price) × shares.
    A negative value is a loss.

    Returns True if a matching open position was found and updated.
    Returns False if no open position exists for this ticker — the caller
    (RiskLayer.close_position) logs a warning in that case.

    `closed_at` defaults to the current UTC time.  Pass an ISO-format date
    string (e.g. "2025-04-29T00:00:00+00:00") in walk-forward simulation so
    that daily P&L calculations use the simulated date, not the real wall-clock
    date — otherwise get_daily_realized_pnl() accumulates all sim-day closes
    into a single real day and fires the daily loss limit permanently.

    Why ORDER BY opened_at DESC LIMIT 1?
        Under normal operation there is only one open position per ticker
        (the duplicate check in evaluate() prevents pyramiding).  The ORDER BY
        is a safety net in case state is corrected manually and a second row
        somehow exists — we always close the most recently opened one.
    """
    closed_at = closed_at or datetime.now(timezone.utc).isoformat()
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


# ---------------------------------------------------------------------------
# Position read operations
# ---------------------------------------------------------------------------

def get_open_positions(db_path: str) -> list[dict]:
    """Return all open positions as a list of dicts (one per row)."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM risk_positions WHERE status = 'open' ORDER BY opened_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_risk_amount(db_path: str) -> float:
    """Sum of risk_amount across all open positions (in portfolio currency).

    This is the key input to the RL-02 total open risk check.
    COALESCE(…, 0.0) ensures we get 0 rather than NULL when no rows exist.
    """
    with sqlite3.connect(db_path) as conn:
        result = conn.execute(
            "SELECT COALESCE(SUM(risk_amount), 0.0) FROM risk_positions WHERE status = 'open'"
        ).fetchone()
    return float(result[0])


def has_open_position(ticker: str, db_path: str) -> bool:
    """Return True if there is already an open position for this ticker (RL-07).

    Used to enforce the duplicate / anti-pyramiding check before accepting
    a new signal.  A stock that is already held cannot be entered again.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM risk_positions WHERE ticker = ? AND status = 'open' LIMIT 1",
            (ticker,),
        ).fetchone()
    return row is not None


def get_daily_realized_pnl(db_path: str, for_date: Optional[date] = None) -> float:
    """Sum of realised P&L for all positions closed on `for_date` (default: today).

    Used to enforce the daily loss limit (RL-06).

    Phase 1 limitation: this only covers realised P&L from positions that
    were fully closed today.  Unrealised intraday losses on open positions
    are not captured until IBKR live pricing is wired up.  The Phase 1
    implementation is conservative — it can only trigger a pause after
    a loss is locked in, not preemptively.
    """
    target = (for_date or date.today()).isoformat()
    with sqlite3.connect(db_path) as conn:
        result = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0.0) FROM risk_positions "
            "WHERE status = 'closed' AND DATE(closed_at) = ?",
            (target,),
        ).fetchone()
    return float(result[0])


# ---------------------------------------------------------------------------
# Trading pause (RL-06 — daily loss limit)
# ---------------------------------------------------------------------------

_PAUSE_KEY = "trading_paused_date"


def is_trading_paused(db_path: str) -> bool:
    """Return True if the daily loss limit was triggered today.

    The pause value is the ISO date on which the limit was breached.
    Comparing against today means the pause automatically expires at midnight
    — the next session starts fresh with no manual intervention required.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (_PAUSE_KEY,)
        ).fetchone()
    if row is None:
        return False
    return row[0] == date.today().isoformat()


def set_trading_pause(db_path: str) -> None:
    """Record today's date as the pause trigger date.

    ON CONFLICT DO UPDATE (UPSERT) is used instead of INSERT + UPDATE because:
    - If no pause row exists yet, we need to INSERT.
    - If a pause was already set earlier today (e.g. the limit function is
      called twice in the same session), we update in-place rather than error.
    """
    today = date.today().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO system_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_PAUSE_KEY, today),
        )


def clear_trading_pause(db_path: str) -> None:
    """Remove the pause flag entirely.

    Used by the CLI 'unpause' command to override the daily loss limit
    manually — for example, if the loss was caused by a data error or a
    position that was manually corrected in the broker account.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM system_state WHERE key = ?", (_PAUSE_KEY,))
