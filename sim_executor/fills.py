# ---------------------------------------------------------------------------
# fills.py — Simulated fill writer
#
# Responsibilities:
#   - Construct the fill record from a Risk Layer decision
#   - Calculate the entry commission estimate
#   - Call risk_layer.open_position() to persist the position
#
# What this module intentionally does NOT do:
#   - Model fill slippage: entry is always at signal.entry_price.  The sim
#     executor's value is in exercising Risk Layer logic and producing a
#     realistic position state — not in modelling execution quality.  The
#     real Order Executor will have actual fill prices from IBKR confirms.
#   - Validate R:R after costs: the signal's R:R is trusted as-is.  Cost
#     revalidation belongs to the real Order Executor.
#   - Look up the IBKR fee schedule: entry_commission uses a flat TOB
#     (Belgian Tax on Stock Exchange Transactions) estimate.  The full IBKR
#     fee schedule (tiered commission + exchange fees + clearing) requires
#     live position data and will be implemented in the real executor.
#
# Commission formula:
#   entry_commission = fill_price × shares × (tob_pct / 100)
#   tob_pct is read from config.yaml (costs.tob_pct, currently 0.35%).
#   This is a per-side flat rate; exit commission is calculated the same way
#   by the Position Manager when the trade closes.
#
# peak_price initialisation:
#   peak_price is set to fill_price at open.  The Position Manager updates it
#   on every monitoring cycle as the position moves in our favour.  It is
#   used to determine when the trailing stop trigger level (trail_trigger_r)
#   has been reached and to calculate the trail stop distance.
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
    """Persist a simulated fill to risk.db after Risk Layer approval.

    Constructs all fill-time fields and delegates the actual DB write to
    risk_layer.open_position().  The separation of concerns is deliberate:
    fills.py owns "what did the fill look like", risk_layer owns "how is
    the position represented in the state store".

    Returns the row id of the new risk_positions record.
    """
    # -------------------------------------------------------------------
    # Fill price — always the signal's entry_price in sim mode.
    # No buffer, no slippage, no market-impact adjustment.
    # -------------------------------------------------------------------
    fill_price = decision.signal.entry_price

    # -------------------------------------------------------------------
    # Fill timestamp — current UTC wall time, not signal_timestamp.
    # signal_timestamp is when the Signal Engine fired the signal (end of
    # EOD scan).  fill_timestamp is when SX processed it — these will
    # differ by however long the scan took and how many signals precede
    # this one in the batch.
    # -------------------------------------------------------------------
    fill_timestamp = datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------------------------
    # Entry commission — flat TOB estimate.
    # tob_pct / 100 converts the percentage to a decimal multiplier.
    # Rounded to 2 decimal places (cent precision) to match P&L accounting.
    # -------------------------------------------------------------------
    entry_commission = round(fill_price * decision.shares * (tob_pct / 100), 2)

    row_id = risk_layer.open_position(
        decision,
        fill_price=fill_price,
        fill_timestamp=fill_timestamp,
        entry_commission=entry_commission,
        bot_initiated=True,    # always True for SX — all fills are automated
        peak_price=fill_price, # initialised to entry; Position Manager updates
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
