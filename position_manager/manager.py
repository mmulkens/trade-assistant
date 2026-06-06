# ---------------------------------------------------------------------------
# manager.py — PositionManager: daily trade management orchestrator
#
# Responsibilities:
#   - For each open position, run the daily EOD management cycle:
#       1. Load OHLCV from the Parquet cache and determine current close price
#       2. Update peak_price if a new high was set (PM-16)
#       3. Calculate ATR14, volatility bucket, and ATR multiplier (PM-04/05)
#       4. If trail not yet active: check whether the 1.5R trigger was reached;
#          if so, activate trail, move stop to cost floor (PM-02/03)
#       5. If trail already active: recalculate trail level and advance stop if
#          it has moved higher — stop only ever rises (PM-06)
#       6. If trail never activated: evaluate the time-based exit rule (PM-09–12)
#   - Support CLI fallback for manual exit recording (PM-14):
#       close_manual(ticker, close_price, note)
#   - Submit stub stop orders to IBKR (Phase 1: log only; Phase 2: API call)
#   - Detect externally-closed positions via ib_insync (Phase 2 stub)
#
# Phase 1 constraints:
#   - No IBKR connectivity: _submit_stop_order() logs instead of calling TWS
#   - No real-time price feed: uses latest close from Parquet cache (yesterday's
#     close for an EOD run, which is the correct input for swing trading)
#   - _detect_manual_exits() is a no-op stub — CLI close is the fallback (PM-14)
#
# Operating mode:
#   run_eod() is a batch function: processes every open position once per day
#   and exits.  It is intended to be called after the daily data fetch has
#   completed so the Parquet cache reflects the previous session's closes.
#
# Pipeline position:
#   Signal Engine → Sim/Order Executor → risk.db
#                                             ↓
#                                     Position Manager (this module)
#                                             ↓
#                               Updated stop_price / closed positions in risk.db
# ---------------------------------------------------------------------------

import os
from datetime import datetime, timezone
from logging import Logger
from pathlib import Path
from typing import Optional

import pandas as pd

from signal_engine import indicators
from risk_layer import state as rl_state

from utils import pm_math as pm
from . import state as pm_state
from . import notify


