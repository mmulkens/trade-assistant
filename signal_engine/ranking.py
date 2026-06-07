# ---------------------------------------------------------------------------
# ranking.py — Signal ranking for the Signal Engine
#
# Ranking rule:
#   1. Elevated conviction first (both strategies fired, or near 52-week high)
#   2. Within the same conviction tier: tightest stop distance (% of entry)
#
# Tighter stop = smaller risk per share relative to entry = better capital
# efficiency.  Two signals at the same conviction tier but different stop
# distances: the one with the tighter stop gets entered first, leaving more
# risk budget for subsequent signals.
#
# Called by SignalEngine.scan() — applies to both live scans (signals.db)
# and walk-forward simulation scans (wf_signals in wf_sim.db).
# ---------------------------------------------------------------------------

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Signal


def rank_signals(signals: list[Signal]) -> list[Signal]:
    """Return signals sorted: elevated conviction first, tightest stop% within tier.

    Pure function — does not modify the input list.
    """
    def _key(s: Signal) -> tuple[int, float]:
        tier     = 0 if s.conviction == "elevated" else 1
        stop_pct = (s.entry_price - s.stop_price) / s.entry_price if s.entry_price > 0 else 1.0
        return (tier, stop_pct)

    return sorted(signals, key=_key)
