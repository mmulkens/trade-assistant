# ---------------------------------------------------------------------------
# sim_pm.py — SimPositionManager: per-bar position evaluation for simulation
#
# Responsibilities:
#   - Gap-aware stop fill: exit at bar.open if open <= stop; sets gap_filled=True on ExitResult
#   - Intraday stop fill: exit at stop if bar.low <= stop
#   - ATR trailing stop advancement when trail is active
#   - Trail trigger detection and activation at 1.5R
#   - Time-based exit evaluation (mirrors PM-09–12 from the live Position Manager)
#
# What this module does NOT do:
#   - Read from or write to any database (all DB writes are the runner's job)
#   - Track peak_price (runner updates it in the position dict before calling evaluate)
#   - Log anything (caller logs from returned ExitResult)
#
# Design:
#   evaluate() is pure given its inputs — same position + same bar always
#   produces the same ExitResult.  The runner is responsible for persisting
#   state changes (new_stop, trail_activated) to wf_sim.db.
# ---------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from utils import pm_math as pm


@dataclass
class ExitResult:
    """Outcome of evaluating one position against one bar.

    If closed is False, new_stop and trail_activated contain the updated state
    for the runner to persist.  If closed is True, the pnl fields are populated
    and new_stop / trail_activated are ignored.
    """

    closed: bool
    exit_price: float = 0.0
    exit_reason: str = ""          # 'stop_hit' | 'trail_stop' | 'time_exit'
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    exit_commission: float = 0.0
    new_stop: float = 0.0          # updated stop (only relevant when closed=False)
    trail_activated: bool = False  # True on the first bar the trail trigger fires
    gap_filled: bool = False       # True when filled at bar.open (open gapped below stop)


