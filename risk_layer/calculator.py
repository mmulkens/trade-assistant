# ---------------------------------------------------------------------------
# calculator.py — Position sizing and risk arithmetic
#
# Pure functions with no I/O or state.  All checks in layer.py call these
# functions so the math can be unit-tested in isolation.
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from math import floor


@dataclass
class SizingResult:
    shares: int
    risk_per_share: float       # entry - stop
    risk_amount: float          # shares × risk_per_share
    position_risk_pct: float    # risk_amount / portfolio_value × 100
    effective_cap_pct: float    # cap used (reduced for thin instruments)


def size_position(
    entry: float,
    stop: float,
    portfolio_value: float,
    max_position_risk_pct: float,
    liquidity_class: str,
    thin_size_multiplier: float,
) -> SizingResult:
    """Calculate position size so that risk stays within the per-trade cap.

    Formula (RL-03):
        risk_per_share = entry - stop
        shares = floor((portfolio_value × cap_pct / 100) / risk_per_share)

    Thin instruments receive a reduced cap (RL-09) so that less capital is
    committed to names with wider spreads and lower liquidity.
    """
    risk_per_share = entry - stop

    if risk_per_share <= 0 or portfolio_value <= 0:
        return SizingResult(
            shares=0,
            risk_per_share=round(risk_per_share, 4),
            risk_amount=0.0,
            position_risk_pct=0.0,
            effective_cap_pct=0.0,
        )

    effective_cap_pct = (
        max_position_risk_pct * thin_size_multiplier
        if liquidity_class == "thin"
        else max_position_risk_pct
    )

    max_risk_amount = portfolio_value * (effective_cap_pct / 100)
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
    """Return total open risk as a percentage of portfolio value."""
    if portfolio_value <= 0:
        return 0.0
    return round((total_risk_amount / portfolio_value) * 100, 4)