class PositionManager:
    """Daily trade manager: ATR trailing stops, time-based exit, manual close.

    Typical call sequence:
        pm = PositionManager(config, logger)
        pm.run_eod()                          # once per day, after data fetch

        # CLI fallback for manual exits:
        pm.close_manual("ASML.AS", 72.50, note="closed early — earnings risk")
    """

    def __init__(self, config: dict, logger: Logger) -> None:
        self._logger = logger

        # --- Config sections ---
        pm = config["position_manager"]
        self._trail_trigger_r: float = float(pm["trail_trigger_r"])     # default 1.5
        self._time_limit_days: int = int(pm["time_limit_days"])         # default 7
        self._atr_period: int = int(pm["atr_period"])                   # default 14
        self._stop_proximity_pct: float = float(pm["stop_proximity_pct"])  # default 25
        self._atr_buckets: dict = pm["atr_buckets"]

        self._tob_pct: float = float(config["costs"]["tob_pct"])         # 0.35
        self._max_open_risk_pct: float = float(config["risk"]["max_open_risk_pct"])  # 6.0
        self._risk_db: str = config["risk"]["db_path"]
        self._signals_db: str = config["signal_engine"]["db_path"]
        self._cache_dir: str = config.get("data_fetcher", {}).get("cache_dir", "./cache")

        # Phase 1 stub: portfolio value from config (Phase 2: IBKR reqAccountSummary)
        self._portfolio_value: float = float(config["risk"]["portfolio_value_stub"])

        # Telegram: notifications enabled only when both fields are non-empty
        notif = config.get("notifications", {})
        self._bot_token: str = notif.get("telegram_bot_token", "") or ""
        self._chat_id: str = notif.get("telegram_chat_id", "") or ""
        self._notify_enabled: bool = bool(self._bot_token and self._chat_id)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run_eod(self) -> None:
        """Process all open positions in one daily EOD batch.

        Loads open positions from risk.db, then runs the full management
        cycle for each one.  Positions for which the Parquet cache is missing
        or too short are skipped with a warning — they will be re-evaluated
        on the next run when data is available.

        Call this once per trading day after the DataFetcher has updated the
        Parquet cache with the latest closes.
        """
        positions = rl_state.get_open_positions(self._risk_db)
        if not positions:
            self._logger.info({"event": "eod_run_complete", "open_positions": 0})
            return

        self._logger.info({
            "event": "eod_run_started",
            "open_positions": len(positions),
        })

        for pos in positions:
            try:
                self._process_position(pos)
            except Exception as exc:
                # A crash on one position must not abort all others.
                # The exception is logged with the position context for debugging.
                self._logger.warning({
                    "event": "position_processing_error",
                    "ticker": pos.get("ticker"),
                    "error": str(exc),
                })

        self._logger.info({
            "event": "eod_run_complete",
            "open_positions": len(positions),
        })

    def close_manual(
        self,
        ticker: str,
        close_price: float,
        note: Optional[str] = None,
    ) -> bool:
        """Record a manually-executed position close from the CLI (PM-14).

        Used when the user closes a position directly via the IBKR platform
        and the automatic detection (Phase 2) is not yet available, or when
        the bot was offline at the time of the close.

        Returns True if the position was found and closed; False otherwise.
        """
        result = pm_state.close_position_full(
            ticker=ticker,
            close_price=close_price,
            reason="manual",
            tob_pct=self._tob_pct,
            bot_initiated=False,
            db_path=self._risk_db,
            exit_note=note,
        )
        if result is None:
            self._logger.warning({
                "event": "manual_close_not_found",
                "ticker": ticker,
                "close_price": close_price,
            })
            return False

        self._logger.info({
            "event": "position_closed",
            "ticker": ticker,
            "close_price": close_price,
            "reason": "manual",
            "bot_initiated": False,
            "gross_pnl": result["gross_pnl"],
            "net_pnl": result["net_pnl"],
            "exit_commission": result["exit_commission"],
            "exit_note": note,
        })

        if self._notify_enabled:
            notify.send_manual_exit(
                ticker=ticker,
                close_price=close_price,
                net_pnl=result["net_pnl"],
                source="cli",
                bot_token=self._bot_token,
                chat_id=self._chat_id,
                logger=self._logger,
            )

        return True

    # -----------------------------------------------------------------------
    # Per-position EOD management cycle
    # -----------------------------------------------------------------------

    def _process_position(self, pos: dict) -> None:
        """Run the full daily management cycle for one open position.

        Decision flow (matches Section 11-F of trade_assistant_design.md):

            1. Load OHLCV; skip if cache missing or insufficient history
            2. Update peak_price if new high reached
            3. Calculate ATR14, ATR%, volatility bucket, multiplier
            4. If trail NOT active:
                   a. Check trail trigger (1.5R threshold)
                   b. If triggered: activate trail, move stop to max(cost_floor, atr_trail)
                   c. If not triggered: evaluate time-based exit (PM-09–12)
            5. If trail IS active:
                   Recalculate ATR trail; advance stop if trail has risen
        """
        ticker = pos["ticker"]

        # -------------------------------------------------------------------
        # Step 1 — Load OHLCV from Parquet cache
        #
        # We need at least atr_period + 1 bars to calculate a meaningful ATR.
        # If the cache is missing or too short, skip this position and log a
        # warning — it will be re-evaluated on the next EOD run.
        # -------------------------------------------------------------------
        df = self._load_ohlcv(ticker)
        if df is None or len(df) < self._atr_period + 1:
            self._logger.warning({
                "event": "ohlcv_unavailable",
                "ticker": ticker,
                "reason": "cache_missing_or_too_short",
            })
            return

        # Current close price = the most recent bar in the Parquet cache
        # (yesterday's close in a daily EOD run after today's data fetch).
        current_price = float(df["close"].iloc[-1])

        # -------------------------------------------------------------------
        # Step 2 — Update peak_price
        #
        # peak_price tracks the all-time high close since entry.  It drives
        # the ATR trail anchor: trail_level = peak_price − (ATR × multiplier).
        # A rising peak compresses the trail level upward (good); a falling
        # price leaves peak_price unchanged (stop stays where it is).
        # -------------------------------------------------------------------
        entry_date = self._parse_date(pos["opened_at"])
        current_peak = float(pos["peak_price"] or pos.get("fill_price") or pos["entry_price"])

        if current_price > current_peak:
            pm_state.update_peak_price(ticker, current_price, self._risk_db)
            current_peak = current_price
            self._logger.info({
                "event": "peak_price_updated",
                "ticker": ticker,
                "new_peak": current_peak,
            })

        # -------------------------------------------------------------------
        # Step 3 — ATR and volatility bucket
        #
        # ATR14 from the full OHLCV history (not just post-entry bars) so we
        # have a stable, representative volatility estimate.  ATR% = ATR / price
        # normalises across instruments of different price levels.
        # -------------------------------------------------------------------
        atr_series = indicators.atr(df, period=self._atr_period)
        current_atr = float(atr_series.iloc[-1])
        atr_pct = current_atr / current_price * 100.0

        bucket = pm.classify_volatility_bucket(
            atr_pct,
            self._atr_buckets["low_threshold_pct"],
            self._atr_buckets["high_threshold_pct"],
        )
        multiplier = pm.atr_multiplier(bucket, self._atr_buckets)
        cost_floor = pm.cost_floor(pos["entry_price"], pos["shares"], self._tob_pct)

        self._logger.info({
            "event": "position_evaluated",
            "ticker": ticker,
            "current_price": current_price,
            "current_peak": current_peak,
            "current_stop": pos["stop_price"],
            "trail_triggered": bool(pos["trail_triggered"]),
            "atr14": round(current_atr, 4),
            "atr_pct": round(atr_pct, 3),
            "volatility_bucket": bucket,
            "atr_multiplier": multiplier,
            "cost_floor": cost_floor,
        })

        trail_triggered = bool(pos["trail_triggered"])

        # -------------------------------------------------------------------
        # Step 4 — Trail not yet active: check trigger and time-based exit
        # -------------------------------------------------------------------
        if not trail_triggered:
            self._handle_pre_trail(pos, current_price, current_peak, current_atr,
                                   multiplier, cost_floor, bucket, atr_pct, df, entry_date)
            return

        # -------------------------------------------------------------------
        # Step 5 — Trail active: advance stop if trail has risen (PM-06)
        # -------------------------------------------------------------------
        self._handle_trail_update(pos, current_peak, current_atr, multiplier, cost_floor)

    # -----------------------------------------------------------------------
    # Sub-handlers (keep _process_position readable)
    # -----------------------------------------------------------------------

    def _handle_pre_trail(
        self,
        pos: dict,
        current_price: float,
        current_peak: float,
        current_atr: float,
        multiplier: float,
        cost_floor: float,
        bucket: str,
        atr_pct: float,
        df: pd.DataFrame,
        entry_date: "datetime.date",
    ) -> None:
        """Handle positions where the trail has not yet been activated.

        Two outcomes:
            A. Trail trigger reached → activate trail, move stop, log + notify
            B. Trail not yet reached → evaluate time-based exit rule (PM-09–12)
        """
        ticker = pos["ticker"]

        # --- A. Check trail trigger ---
        if pm.trail_trigger_reached(
            current_price,
            pos["entry_price"],
            pos["risk_per_share"],
            self._trail_trigger_r,
        ):
            trail_level = pm.atr_trail_level(current_peak, current_atr, multiplier)
            new_stop = pm.active_stop(cost_floor, trail_level)

            # Stop only moves up — if somehow the trail calc is below the
            # existing stop (e.g. an unusually wide ATR on activation day),
            # keep the existing stop rather than lowering it.
            if new_stop <= pos["stop_price"]:
                new_stop = pos["stop_price"]

            # Guard: never set stop at or below zero
            if new_stop > 0.0:
                new_rps = pos["entry_price"] - new_stop
                new_risk_amount = max(0.0, pos["shares"] * new_rps)
                pm_state.update_position_stop(ticker, new_stop, new_rps, new_risk_amount, self._risk_db)

            pm_state.activate_trail(ticker, current_price, self._risk_db)

            self._logger.info({
                "event": "trail_activated",
                "ticker": ticker,
                "trigger_price": current_price,
                "cost_floor": cost_floor,
                "atr_trail_level": trail_level,
                "new_stop": new_stop,
                "running_high": current_peak,
                "volatility_bucket": bucket,
                "atr_pct": round(atr_pct, 3),
            })

            if self._notify_enabled:
                notify.send_trail_activated(
                    ticker=ticker,
                    entry_price=pos["entry_price"],
                    cost_floor=cost_floor,
                    new_stop=new_stop,
                    current_price=current_price,
                    bucket=bucket,
                    atr_pct=atr_pct,
                    bot_token=self._bot_token,
                    chat_id=self._chat_id,
                    logger=self._logger,
                )

            # Phase 1 stub: log the stop order that would be submitted to IBKR
            self._submit_stop_order(ticker, new_stop)
            return

        # --- B. Trail not triggered → time-based exit evaluation (PM-09) ---
        trading_days = self._count_trading_days_since_open(entry_date, df)

        self._logger.info({
            "event": "time_exit_evaluation",
            "ticker": ticker,
            "trading_days": trading_days,
            "time_limit_days": self._time_limit_days,
            "trail_triggered": False,
            "current_price": current_price,
            "stop_price": pos["stop_price"],
        })

        if trading_days <= self._time_limit_days:
            # Time limit not yet reached — nothing to do today
            return

        # Time limit exceeded — check PM-10 gate conditions
        self._evaluate_time_exit(pos, current_price, trading_days)

    def _handle_trail_update(
        self,
        pos: dict,
        current_peak: float,
        current_atr: float,
        multiplier: float,
        cost_floor: float,
    ) -> None:
        """Advance the ATR trail for a position that already has an active trail.

        The stop only moves up (PM-06).  If the recalculated trail level is
        lower than the current stop (can happen when ATR expands sharply on a
        volatile session), the stop is left unchanged.
        """
        ticker = pos["ticker"]
        current_stop = pos["stop_price"]

        trail_level = pm.atr_trail_level(current_peak, current_atr, multiplier)
        new_stop = pm.active_stop(cost_floor, trail_level)

        if new_stop <= current_stop:
            # Trail has not risen — no action needed today
            self._logger.info({
                "event": "trail_no_change",
                "ticker": ticker,
                "current_stop": current_stop,
                "recalculated_trail": trail_level,
                "active_stop_candidate": new_stop,
                "running_high": current_peak,
            })
            return

        # Stop advanced — update DB, log, notify, submit order
        new_rps = pos["entry_price"] - new_stop
        new_risk_amount = max(0.0, pos["shares"] * new_rps)
        pm_state.update_position_stop(ticker, new_stop, new_rps, new_risk_amount, self._risk_db)

        self._logger.info({
            "event": "trail_updated",
            "ticker": ticker,
            "old_stop": current_stop,
            "new_stop": new_stop,
            "atr_trail_level": trail_level,
            "running_high": current_peak,
            "new_risk_amount": new_risk_amount,
        })

        if self._notify_enabled:
            notify.send_trail_updated(
                ticker=ticker,
                old_stop=current_stop,
                new_stop=new_stop,
                running_high=current_peak,
                atr_trail_level=trail_level,
                bot_token=self._bot_token,
                chat_id=self._chat_id,
                logger=self._logger,
            )

        self._submit_stop_order(ticker, new_stop)

    def _evaluate_time_exit(
        self,
        pos: dict,
        current_price: float,
        trading_days: int,
    ) -> None:
        """Evaluate PM-10/11/12 time-exit conditions after the time limit passes.

        Exit fires only when ALL conditions are met (AND logic):
            1. Signal queue non-empty   → opportunity cost exists (PM-10)
            2. Open risk ≥ 6%           → position consumes constrained budget (PM-10)
            3. Price within 25% of stop → stop likely incoming anyway (PM-11)

        If any condition fails, the position is held and the rationale logged.
        PM-12: never force-close at a loss on time alone — the stop manages that.
        """
        ticker = pos["ticker"]

        # --- Condition 1: signal queue must be non-empty ---
        pending = pm_state.count_pending_signals(self._signals_db)
        if pending == 0:
            self._log_time_hold(ticker, trading_days, "no_pending_signals",
                                {"pending_signals": 0})
            return

        # --- Condition 2: open risk must be at or above the cap ---
        open_risk_pct = pm_state.get_open_risk_pct(self._risk_db, self._portfolio_value)
        if open_risk_pct < self._max_open_risk_pct:
            self._log_time_hold(ticker, trading_days, "open_risk_below_cap",
                                {"open_risk_pct": open_risk_pct,
                                 "max_open_risk_pct": self._max_open_risk_pct})
            return

        # --- Condition 3: price must be near the stop ---
        proximity = pm.stop_proximity_ratio(
            current_price, pos["stop_price"], pos["entry_price"]
        )
        is_near_stop = proximity <= (self._stop_proximity_pct / 100.0)

        self._logger.info({
            "event": "time_exit_proximity_check",
            "ticker": ticker,
            "current_price": current_price,
            "stop_price": pos["stop_price"],
            "proximity_ratio": round(proximity, 4),
            "threshold_ratio": round(self._stop_proximity_pct / 100.0, 4),
            "near_stop": is_near_stop,
        })

        if not is_near_stop:
            self._log_time_hold(ticker, trading_days, "not_near_stop",
                                {"proximity_ratio": round(proximity, 4)})
            return

        # --- All conditions met: close the position ---
        result = pm_state.close_position_full(
            ticker=ticker,
            close_price=current_price,
            reason="time_exit",
            tob_pct=self._tob_pct,
            bot_initiated=True,
            db_path=self._risk_db,
        )
        if result is None:
            # Should not happen here but guard in case of a concurrent close
            self._logger.warning({
                "event": "time_exit_close_missed",
                "ticker": ticker,
                "note": "no open position found at close time",
            })
            return

        self._logger.info({
            "event": "position_closed",
            "ticker": ticker,
            "close_price": current_price,
            "reason": "time_exit",
            "bot_initiated": True,
            "trading_days": trading_days,
            "gross_pnl": result["gross_pnl"],
            "net_pnl": result["net_pnl"],
            "exit_commission": result["exit_commission"],
        })

        if self._notify_enabled:
            notify.send_time_exit_fired(
                ticker=ticker,
                close_price=current_price,
                trading_days=trading_days,
                gross_pnl=result["gross_pnl"],
                net_pnl=result["net_pnl"],
                bot_token=self._bot_token,
                chat_id=self._chat_id,
                logger=self._logger,
            )

    # -----------------------------------------------------------------------
    # OHLCV helpers
    # -----------------------------------------------------------------------

    def _load_ohlcv(self, ticker: str) -> Optional[pd.DataFrame]:
        """Load the Parquet cache for a ticker.

        Returns a DataFrame with a DatetimeIndex named 'date' and lowercase
        OHLCV columns, or None if the cache file does not exist.

        Replicates the same path-sanitization logic as data_fetcher/cache.py
        (_cache_path): tickers with special characters (^ / :) have those
        characters replaced with underscores so the filename is valid on all
        operating systems.
        """
        safe = ticker.replace("^", "_").replace("/", "_").replace(":", "_")
        path = Path(self._cache_dir) / f"{safe}.parquet"
        if not path.exists():
            return None
        try:
            return pd.read_parquet(path)
        except Exception as exc:
            self._logger.warning({
                "event": "ohlcv_load_error",
                "ticker": ticker,
                "path": str(path),
                "error": str(exc),
            })
            return None

    @staticmethod
    def _count_trading_days_since_open(
        entry_date: "datetime.date",
        df: pd.DataFrame,
    ) -> int:
        """Count distinct trading days in the OHLCV history on or after entry_date.

        Uses the DatetimeIndex from the Parquet cache — each row is one
        trading day, so counting rows since the entry date gives the number
        of trading sessions the position has been open.

        This is more accurate than calendar-day arithmetic because it
        automatically accounts for weekends and market holidays without
        requiring a separate trading-calendar library.
        """
        if df.index.tz is not None:
            # Normalise to tz-naive for comparison with entry_date
            idx_dates = df.index.tz_localize(None).normalize()
        else:
            idx_dates = df.index.normalize()
        entry_dt = pd.Timestamp(entry_date)
        return int((idx_dates >= entry_dt).sum())

    # -----------------------------------------------------------------------
    # Phase 2 stubs
    # -----------------------------------------------------------------------

    def _submit_stop_order(self, ticker: str, stop_price: float) -> None:
        """Submit or modify a stop order via IBKR TWS (Phase 1: log only).

        Phase 1 — stub: logs the intended order so the operator can manually
        place or adjust the stop if needed.  The stop is tracked in risk.db
        regardless; the IBKR stop is for automatic execution if the bot is
        offline when the stop triggers.

        Phase 2 — implementation:
            Use ib_insync to place a STP order (or modify the existing one
            if stop_order_id is stored on the position row):
                contract = ib.qualifyContracts(Stock(ticker, ...))[0]
                order = Order(action='SELL', orderType='STP',
                              totalQuantity=shares, auxPrice=stop_price)
                trade = ib.placeOrder(contract, order)
            Store trade.order.orderId in risk_positions for future modifications.
        """
        self._logger.info({
            "event": "stop_order_stub",
            "ticker": ticker,
            "stop_price": stop_price,
            "note": "Phase 1: stop tracked in DB only; submit to IBKR manually or via Phase 2",
        })

    def _detect_manual_exits(self) -> None:
        """Detect positions closed externally via the IBKR platform (Phase 2).

        Phase 1 — stub: no-op.  Manual exits must be recorded via the CLI
        fallback (PM-14): `python -m position_manager close TICKER PRICE`.

        Phase 2 — implementation:
            Subscribe to ib.positionEvent (fires on any account position change).
            For each event, check whether the affected instrument has an open
            row in risk_positions.  If the IBKR position is 0 shares but the
            DB shows 'open', the position was closed outside the bot — record
            it as exit_reason='manual', bot_initiated=False, using the fill
            price from the ib.fills() report.
        """

    # -----------------------------------------------------------------------
    # Logging helpers
    # -----------------------------------------------------------------------

    def _log_time_hold(
        self,
        ticker: str,
        trading_days: int,
        reason: str,
        extra: dict,
    ) -> None:
        """Log a time-exit hold decision with its rationale (PM-17)."""
        self._logger.info({
            "event": "time_exit_hold",
            "ticker": ticker,
            "trading_days": trading_days,
            "reason": reason,
            **extra,
        })
        if self._notify_enabled:
            notify.send_time_exit_hold(
                ticker=ticker,
                trading_days=trading_days,
                hold_reason=reason,
                bot_token=self._bot_token,
                chat_id=self._chat_id,
                logger=self._logger,
            )

    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_date(iso_str: str) -> "datetime.date":
        """Parse an ISO-8601 UTC string from the DB and return the date part."""
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date()
