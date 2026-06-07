# ---------------------------------------------------------------------------
# executor.py — SimExecutor: main processing loop for the Sim Executor
#
# Responsibilities:
#   - Fetch unprocessed signals from signals.db
#   - Pass each signal through the real RiskLayer.evaluate() — no mocking
#   - On approval: record the fill (via fills.py) and send a notification
#   - On rejection: log the reason; notify only for system-level conditions
#   - Mark each signal processed = 1 in signals.db after handling
#
# Pipeline position:
#   Signal Engine → signals.db → SimExecutor → risk.db → Position Manager
#
# Two operating modes:
#   Batch (default) — processes all pending signals once and exits.
#       Intended for the daily EOD run: after the Signal Engine scan
#       completes, SX is invoked once to work through the day's output.
#
#   Watch (--watch) — polls signals.db on a configurable interval.
#       Intended for future intraday use where signals arrive continuously.
#       Building it now avoids a rewrite later; the poll interval is
#       configured via sim_executor.watch_poll_seconds in config.yaml.
#
# Idempotency:
#   Every signal is marked processed = 1 after handling — whether approved,
#   rejected, or unparseable.  Re-running SX on the same database will find
#   no unprocessed rows and exit cleanly.  This makes the daily run safe to
#   retry after a partial failure.
#
# Dry-run mode (--dry-run):
#   Evaluates all signals through the Risk Layer and logs outcomes, but does
#   not write positions to risk.db, does not send Telegram notifications, and
#   does not mark signals as processed.  Useful for verifying that a day's
#   signals would pass risk checks before committing anything to the database.
#   Because signals are not marked processed, a dry run can be followed by a
#   real run that processes the same signals.
#
# Rejection notification policy:
#   Most rejections (duplicate_instrument, open_risk_cap_exceeded, etc.) are
#   routine risk budget management and are logged to file only.  Two codes
#   indicate system-level conditions that the operator should investigate:
#
#       daily_loss_limit_breached  — the account has lost more than the
#           configured daily loss limit today.  Trading is now paused for
#           the remainder of the session.
#
#       trading_paused:daily_loss_limit_reached  — the pause flag from a
#           prior breach is still active.  This fires if SX is re-run
#           within the same calendar day after a limit breach.
#
#   For these two, a Telegram alert is sent so the operator is aware before
#   the next session begins.
# ---------------------------------------------------------------------------

import sqlite3
import time
from datetime import datetime, timezone
from logging import Logger

from risk_layer.layer import RiskLayer
from risk_layer import state as st
from signal_engine.engine import Signal

from .fills import record_fill
from .notify import send_fill_notification, send_system_alert

# These two rejection codes indicate system-level conditions, not routine
# per-signal risk budget decisions.  They are the only rejections that
# trigger a Telegram notification (see module docstring above).
_SYSTEM_LEVEL_REJECTIONS = frozenset({
    "daily_loss_limit_breached",
    "trading_paused:daily_loss_limit_reached",
})


# ---------------------------------------------------------------------------
# SimExecutor
# ---------------------------------------------------------------------------

