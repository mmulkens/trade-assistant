# ---------------------------------------------------------------------------
# calculator.py — Position sizing and risk arithmetic
#
# Pure functions with no I/O or state.  All sizing decisions in layer.py
# call these functions so the math can be unit-tested in isolation.
#
# All functions take explicit arguments rather than reading from config —
# the caller (RiskLayer) is responsible for passing the right values, which
# makes the math readable and the functions reusable in tests and notebooks.
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from math import floor


@dataclass
class SizingResult:
    shares: int
    risk_per_share: float       # entry − stop
    risk_amount: float          # shares × risk_per_share  (currency, e.g. EUR)
    position_risk_pct: float    # risk_amount / portfolio_value × 100
    effective_cap_pct: float    # cap that was actually applied (reduced for thin)


def size_position(
    entry: float,
    stop: float,
    portfolio_value: float,
    max_position_risk_pct: float,
    liquidity_class: str,
    thin_size_multiplier: float,
    max_risk_amount_override: float | None = None,
) -> SizingResult:
    """Calculate position size so that risk stays within the per-trade cap.

    Formula (RL-03 from trade_assistant_design.md):
        risk_per_share  = entry - stop
        max_risk_amount = portfolio_value × (effective_cap_pct / 100)
        shares          = floor(max_risk_amount / risk_per_share)

    Why floor() and not round()?
        We are sizing to a HARD MAXIMUM risk amount.  Rounding up would give one
        extra share that pushes actual risk above the cap.  floor() is the only
        function that guarantees we never exceed it — even by a fraction.

    Why a reduced cap for thin instruments? (RL-09)
        Thin names (average daily turnover < €1M) carry spread risk, slippage,
        and potential impact cost that are not captured in the ATR-based stop.
        Committing a full 1.5% to an illiquid name understates the true cost of
        the trade.  Halving the position (thin_size_multiplier = 0.5) reduces
        sizing to 0.75% risk while still allowing the trade to be taken.

    `max_risk_amount_override` bypasses the percentage-based cap and sizes
    directly to the supplied dollar amount.  Used by the partial-fill path in
    RiskLayer.evaluate() when remaining open-risk room is smaller than the
    standard per-trade cap.  The thin_size_multiplier is still applied on top
    so thin instruments never consume more than their proportional share of
    the remaining room.
    """
    risk_per_share = entry - stop

    # Reject impossible inputs rather than silently returning a wrong result
    if risk_per_share <= 0 or portfolio_value <= 0:
        return SizingResult(
            shares=0,
            risk_per_share=round(risk_per_share, 4),
            risk_amount=0.0,
            position_risk_pct=0.0,
            effective_cap_pct=0.0,
        )

    if max_risk_amount_override is not None:
        # Partial-fill path: use the supplied dollar cap directly.
        # Still apply the thin multiplier — thin instruments are half-sized
        # even when filling remaining room.
        multiplier = thin_size_multiplier if liquidity_class == "thin" else 1.0
        max_risk_amount = max_risk_amount_override * multiplier
        effective_cap_pct = (max_risk_amount / portfolio_value) * 100
    else:
        # Normal path: derive cap from percentage config.
        effective_cap_pct = (
            max_position_risk_pct * thin_size_multiplier
            if liquidity_class == "thin"
            else max_position_risk_pct
        )
        max_risk_amount = portfolio_value * (effective_cap_pct / 100)

    # floor() ensures actual risk never exceeds the hard cap (see docstring)
    shares = floor(max_risk_amount / risk_per_share)

    actual_risk = shares * risk_per_share
    position_risk_pct = (actual_risk / portfolio_value) * 100

    return SizingResult(
        shares=shares,
        risk_per_share=round(risk_per_share, 4),
        risk_amount=round(actual_risk, 2),
        position_risk_pct=round(position_risk_pct, 4),
        effective_cap_pct=round(effective_cap_pct, 4),
    )


def open_risk_pct(total_risk_amount: float, portfolio_value: float) -> float:
    """Return total open risk across all positions as a percentage of portfolio value.

    Used both to check RL-02 (6% hard cap) and for status/logging display.
    total_risk_amount is the sum of risk_amount across all open risk_positions rows.
    """
    if portfolio_value <= 0:
        return 0.0
    return round((total_risk_amount / portfolio_value) * 100, 4)
