# ---------------------------------------------------------------------------
# layer.py — Risk Layer orchestrator
#
# Responsibilities:
#   - Evaluate every incoming Signal before it reaches the Order Executor
#   - Calculate position size (RL-03) and enforce hard caps (RL-01, RL-02)
#   - Block duplicate positions on the same instrument (RL-07)
#   - Block new orders when the daily loss limit is breached (RL-06)
#   - Apply reduced sizing for thin/illiquid instruments (RL-09)
#   - Persist approved trades to the SQLite state store
#   - Record position closes and track realised P&L
#
# IBKR stubs (Phase 1):
#   _get_portfolio_value() returns the YAML config value (risk.portfolio_value_stub).
#   Daily loss limit checks only cover realised P&L — unrealised intraday
#   losses require live pricing from TWS and will be added in Phase 2 when
#   the IBKR connection is live.
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from logging import Logger
from typing import Optional, TYPE_CHECKING

from . import calculator as calc
from . import state as st

if TYPE_CHECKING:
    from signal_engine.engine import Signal


# ---------------------------------------------------------------------------
# RiskDecision — the output contract passed to the Order Executor
# ---------------------------------------------------------------------------

@dataclass
class RiskDecision:
    """Result of running a Signal through all pre-trade risk checks.

    If approved is False, shares is 0 and reject_reason explains why.
    The Order Executor only proceeds if approved is True.
    All monetary values are in the portfolio's base currency (EUR).
    """
    approved: bool
    signal: "Signal"
    shares: int                        # 0 when rejected
    risk_per_share: float              # entry − stop
    risk_amount: float                 # shares × risk_per_share  (EUR)
    position_risk_pct: float           # risk_amount / portfolio_value × 100
    effective_cap_pct: float           # cap that was applied (reduced for thin)
    current_open_risk_pct: float       # total open risk before this trade
    projected_open_risk_pct: float     # total open risk if this trade is taken
    portfolio_value: float             # value used for all calculations
    reject_reason: Optional[str]       # None when approved


# ---------------------------------------------------------------------------
# RiskLayer
# ---------------------------------------------------------------------------

