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
#   get_portfolio_value() returns the YAML config value.
#   Daily loss limit checks only cover realised P&L (unrealised requires live
#   prices from TWS); both will be wired in Phase 2.
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
    approved: bool
    signal: "Signal"
    shares: int                        # 0 when rejected
    risk_per_share: float              # entry − stop
    risk_amount: float                 # shares × risk_per_share (currency)
    position_risk_pct: float           # risk_amount / portfolio_value × 100
    effective_cap_pct: float           # cap used (reduced for thin instruments)
    current_open_risk_pct: float       # total open risk before this trade
    projected_open_risk_pct: float     # total open risk if this trade is taken
    portfolio_value: float             # value used for all calculations
    reject_reason: Optional[str]       # None when approved


# ---------------------------------------------------------------------------
# RiskLayer
# ---------------------------------------------------------------------------

class RiskLayer:
    """Evaluates signals against portfolio risk constraints.

    Typical call sequence per session:
        1. engine.scan()         → list[Signal]
        2. risk.evaluate(signal) → RiskDecision  (for each signal)
        3. order_executor.submit(decision)       (only if approved)
        4. risk.open_position(decision)          (after fill confirmed)
        ...later...
        5. risk.close_position(ticker, price, reason)
    """

    def __init__(self, config: dict, logger: Logger) -> None:
        self._logger = logger
        rl = config["risk"]
        self._max_position_risk_pct: float = rl["max_position_risk_pct"]
        self._max_open_risk_pct: float = rl["max_open_risk_pct"]
        self._daily_loss_limit_pct: float = rl["daily_loss_limit_pct"]
        self._portfolio_value_stub: float = rl["portfolio_value_stub"]
        self._thin_size_multiplier: float = rl.get("thin_size_multiplier", 0.5)
        self._db_path: str = rl["db_path"]

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def evaluate(self, signal: "Signal") -> RiskDecision:
        """Run all pre-trade checks and return a RiskDecision.

        Checks run in order of cheapness/importance:
            1. Trading paused (daily loss limit previously triggered today)
            2. Duplicate instrument
            3. Daily loss limit (realised P&L only until IBKR wired)
            4. Position sizing
            5. Per-trade risk hard cap  (RL-01)
            6. Total open risk hard cap (RL-02)
        """
        portfolio_value = self._get_portfolio_value()
        current_open_risk_amount = st.get_open_risk_amount(self._db_path)
        current_open_risk_pct = calc.open_risk_pct(current_open_risk_amount, portfolio_value)

        def _reject(reason: str) -> RiskDecision:
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

        # --- Check 1: trading paused ---
        if st.is_trading_paused(self._db_path):
            return _reject("trading_paused:daily_loss_limit_reached")

        # --- Check 2: duplicate instrument ---
        if st.has_open_position(signal.ticker, self._db_path):
            return _reject("duplicate_instrument")

        # --- Check 3: daily loss limit (realised only, Phase 1) ---
        daily_pnl = st.get_daily_realized_pnl(self._db_path)
        daily_loss_pct = (daily_pnl / portfolio_value) * 100  # negative means loss
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

        # --- Check 4: position sizing ---
        sizing = calc.size_position(
            entry=signal.entry_price,
            stop=signal.stop_price,
            portfolio_value=portfolio_value,
            max_position_risk_pct=self._max_position_risk_pct,
            liquidity_class=signal.liquidity_class,
            thin_size_multiplier=self._thin_size_multiplier,
        )

        if sizing.shares == 0:
            return _reject("zero_shares:risk_per_share_too_large_or_invalid_prices")

        # --- Check 5: per-trade hard cap (RL-01) ---
        # floor() guarantees we never exceed the cap, but we verify explicitly
        # because this rule must never be overridden.
        if sizing.position_risk_pct > self._max_position_risk_pct + 0.001:
            return _reject(
                f"position_risk_cap_exceeded:"
                f"{sizing.position_risk_pct:.3f}pct_vs_limit_{self._max_position_risk_pct}pct"
            )

        # --- Check 6: total open risk hard cap (RL-02) ---
        projected_open_risk_pct = calc.open_risk_pct(
            current_open_risk_amount + sizing.risk_amount, portfolio_value
        )
        if projected_open_risk_pct > self._max_open_risk_pct:
            return _reject(
                f"open_risk_cap_exceeded:"
                f"projected_{projected_open_risk_pct:.3f}pct_vs_limit_{self._max_open_risk_pct}pct"
            )

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

    def open_position(self, decision: RiskDecision) -> int:
        """Persist an approved trade to the state store after the order fills.

        Returns the row id of the new position record.
        Must only be called after Order Executor confirms a fill.
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
        )
        self._logger.info({
            "event": "position_opened",
            "ticker": sig.ticker,
            "instrument_id": sig.instrument_id,
            "shares": decision.shares,
            "entry": sig.entry_price,
            "stop": sig.stop_price,
            "risk_amount": decision.risk_amount,
            "position_risk_pct": decision.position_risk_pct,
            "row_id": row_id,
        })
        return row_id

    def close_position(self, ticker: str, close_price: float, reason: str) -> bool:
        """Record a position close and realised P&L.

        Returns True if a matching open position was found and updated.
        `reason` is one of: 'stop' | 'target' | 'trail' | 'manual'.
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
        """Return a dict summarising current open exposure (for logging/display)."""
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
        """Return portfolio value for sizing calculations.

        Phase 1: returns the stub value from config.yaml (risk.portfolio_value_stub).
        Phase 2: will call IBKR TWS API reqAccountSummary to get live net liquidation
                 value (RL-04).
        """
        return self._portfolio_value_stub