class SimExecutor:
    """Reads unprocessed signals from signals.db, passes them through the Risk
    Layer, and records approved fills in risk.db.

    This class is the thin orchestrator — it delegates fill logic to fills.py
    and notification formatting to notify.py so those modules can be reused
    by the real Order Executor without modification.

    Typical call sequence:
        executor = SimExecutor(config, logger)
        executor.run_batch()          # or run_watch() for continuous mode
    """

    def __init__(self, config: dict, logger: Logger, dry_run: bool = False) -> None:
        self._logger = logger
        self._dry_run = dry_run

        self._signals_db: str = config["signal_engine"]["db_path"]
        self._tob_pct: float = config["costs"]["tob_pct"]
        self._poll_seconds: int = config.get("sim_executor", {}).get("watch_poll_seconds", 10)

        # Telegram notifications are enabled only when both token and chat_id
        # are non-empty.  An empty token means notifications are intentionally
        # off — no warning is logged.  Any send failure after this point
        # (network error, bad token format, etc.) is logged at WARNING.
        notif = config.get("notifications", {})
        self._bot_token: str = notif.get("telegram_bot_token", "") or ""
        self._chat_id: str = notif.get("telegram_chat_id", "") or ""
        self._notify_enabled: bool = bool(self._bot_token and self._chat_id)

        # Initialise risk.db schema (forward migration adds any new columns to
        # an existing database without touching existing rows).
        st.init_db(config["risk"]["db_path"])
        self._risk_layer = RiskLayer(config, logger)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run_batch(self) -> None:
        """Process all unprocessed signals in signal_timestamp order and exit.

        This is the normal daily use case: called once after the Signal Engine
        scan completes.  Processes every pending signal, then logs a summary.
        """
        rows = self._fetch_unprocessed()
        if not rows:
            self._logger.info({"event": "batch_complete", "processed": 0, "dry_run": self._dry_run})
            return

        for row in rows:
            self._process_row(row)

        self._logger.info({
            "event": "batch_complete",
            "processed": len(rows),
            "dry_run": self._dry_run,
        })

    def run_watch(self) -> None:
        """Poll signals.db continuously and process new signals as they arrive.

        Polls every watch_poll_seconds (config: sim_executor.watch_poll_seconds).
        If no unprocessed signals are found on a given poll, the loop sleeps
        and tries again — it does not exit.  Intended for future intraday use;
        in EOD-only operation use run_batch() instead.
        """
        self._logger.info({
            "event": "watch_mode_started",
            "poll_seconds": self._poll_seconds,
            "dry_run": self._dry_run,
        })
        while True:
            rows = self._fetch_unprocessed()
            for row in rows:
                self._process_row(row)
            time.sleep(self._poll_seconds)

    # -----------------------------------------------------------------------
    # Private
    # -----------------------------------------------------------------------

    def _fetch_unprocessed(self) -> list:
        """Return all signals not yet handled, ordered by signal rank.

        Uses IS NOT 1 rather than = 0 so that legacy rows with a NULL
        processed column (from a signals.db populated before this column was
        added) are treated as unprocessed.  NULL and 0 both mean "not yet
        handled".
        """
        with sqlite3.connect(self._signals_db) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """SELECT * FROM signals WHERE processed IS NOT 1
                   ORDER BY COALESCE(signal_rank, 99999), signal_timestamp"""
            ).fetchall()

    def _process_row(self, row: sqlite3.Row) -> None:
        """Evaluate one signal row through the full Risk Layer pipeline.

        Processing steps:
            1. Reconstruct a Signal dataclass from the DB row
            2. Pass the Signal to RiskLayer.evaluate()
            3. If approved: record the fill and (optionally) notify
            4. If rejected: log the reason; alert for system-level codes
            5. Mark the signal processed = 1 (idempotency guard)

        A parse error on step 1 marks the signal processed and moves on —
        a malformed row should not block every subsequent signal in the batch.
        """
        # -------------------------------------------------------------------
        # Step 1 — Reconstruct Signal from DB row
        #
        # Any missing or invalid field raises an exception here.  We catch
        # it, log a warning, mark the row processed, and continue.  Leaving
        # the row unprocessed would cause it to be re-evaluated on every
        # subsequent run, which is unhelpful if the data is genuinely corrupt.
        # -------------------------------------------------------------------
        try:
            signal = _row_to_signal(row)
        except Exception as exc:
            self._logger.warning({
                "event": "signal_parse_error",
                "signal_id": row["id"],
                "ticker": row["ticker"],
                "error": str(exc),
            })
            if not self._dry_run:
                self._mark_processed(row["id"])
            return

        # -------------------------------------------------------------------
        # Step 2 — Risk Layer evaluation
        #
        # The real RiskLayer.evaluate() is called — no mocking, no shortcuts.
        # This is the entire point of SX: to exercise the live risk logic and
        # produce a realistic position state before the real executor exists.
        # -------------------------------------------------------------------
        decision = self._risk_layer.evaluate(signal)

        # -------------------------------------------------------------------
        # Step 3 — Fill recording and notification (approved signals)
        # -------------------------------------------------------------------
        if decision.approved:
            if not self._dry_run:
                record_fill(self._risk_layer, decision, self._tob_pct, self._logger)
                if self._notify_enabled:
                    send_fill_notification(decision, self._bot_token, self._chat_id, self._logger)
            self._logger.info({
                "event": "sim_fill",
                "ticker": signal.ticker,
                "shares": decision.shares,
                "fill_price": signal.entry_price,
                "risk_amount": decision.risk_amount,
                "position_risk_pct": decision.position_risk_pct,
                "projected_open_risk_pct": decision.projected_open_risk_pct,
                "dry_run": self._dry_run,
            })

        # -------------------------------------------------------------------
        # Step 4 — Rejection handling
        #
        # Routine rejections (duplicate, cap exceeded, zero shares) are logged
        # to file only — too noisy for Telegram during normal operation.
        #
        # System-level rejections indicate that trading has been halted for
        # the session.  These are sent as Telegram alerts so the operator
        # can review the account before the next day's run.
        # -------------------------------------------------------------------
        else:
            self._logger.info({
                "event": "signal_rejected",
                "ticker": signal.ticker,
                "reason": decision.reject_reason,
                "dry_run": self._dry_run,
            })
            if decision.reject_reason in _SYSTEM_LEVEL_REJECTIONS:
                if self._notify_enabled and not self._dry_run:
                    send_system_alert(
                        f"⚠️ Trading paused: {decision.reject_reason} (signal: {signal.ticker})",
                        self._bot_token,
                        self._chat_id,
                        self._logger,
                    )

        # -------------------------------------------------------------------
        # Step 5 — Mark processed
        #
        # Done last, after all side effects complete, so that a crash mid-
        # step leaves the signal unprocessed and eligible for a safe retry.
        # In dry-run mode we intentionally skip this so the same signals can
        # be re-evaluated in a subsequent real run.
        # -------------------------------------------------------------------
        if not self._dry_run:
            self._mark_processed(row["id"])

    def _mark_processed(self, signal_id: int) -> None:
        with sqlite3.connect(self._signals_db) as conn:
            conn.execute("UPDATE signals SET processed = 1 WHERE id = ?", (signal_id,))