class RiskLayer:
    """Pre-trade risk gate: evaluates every Signal before an order is placed.

    Typical call sequence per session:
        1.  engine.scan()            → list[Signal]
        2.  risk.evaluate(signal)    → RiskDecision  (for each signal)
        3.  order_executor.submit(decision)          (only if decision.approved)
        4.  risk.open_position(decision)             (after fill confirmed by broker)
            ...position is now tracked in SQLite...
        5.  risk.close_position(ticker, price, reason)  (on stop hit / target / trail)
    """

    def __init__(self, config: dict, logger: Logger) -> None:
        self._logger = logger
        rl = config["risk"]
        self._max_position_risk_pct: float = rl["max_position_risk_pct"]   # RL-01: 1.5%
        self._max_open_risk_pct: float = rl["max_open_risk_pct"]           # RL-02: 6.0%
        self._daily_loss_limit_pct: float = rl["daily_loss_limit_pct"]     # RL-06: 3.0%
        self._portfolio_value_stub: float = rl["portfolio_value_stub"]     # Phase 1 stub (RL-04)
        self._thin_size_multiplier: float = rl.get("thin_size_multiplier", 0.5)  # RL-09
        self._db_path: str = rl["db_path"]

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def evaluate(self, signal: "Signal") -> RiskDecision:
        """Run all pre-trade checks and return a RiskDecision.

        Checks run in this specific order for a reason:
            1. Trading paused   — cheapest check; if paused, nothing else matters
            2. Duplicate        — single DB query; prevents wasted sizing math
            3. Daily loss limit — computes P&L before sizing; can also set pause
            4. Position sizing  — pure math; must precede the two cap checks
            5. Per-trade cap    — hard cap on individual position risk (RL-01)
            6. Total open risk  — hard cap on aggregate portfolio risk (RL-02)

        The two hard caps (5 and 6) are NEVER overridden.  If either fires,
        the signal is silently dropped — no human action is required or expected.
        """
        portfolio_value = self._get_portfolio_value()
        current_open_risk_amount = st.get_open_risk_amount(self._db_path)
        current_open_risk_pct = calc.open_risk_pct(current_open_risk_amount, portfolio_value)

        def _reject(reason: str) -> RiskDecision:
            """Build a rejected RiskDecision and log it."""
            self._logger.info({
                "event": "risk_check_failed",
                "ticker": signal.ticker,
                "reason": reason,
                "portfolio_value": portfolio_value,
                "current_open_risk_pct": current_open_risk_pct,
            })
            return RiskDecision(
                approved=False,
                signal=signal,
                shares=0,
                risk_per_share=round(signal.entry_price - signal.stop_price, 4),
                risk_amount=0.0,
                position_risk_pct=0.0,
                effective_cap_pct=0.0,
                current_open_risk_pct=current_open_risk_pct,
                projected_open_risk_pct=current_open_risk_pct,
                portfolio_value=portfolio_value,
                reject_reason=reason,
            )

        # -------------------------------------------------------------------
        # Check 1 — Trading paused (daily loss limit previously triggered today)
        #
        # The pause flag is set as today's date in system_state.  It auto-
        # expires at midnight — no reset required for a normal next-session
        # start.  The CLI 'unpause' command clears it manually if needed.
        # -------------------------------------------------------------------
        if st.is_trading_paused(self._db_path):
            return _reject("trading_paused:daily_loss_limit_reached")

        # -------------------------------------------------------------------
        # Check 2 — Duplicate instrument (RL-07, anti-pyramiding)
        #
        # Only one open position per ticker is allowed.  This prevents both
        # accidental double-entries and deliberate pyramiding (adding to a
        # winner), which is out of scope for v1.
        # -------------------------------------------------------------------
        if st.has_open_position(signal.ticker, self._db_path):
            return _reject("duplicate_instrument")

        # -------------------------------------------------------------------
        # Check 3 — Daily loss limit (RL-06)
        #
        # If total realised P&L for today falls below −daily_loss_limit_pct
        # of portfolio value, pause all new orders for the rest of the session.
        #
        # Phase 1 covers only realised losses (positions closed today).
        # Unrealised intraday drawdown will be added in Phase 2 when live
        # pricing from IBKR is available.
        #
        # Note: daily_loss_pct is negative when the account has lost money,
        # so we compare against the negative of the limit threshold.
        # -------------------------------------------------------------------
        daily_pnl = st.get_daily_realized_pnl(self._db_path)
        daily_loss_pct = (daily_pnl / portfolio_value) * 100
        if daily_loss_pct < -self._daily_loss_limit_pct:
            st.set_trading_pause(self._db_path)
            self._logger.warning({
                "event": "daily_loss_limit_breached",
                "ticker": signal.ticker,
                "daily_pnl": daily_pnl,
                "daily_loss_pct": round(daily_loss_pct, 3),
                "limit_pct": self._daily_loss_limit_pct,
            })
            return _reject("daily_loss_limit_breached")

        # -------------------------------------------------------------------
        # Check 4 — Position sizing (RL-03, RL-09)
        #
        # Calculate the number of shares we can buy so that the risk
        # (entry − stop) × shares stays within the per-trade cap.
        # Thin instruments receive a reduced cap (RL-09).
        # -------------------------------------------------------------------
        sizing = calc.size_position(
            entry=signal.entry_price,
            stop=signal.stop_price,
            portfolio_value=portfolio_value,
            max_position_risk_pct=self._max_position_risk_pct,
            liquidity_class=signal.liquidity_class,
            thin_size_multiplier=self._thin_size_multiplier,
        )

        # shares == 0 means risk_per_share is larger than the entire risk budget
        # (extremely wide stop relative to portfolio size).  Cannot size.
        if sizing.shares == 0:
            return _reject("zero_shares:risk_per_share_too_large_or_invalid_prices")

        # -------------------------------------------------------------------
        # Check 5 — Per-trade hard cap (RL-01): max 1.5% per trade
        #
        # floor() in size_position() mathematically guarantees we cannot
        # exceed the cap.  This explicit check acts as a safety net against
        # any future changes to size_position() that might inadvertently
        # produce oversized results.  The 0.001% tolerance covers floating-
        # point rounding in the final percentage calculation.
        #
        # This rule is NEVER overridden — it is the outermost guard on
        # individual position risk.
        # -------------------------------------------------------------------
        if sizing.position_risk_pct > self._max_position_risk_pct + 0.001:
            return _reject(
                f"position_risk_cap_exceeded:"
                f"{sizing.position_risk_pct:.3f}pct_vs_limit_{self._max_position_risk_pct}pct"
            )

        # -------------------------------------------------------------------
        # Check 6 — Total open risk hard cap (RL-02): max 6% across all trades
        #
        # Projects what total open risk would be if this trade is added.
        # If it would push the aggregate over 6%, the signal is dropped even
        # though it would be valid on its own.  When this cap is hit, the
        # session is considered "fully invested" from a risk perspective —
        # new signals should wait for existing positions to be closed.
        #
        # This rule is NEVER overridden.
        # -------------------------------------------------------------------
        projected_open_risk_pct = calc.open_risk_pct(
            current_open_risk_amount + sizing.risk_amount, portfolio_value
        )
        if projected_open_risk_pct > self._max_open_risk_pct:
            return _reject(
                f"open_risk_cap_exceeded:"
                f"projected_{projected_open_risk_pct:.3f}pct_vs_limit_{self._max_open_risk_pct}pct"
            )

        # --- All checks passed ---
        self._logger.info({
            "event": "risk_check_passed",
            "ticker": signal.ticker,
            "signal_type": signal.signal_type,
            "liquidity_class": signal.liquidity_class,
            "shares": sizing.shares,
            "entry": signal.entry_price,
            "stop": signal.stop_price,
            "risk_per_share": sizing.risk_per_share,
            "risk_amount": sizing.risk_amount,
            "position_risk_pct": sizing.position_risk_pct,
            "effective_cap_pct": sizing.effective_cap_pct,
            "current_open_risk_pct": current_open_risk_pct,
            "projected_open_risk_pct": projected_open_risk_pct,
            "portfolio_value": portfolio_value,
        })

        return RiskDecision(
            approved=True,
            signal=signal,
            shares=sizing.shares,
            risk_per_share=sizing.risk_per_share,
            risk_amount=sizing.risk_amount,
            position_risk_pct=sizing.position_risk_pct,
            effective_cap_pct=sizing.effective_cap_pct,
            current_open_risk_pct=current_open_risk_pct,
            projected_open_risk_pct=projected_open_risk_pct,
            portfolio_value=portfolio_value,
            reject_reason=None,
        )

    def open_position(
        self,
        decision: RiskDecision,
        fill_price: Optional[float] = None,
        fill_timestamp: Optional[str] = None,
        entry_commission: Optional[float] = None,
        bot_initiated: bool = False,
        peak_price: Optional[float] = None,
    ) -> int:
        """Persist an approved trade to the state store after the order fills.

        Must only be called after Order Executor confirms a fill — not on
        approval.  An approved signal may fail to fill (session closes,
        order rejected by broker, etc.), and recording it prematurely would
        inflate the open risk count and block subsequent valid signals.

        Returns the row id of the new risk_positions record.
        """
        sig = decision.signal
        row_id = st.add_position(
            ticker=sig.ticker,
            instrument_id=sig.instrument_id,
            entry_price=sig.entry_price,
            stop_price=sig.stop_price,
            shares=decision.shares,
            risk_per_share=decision.risk_per_share,
            risk_amount=decision.risk_amount,
            position_risk_pct=decision.position_risk_pct,
            portfolio_value_at_open=decision.portfolio_value,
            liquidity_class=sig.liquidity_class,
            signal_type=sig.signal_type,
            conviction=sig.conviction,
            db_path=self._db_path,
            target_price=sig.target_price,
            run_type=getattr(sig, "run_type", None),
            fill_price=fill_price,
            fill_timestamp=fill_timestamp,
            entry_commission=entry_commission,
            bot_initiated=bot_initiated,
            peak_price=peak_price,
        )
        self._logger.info({
            "event": "position_opened",
            "ticker": sig.ticker,
            "instrument_id": sig.instrument_id,
            "shares": decision.shares,
            "fill_price": fill_price,
            "entry": sig.entry_price,
            "stop": sig.stop_price,
            "risk_amount": decision.risk_amount,
            "position_risk_pct": decision.position_risk_pct,
            "bot_initiated": bot_initiated,
            "row_id": row_id,
        })
        return row_id

    def close_position(self, ticker: str, close_price: float, reason: str) -> bool:
        """Record a position close and calculate realised P&L.

        `reason` must be one of: 'stop' | 'target' | 'trail' | 'manual'.
        Returns True if a matching open position was found and updated.
        Returns False (with a warning log) if no open position existed.

        After a close, the realised P&L feeds into get_daily_realized_pnl()
        which is used by Check 3 (daily loss limit) on the next evaluate() call.
        """
        updated = st.close_position(ticker, close_price, reason, self._db_path)
        if updated:
            self._logger.info({
                "event": "position_closed",
                "ticker": ticker,
                "close_price": close_price,
                "reason": reason,
            })
        else:
            self._logger.warning({
                "event": "close_position_not_found",
                "ticker": ticker,
                "close_price": close_price,
                "reason": reason,
            })
        return updated

    def get_open_risk_summary(self) -> dict:
        """Return a dict summarising current open exposure.

        Used by the CLI status command and for session-start logging.
        All monetary values are in the portfolio's base currency (EUR).
        """
        portfolio_value = self._get_portfolio_value()
        open_positions = st.get_open_positions(self._db_path)
        total_risk_amount = sum(p["risk_amount"] for p in open_positions)
        total_open_risk_pct = calc.open_risk_pct(total_risk_amount, portfolio_value)
        return {
            "portfolio_value": portfolio_value,
            "open_positions": len(open_positions),
            "total_risk_amount": round(total_risk_amount, 2),
            "total_open_risk_pct": total_open_risk_pct,
            "remaining_risk_budget_pct": round(self._max_open_risk_pct - total_open_risk_pct, 4),
            "positions": [
                {
                    "ticker": p["ticker"],
                    "shares": p["shares"],
                    "entry": p["entry_price"],
                    "stop": p["stop_price"],
                    "risk_amount": p["risk_amount"],
                    "position_risk_pct": p["position_risk_pct"],
                    "opened_at": p["opened_at"],
                }
                for p in open_positions
            ],
        }

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _get_portfolio_value(self) -> float:
        """Return the portfolio value used for all position sizing calculations.

        Phase 1 — stub: returns risk.portfolio_value_stub from config.yaml.
        Phase 2 — live: will call IBKR TWS API reqAccountSummary to retrieve
            the account's net liquidation value in real time (RL-04).

        The net liquidation value (cash + market value of open positions) is the
        correct base for risk % calculations because it reflects what the account
        is actually worth at any given moment, not just the starting cash balance.
        """
        return self._portfolio_value_stub
