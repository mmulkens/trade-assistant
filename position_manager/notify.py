# ---------------------------------------------------------------------------
# notify.py — Telegram notification formatting for the Position Manager
#
# Responsibilities:
#   - Format and dispatch Telegram messages for all PM events (PM-18):
#       trail_activated      — stop moved to cost floor; ATR trailing begins
#       trail_updated        — ATR trail moved the stop higher
#       time_exit_fired      — time-based exit closed a stalled position
#       time_exit_hold       — time limit reached but conditions not met; holding
#       manual_exit          — position recorded as manually closed
#
# Transport:
#   Uses the same urllib-based approach as sim_executor/notify.py.  urllib is
#   stdlib and avoids relying on requests as a transitive dependency of yfinance.
#
# Failure policy (same as sim_executor/notify.py):
#   All send failures are caught and logged at WARNING level.  A missed
#   Telegram message never stops position management — the database is always
#   updated first, notifications are best-effort on top.
#
# Caller contract:
#   The PositionManager checks self._notify_enabled (bot_token and chat_id
#   both non-empty) before calling any function in this module.  An empty
#   token is a deliberate configuration choice, not an error to log.
# ---------------------------------------------------------------------------

import json
import urllib.error
import urllib.request
from logging import Logger

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


# ---------------------------------------------------------------------------
# Public notification functions
# ---------------------------------------------------------------------------

def send_trail_activated(
    ticker: str,
    entry_price: float,
    cost_floor: float,
    new_stop: float,
    current_price: float,
    bucket: str,
    atr_pct: float,
    bot_token: str,
    chat_id: str,
    logger: Logger,
) -> None:
    """Notify when the trailing stop is activated for the first time (PM-02/03).

    Sent once, when the position closes above the 1.5R trigger price.
    Tells the operator:
      - The position is now risk-free (stop ≥ cost floor)
      - ATR trailing is live from this day's running high

    The volatility bucket and ATR% are shown so the operator knows how
    wide the trail is (low bucket = tighter trail = follows price more closely).
    """
    text = (
        f"🔵 TRAIL ACTIVATED · {ticker}\n"
        f"Current: €{current_price:,.2f}  (≥ 1.5R trigger)\n"
        f"Stop → €{new_stop:,.2f}  |  Cost floor: €{cost_floor:,.2f}\n"
        f"Entry was: €{entry_price:,.2f}\n"
        f"Volatility: {bucket} bucket  (ATR {atr_pct:.2f}%)"
    )
    _send(text, bot_token, chat_id, logger)


def send_trail_updated(
    ticker: str,
    old_stop: float,
    new_stop: float,
    running_high: float,
    atr_trail_level: float,
    bot_token: str,
    chat_id: str,
    logger: Logger,
) -> None:
    """Notify when the ATR trail has moved the active stop higher.

    Only sent when the stop actually advances — the manager does not call this
    for trivial float-precision differences or when the trail level is below
    the current stop.  The running high shows how far the trade has extended
    since the trail activated.
    """
    moved = new_stop - old_stop
    text = (
        f"⬆️ TRAIL UPDATE · {ticker}\n"
        f"Stop: €{old_stop:,.2f} → €{new_stop:,.2f}  (+€{moved:,.2f})\n"
        f"Running high: €{running_high:,.2f}  |  ATR trail: €{atr_trail_level:,.2f}"
    )
    _send(text, bot_token, chat_id, logger)


def send_time_exit_fired(
    ticker: str,
    close_price: float,
    trading_days: int,
    gross_pnl: float,
    net_pnl: float,
    bot_token: str,
    chat_id: str,
    logger: Logger,
) -> None:
    """Notify when a stalled position is closed by the time-based exit rule (PM-09/11).

    This only fires when all three conditions were met:
        1. trail_triggered == False (trail never activated)
        2. Signal queue non-empty (there is an opportunity cost)
        3. Open risk ≥ 6% (position is consuming constrained risk budget)
        4. Price within stop_proximity_pct of the stop (stop likely incoming)
    """
    pnl_sign = "+" if net_pnl >= 0 else "−"
    pnl_abs = abs(net_pnl)
    gross_sign = "+" if gross_pnl >= 0 else "−"
    gross_abs = abs(gross_pnl)
    text = (
        f"⏱️ TIME EXIT · {ticker}\n"
        f"Closed at €{close_price:,.2f}  after {trading_days} trading days\n"
        f"Gross P&L: {gross_sign}€{gross_abs:,.2f}  |  Net P&L: {pnl_sign}€{pnl_abs:,.2f}"
    )
    _send(text, bot_token, chat_id, logger)


def send_time_exit_hold(
    ticker: str,
    trading_days: int,
    hold_reason: str,
    bot_token: str,
    chat_id: str,
    logger: Logger,
) -> None:
    """Notify when the time limit is reached but exit conditions are not met.

    Keeps the operator aware of ageing positions without forcing a close.
    hold_reason values (from manager.py):
        'no_pending_signals'   — signal queue empty; no opportunity cost
        'open_risk_below_cap'  — risk budget not exhausted; can still add trades
        'not_near_stop'        — price not within proximity threshold; let stop manage exit
    """
    reason_labels = {
        "no_pending_signals":   "Signal queue empty — no opportunity cost",
        "open_risk_below_cap":  "Open risk below 6% cap — risk budget not constrained",
        "not_near_stop":        "Price not near stop — holding for stop-managed exit",
    }
    label = reason_labels.get(hold_reason, hold_reason)
    text = (
        f"⏳ TIME LIMIT REACHED · {ticker}\n"
        f"{trading_days} trading days open — HOLDING\n"
        f"Reason: {label}"
    )
    _send(text, bot_token, chat_id, logger)


def send_manual_exit(
    ticker: str,
    close_price: float,
    net_pnl: float,
    source: str,
    bot_token: str,
    chat_id: str,
    logger: Logger,
) -> None:
    """Notify when a position is recorded as manually closed (PM-13/14).

    'source' distinguishes the detection path:
        'cli'          — operator ran `python -m position_manager close ...`
        'ibkr_event'   — auto-detected via ib_insync positionEvent (Phase 2)
    """
    pnl_sign = "+" if net_pnl >= 0 else "−"
    pnl_abs = abs(net_pnl)
    text = (
        f"🤚 MANUAL EXIT · {ticker}\n"
        f"Closed at €{close_price:,.2f}  |  Net P&L: {pnl_sign}€{pnl_abs:,.2f}\n"
        f"Source: {source}"
    )
    _send(text, bot_token, chat_id, logger)


# ---------------------------------------------------------------------------
# Private transport
# ---------------------------------------------------------------------------

def _send(text: str, bot_token: str, chat_id: str, logger: Logger) -> None:
    """POST a message to the Telegram Bot API (10-second timeout, best-effort).

    Failures are logged at WARNING level and never propagate to the caller —
    a missed notification must not interrupt or roll back a position update.
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
                logger.warning({
                    "event": "telegram_send_failed",
                    "http_status": resp.status,
                })
    except urllib.error.URLError as exc:
        logger.warning({"event": "telegram_send_error", "error": str(exc)})
    except Exception as exc:
        logger.warning({"event": "telegram_send_error", "error": str(exc)})