class SimPositionManager:
    """Evaluate one open position against one OHLCV bar.

    Mirrors PositionManager's daily cycle but operates entirely on the data
    passed in — no IO of any kind.  The runner calls this for each position
    on each simulation day, then persists the resulting state changes.

    Typical call:
        result = sim_pm.evaluate(position, today_bar, pending_signals,
                                 all_open_positions, config, atr_value,
                                 trading_days=n)
        if result.closed:
            # record exit, update portfolio_value
        elif result.new_stop > current_stop:
            # persist new_stop to wf_sim.db
        if result.trail_activated:
            # persist trail activation to risk_positions
    """

    def __init__(self, config: dict) -> None:
        pm_cfg = config["position_manager"]
        self._trail_trigger_r: float = float(pm_cfg["trail_trigger_r"])
        self._time_limit_days: int = int(pm_cfg["time_limit_days"])
        self._stop_proximity_pct: float = float(pm_cfg["stop_proximity_pct"])
        self._atr_buckets: dict = pm_cfg["atr_buckets"]
        self._tob_pct: float = float(config["costs"]["tob_pct"])
        self._max_open_risk_pct: float = float(config["risk"]["max_open_risk_pct"])

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def evaluate(
        self,
        position: dict,
        bar: pd.Series,
        signal_queue: list,
        all_open_positions: list,
        config: dict,
        atr_value: float,
        trading_days: int = 0,
    ) -> ExitResult:
        """Evaluate one position for one bar. Returns an ExitResult.

        position  — dict from rl_state.get_open_positions(); runner must have
                    already updated 'peak_price' to today's running high before
                    calling this method.
        bar       — today's OHLCV Series (open, high, low, close, volume).
        signal_queue — signals pending entry today (used for time-exit gate).
        all_open_positions — all currently open positions (for open-risk calc).
        config    — live config dict (portfolio_value_stub kept current by runner).
        atr_value — ATR14 computed from the walker-truncated DataFrame.
        trading_days — number of trading sessions since entry (runner-computed).
        """
        entry_price = float(position["entry_price"])
        stop_price  = float(position["stop_price"])
        shares      = int(position["shares"])
        rps         = float(position["risk_per_share"])
        peak_price  = float(position.get("peak_price") or position.get("fill_price") or entry_price)
        trail_on    = bool(position["trail_triggered"])

        bar_open  = float(bar["open"])
        bar_low   = float(bar["low"])
        bar_close = float(bar["close"])

        # --- Gap-down through stop ---
        # Fill at bar open (worse than stop). Reason reflects whether trail was active.
        if bar_open <= stop_price:
            reason = "trail_stop" if trail_on else "stop_hit"
            return self._make_exit(entry_price, bar_open, shares, reason, gap_filled=True)

        # --- Intraday stop hit ---
        if bar_low <= stop_price:
            reason = "trail_stop" if trail_on else "stop_hit"
            return self._make_exit(entry_price, stop_price, shares, reason)

        # --- No stop hit; evaluate trail / time-exit ---
        new_stop       = stop_price
        trail_activated = False

        if trail_on:
            new_stop = self._advance_trail(entry_price, stop_price, peak_price,
                                           atr_value, bar_close, shares)
        else:
            if pm.trail_trigger_reached(bar_close, entry_price, rps, self._trail_trigger_r):
                new_stop, trail_activated = self._activate_trail(
                    entry_price, stop_price, peak_price, atr_value, bar_close, shares
                )
            elif self._time_exit_fires(bar_close, stop_price, entry_price,
                                       trading_days, signal_queue,
                                       all_open_positions, config):
                return self._make_exit(entry_price, bar_close, shares, "time_exit")

        return ExitResult(closed=False, new_stop=new_stop, trail_activated=trail_activated)

    # -----------------------------------------------------------------------
    # Trail helpers
    # -----------------------------------------------------------------------

    def _advance_trail(
        self,
        entry_price: float,
        current_stop: float,
        peak_price: float,
        atr_value: float,
        current_price: float,
        shares: int,
    ) -> float:
        """Recalculate trail level; return new stop (never lower than current)."""
        floor     = pm.cost_floor(entry_price, shares, self._tob_pct)
        mult      = pm.atr_multiplier(self._bucket(atr_value, current_price), self._atr_buckets)
        trail     = pm.atr_trail_level(peak_price, atr_value, mult)
        candidate = pm.active_stop(floor, trail)
        return candidate if candidate > current_stop else current_stop

    def _activate_trail(
        self,
        entry_price: float,
        current_stop: float,
        peak_price: float,
        atr_value: float,
        current_price: float,
        shares: int,
    ) -> tuple[float, bool]:
        """Compute initial trail stop on activation day.

        Returns (new_stop, trail_activated=True).  Never lowers the existing stop.
        """
        floor     = pm.cost_floor(entry_price, shares, self._tob_pct)
        mult      = pm.atr_multiplier(self._bucket(atr_value, current_price), self._atr_buckets)
        trail     = pm.atr_trail_level(peak_price, atr_value, mult)
        candidate = pm.active_stop(floor, trail)
        new_stop  = candidate if (candidate > current_stop and candidate > 0) else current_stop
        return new_stop, True

    # -----------------------------------------------------------------------
    # Time-exit gate (mirrors PM-09–12)
    # -----------------------------------------------------------------------

    def _time_exit_fires(
        self,
        current_price: float,
        stop_price: float,
        entry_price: float,
        trading_days: int,
        signal_queue: list,
        all_open_positions: list,
        config: dict,
    ) -> bool:
        """Return True when ALL four time-exit conditions are met."""
        if trading_days <= self._time_limit_days:
            return False
        if len(signal_queue) == 0:
            return False
        if self._open_risk_pct(all_open_positions, config) < self._max_open_risk_pct:
            return False
        proximity = pm.stop_proximity_ratio(current_price, stop_price, entry_price)
        return proximity <= self._stop_proximity_pct / 100.0

    # -----------------------------------------------------------------------
    # Exit builder
    # -----------------------------------------------------------------------

    def _make_exit(
        self,
        entry_price: float,
        exit_price: float,
        shares: int,
        reason: str,
        gap_filled: bool = False,
    ) -> ExitResult:
        """Compute round-trip P&L and return a closed ExitResult."""
        gross_pnl        = round((exit_price - entry_price) * shares, 4)
        entry_commission = round(entry_price * shares * self._tob_pct / 100, 4)
        exit_commission  = round(exit_price  * shares * self._tob_pct / 100, 4)
        net_pnl          = round(gross_pnl - entry_commission - exit_commission, 4)
        return ExitResult(
            closed=True,
            exit_price=exit_price,
            exit_reason=reason,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            exit_commission=exit_commission,
            gap_filled=gap_filled,
        )

    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------

    def _bucket(self, atr_value: float, current_price: float) -> str:
        atr_pct = atr_value / current_price * 100.0
        return pm.classify_volatility_bucket(
            atr_pct,
            self._atr_buckets["low_threshold_pct"],
            self._atr_buckets["high_threshold_pct"],
        )

    def _open_risk_pct(self, positions: list, config: dict) -> float:
        portfolio_value = float(config["risk"]["portfolio_value_stub"])
        if portfolio_value <= 0:
            return 0.0
        total_risk = sum(float(p.get("risk_amount", 0.0)) for p in positions)
        return total_risk / portfolio_value * 100.0