# ---------------------------------------------------------------------------
# Signal reconstruction from DB row
# ---------------------------------------------------------------------------

def _row_to_signal(row: sqlite3.Row) -> Signal:
    """Reconstruct a Signal dataclass from a signals.db row.

    Handles two SQLite quirks:
      - Timestamps: signal_timestamp is stored as a naive ISO string.  If
        tzinfo is absent (rows written by older SE versions before timezone-
        aware timestamps were standardised), we attach UTC explicitly so the
        Risk Layer always receives a timezone-aware datetime.
      - Booleans: SQLite has no native boolean type.  The Signal Engine stores
        True/False as INTEGER 1/0.  Python's bool() converts 1 → True and
        0 → False correctly.  earnings_flag also allows NULL (not yet
        resolved), so it requires an explicit None check before conversion.
    """
    ts_str = row["signal_timestamp"]
    ts = datetime.fromisoformat(ts_str)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    # earnings_flag: NULL = unknown (IBKR stub); 0/1 = resolved boolean
    earnings_raw = row["earnings_flag"]
    earnings_flag = None if earnings_raw is None else bool(earnings_raw)

    return Signal(
        instrument_id=row["instrument_id"] or row["ticker"],  # ticker is the Phase 1 stub
        ticker=row["ticker"],
        direction=row["direction"] or "long",
        entry_price=row["entry_price"],
        stop_price=row["stop_price"],
        target_price=row["target_price"],
        signal_type=row["signal_type"],
        liquidity_class=row["liquidity_class"],
        conviction=row["conviction"],
        signal_timestamp=ts,
        earnings_flag=earnings_flag,
        stop_capped=bool(row["stop_capped"]),
        swing_low_stop=row["swing_low_stop"],
        atr_stop=row["atr_stop"],
        stop_method=row["stop_method"],
        strategy_a_fired=bool(row["strategy_a_fired"]),
        strategy_b_fired=bool(row["strategy_b_fired"]),
        near_52wk_high=bool(row["near_52wk_high"]),
        market_regime=row["market_regime"] or "unknown",
        rs_value=row["rs_value"],
        # run_type: NULL in rows written before this column was added;
        # default to 'eod' since all existing rows are EOD signals.
        run_type=row["run_type"] or "eod",
        signal_rank=int(row["signal_rank"] or 0),
    )
