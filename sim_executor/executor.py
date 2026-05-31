# ---------------------------------------------------------------------------
# executor.py — SimExecutor: reads signals, evaluates risk, records fills
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

_SYSTEM_LEVEL_REJECTIONS = frozenset({
    "daily_loss_limit_breached",
    "trading_paused:daily_loss_limit_reached",
})


class SimExecutor:
    """Reads unprocessed signals from signals.db, passes them through the Risk
    Layer, and records approved fills in risk.db.

    Two modes:
        run_batch()  — processes all pending signals once (daily EOD use)
        run_watch()  — polls for new signals on a configurable interval
    """

    def __init__(self, config: dict, logger: Logger, dry_run: bool = False) -> None:
        self._logger = logger
        self._dry_run = dry_run

        self._signals_db: str = config["signal_engine"]["db_path"]
        self._tob_pct: float = config["costs"]["tob_pct"]
        self._poll_seconds: int = config.get("sim_executor", {}).get("watch_poll_seconds", 10)

        notif = config.get("notifications", {})
        self._bot_token: str = notif.get("telegram_bot_token", "") or ""
        self._chat_id: str = notif.get("telegram_chat_id", "") or ""
        self._notify_enabled: bool = bool(self._bot_token and self._chat_id)

        st.init_db(config["risk"]["db_path"])
        self._risk_layer = RiskLayer(config, logger)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run_batch(self) -> None:
        """Process all unprocessed signals in signal_timestamp order."""
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
        """Poll signals.db and process new signals as they arrive."""
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
        with sqlite3.connect(self._signals_db) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM signals WHERE processed IS NOT 1 ORDER BY signal_timestamp"
            ).fetchall()

    def _process_row(self, row: sqlite3.Row) -> None:
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

        decision = self._risk_layer.evaluate(signal)

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

        if not self._dry_run:
            self._mark_processed(row["id"])

    def _mark_processed(self, signal_id: int) -> None:
        with sqlite3.connect(self._signals_db) as conn:
            conn.execute("UPDATE signals SET processed = 1 WHERE id = ?", (signal_id,))


# ---------------------------------------------------------------------------
# Signal reconstruction from DB row
# ---------------------------------------------------------------------------

def _row_to_signal(row: sqlite3.Row) -> Signal:
    """Reconstruct a Signal dataclass from a signals.db row."""
    ts_str = row["signal_timestamp"]
    ts = datetime.fromisoformat(ts_str)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    earnings_raw = row["earnings_flag"]
    earnings_flag = None if earnings_raw is None else bool(earnings_raw)

    return Signal(
        instrument_id=row["instrument_id"] or row["ticker"],
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
        run_type=row["run_type"] or "eod",
    )
