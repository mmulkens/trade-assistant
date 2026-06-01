# ---------------------------------------------------------------------------
# notify.py — Telegram notification formatting for fills and system alerts
#
# Responsibilities:
#   - Format and dispatch Telegram messages for approved fills
#   - Format and dispatch system-level alert messages (trading paused, etc.)
#   - Provide a reusable transport layer for the real Order Executor
#
# Design intent — standalone module:
#   This module is written as a standalone module from the start so the real
#   Order Executor can import it directly without modification.  The only
#   caller-side difference between sim and live is the is_sim flag on
#   send_fill_notification(), which controls the "SIM FILL" vs "FILL" prefix.
#   All formatting, error handling, and transport logic is shared.
#
# Why urllib (not requests):
#   This project's declared dependencies do not include requests as a direct
#   dependency — it happens to be available because yfinance depends on it,
#   but relying on a transitive dependency is fragile.  urllib.request is
#   stdlib and requires no installation.  The Telegram Bot API only needs a
#   single POST endpoint, so there is no benefit to a higher-level HTTP lib.
#
# Failure policy:
#   Notifications are best-effort.  A failed send is logged at WARNING level
#   and the process continues.  A missed Telegram message should never crash
#   the bot or abort a processing run — the fill is already recorded in the
#   database, which is the authoritative record.
#
# Token absent = notifications intentionally off:
#   If telegram_bot_token is empty in config.yaml, the caller is responsible
#   for not calling these functions (SimExecutor checks self._notify_enabled
#   before calling).  An empty token is a deliberate configuration choice,
#   not an error worth logging.
# ---------------------------------------------------------------------------

import json
import urllib.error
import urllib.request
from logging import Logger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from risk_layer.layer import RiskDecision

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_fill_notification(
    decision: "RiskDecision",
    bot_token: str,
    chat_id: str,
    logger: Logger,
    is_sim: bool = True,
) -> None:
    """Send one Telegram message summarising an approved fill.

    The SIM prefix is present on every simulated fill so that sim and live
    fills are unambiguously distinguishable in the notification history —
    especially important during the period when both modes may run in
    parallel for validation purposes.

    Message format (is_sim=True):
        🟢 SIM FILL · ASML.AS
        Strategy: breakout · Elevated conviction
        Entry: €1,424.60 · Stop: €1,310.63 · Target: €1,652.54
        Shares: 5 · Risk: €569.85 (1.1%)
        Open risk: 3.6% → 4.7%

    The open risk line shows the before→after change so the operator can
    assess how much of the 6% risk budget this fill consumed.
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
    """Send a free-text system alert that requires operator attention.

    Used for conditions that are not routine risk filtering — specifically
    the daily loss limit breach and trading paused states, where the operator
    may want to investigate before the next session begins.  These are the
    only rejection codes that warrant a notification; all other rejections
    (duplicate, cap exceeded, etc.) are routine and logged to file only.
    """
    _send(message, bot_token, chat_id, logger)


# ---------------------------------------------------------------------------
# Private transport
# ---------------------------------------------------------------------------

def _send(text: str, bot_token: str, chat_id: str, logger: Logger) -> None:
    """POST a message to the Telegram Bot API.

    Uses a 10-second timeout to avoid hanging if the API is slow.  Failures
    are caught and logged at WARNING level — they never propagate to the
    caller, because a notification failure must not abort a fill that has
    already been committed to the database.
    """
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
                # Non-200 from Telegram usually means a bad token or chat_id.
                # Log it so the operator can fix the config; do not raise.
                logger.warning({
                    "event": "telegram_send_failed",
                    "http_status": resp.status,
                })
    except urllib.error.URLError as exc:
        # Network error, DNS failure, timeout, etc.
        logger.warning({"event": "telegram_send_error", "error": str(exc)})
    except Exception as exc:
        # Catch-all: unexpected failures should never crash the process.
        logger.warning({"event": "telegram_send_error", "error": str(exc)})
