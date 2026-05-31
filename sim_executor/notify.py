# ---------------------------------------------------------------------------
# notify.py — Telegram notification formatting for sim fills and system alerts
#
# Written as a standalone module so the real Order Executor can import it
# directly without modification.  The only difference in live mode is that
# the caller omits is_sim=True (or passes False) to drop the SIM prefix.
# ---------------------------------------------------------------------------

import json
import urllib.error
import urllib.parse
import urllib.request
from logging import Logger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from risk_layer.layer import RiskDecision

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def send_fill_notification(
    decision: "RiskDecision",
    bot_token: str,
    chat_id: str,
    logger: Logger,
    is_sim: bool = True,
) -> None:
    """Send one Telegram message summarising an approved fill.

    Format (is_sim=True):
        🟢 SIM FILL · ASML.AS
        Strategy: breakout · Elevated conviction
        Entry: €1,424.60 · Stop: €1,310.63 · Target: €1,652.54
        Shares: 5 · Risk: €569.85 (1.1%)
        Open risk: 3.6% → 4.7%
    """
    sig = decision.signal
    prefix = "SIM FILL" if is_sim else "FILL"
    conviction_label = sig.conviction.capitalize() + " conviction"

    text = (
        f"🟢 {prefix} · {sig.ticker}\n"
        f"Strategy: {sig.signal_type} · {conviction_label}\n"
        f"Entry: €{sig.entry_price:,.2f} · Stop: €{sig.stop_price:,.2f} · Target: €{sig.target_price:,.2f}\n"
        f"Shares: {decision.shares} · Risk: €{decision.risk_amount:,.2f} ({decision.position_risk_pct:.1f}%)\n"
        f"Open risk: {decision.current_open_risk_pct:.1f}% → {decision.projected_open_risk_pct:.1f}%"
    )
    _send(text, bot_token, chat_id, logger)


def send_system_alert(
    message: str,
    bot_token: str,
    chat_id: str,
    logger: Logger,
) -> None:
    """Send a free-text system alert (e.g. daily loss limit breach)."""
    _send(message, bot_token, chat_id, logger)


def _send(text: str, bot_token: str, chat_id: str, logger: Logger) -> None:
    url = _API_BASE.format(token=bot_token)
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning({
                    "event": "telegram_send_failed",
                    "http_status": resp.status,
                })
    except urllib.error.URLError as exc:
        logger.warning({"event": "telegram_send_error", "error": str(exc)})
    except Exception as exc:
        logger.warning({"event": "telegram_send_error", "error": str(exc)})
