# ---------------------------------------------------------------------------
# fills.py — Simulated fill writer
#
# Builds the fill record and calls risk_layer.open_position().
# Entry is always at signal.entry_price — no slippage modelling.
# Commission is a flat TOB estimate (entry_price × shares × tob_pct / 100).
# ---------------------------------------------------------------------------

from datetime import datetime, timezone
from logging import Logger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from risk_layer.layer import RiskDecision, RiskLayer


def record_fill(
    risk_layer: "RiskLayer",
    decision: "RiskDecision",
    tob_pct: float,
    logger: Logger,
) -> int:
    """Write a simulated position to risk.db after Risk Layer approval.

    Returns the row id of the new risk_positions record.
    """
    fill_price = decision.signal.entry_price
    fill_timestamp = datetime.now(timezone.utc).isoformat()
    entry_commission = round(fill_price * decision.shares * (tob_pct / 100), 2)

    row_id = risk_layer.open_position(
        decision,
        fill_price=fill_price,
        fill_timestamp=fill_timestamp,
        entry_commission=entry_commission,
        bot_initiated=True,
        peak_price=fill_price,
    )

    logger.info({
        "event": "sim_fill_recorded",
        "ticker": decision.signal.ticker,
        "row_id": row_id,
        "fill_price": fill_price,
        "shares": decision.shares,
        "entry_commission": entry_commission,
    })
    return row_id
