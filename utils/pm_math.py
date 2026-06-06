# ---------------------------------------------------------------------------
# pm_math.py — Shared ATR trailing-stop math (Position Manager + Walk-Forward Sim)
#
# Responsibilities:
#   - Cost break-even floor (entry + 2× TOB)
#   - Classify volatility bucket from ATR% reading
#   - Map volatility bucket → ATR multiplier from config
#   - Compute raw ATR trail level from running high
#   - Compute active stop as max(cost_floor, atr_trail_level)
#   - Test whether the 1.5R trail trigger price has been reached
#   - Calculate price proximity to stop (for time-exit gate PM-11)
#
# Design rules:
#   - All functions are pure math: no DB access, no logging, no side effects.
#   - Stop only ever moves up — callers are responsible for enforcing the
#     "never down" invariant before committing any update to the database.
#   - Imported by both position_manager/manager.py (live) and
#     walk_forward/sim_pm.py (simulation); changes here affect both paths.
# ---------------------------------------------------------------------------


def cost_floor(entry_price: float, shares: int, tob_pct: float) -> float:
    """Minimum exit price that covers both sides of the TOB transaction cost.

    Belgian TOB applies to both entry and exit legs.  The cost floor is the
    exit price at which net proceeds after paying both TOBs are exactly zero:

        cost_floor = entry_price × (1 + 2 × tob_pct / 100)

    The shares parameter is reserved for a future refinement where the
    per-share cost model also accounts for fixed ticket charges.

    Example:  entry = €100.00, tob_pct = 0.35
        cost_floor = 100 × (1 + 0.007) = €100.70
    """
    return round(entry_price * (1.0 + 2.0 * tob_pct / 100.0), 4)


def classify_volatility_bucket(
    atr_pct: float,
    low_threshold_pct: float,
    high_threshold_pct: float,
) -> str:
    """Classify daily volatility into one of three named buckets.

    ATR% = ATR14 / current_close × 100.  Thresholds from config
    position_manager.atr_buckets:
        low_threshold_pct   (default 1.5%)
        high_threshold_pct  (default 3.0%)

    Returns:
        'low'    — ATR% < low_threshold_pct  → tight trail (2.0× ATR default)
        'medium' — low ≤ ATR% < high         → standard trail (2.5× ATR default)
        'high'   — ATR% ≥ high_threshold_pct → loose trail (3.0× ATR default)
    """
    if atr_pct < low_threshold_pct:
        return "low"
    if atr_pct < high_threshold_pct:
        return "medium"
    return "high"


def atr_multiplier(bucket: str, atr_buckets_cfg: dict) -> float:
    """Return the ATR multiplier for the given volatility bucket.

    Reads from position_manager.atr_buckets in config.yaml:
        low_multiplier    (default 2.0)
        medium_multiplier (default 2.5)
        high_multiplier   (default 3.0)

    Raises ValueError for any unrecognised bucket name.
    """
    if bucket == "low":
        return float(atr_buckets_cfg["low_multiplier"])
    if bucket == "medium":
        return float(atr_buckets_cfg["medium_multiplier"])
    if bucket == "high":
        return float(atr_buckets_cfg["high_multiplier"])
    raise ValueError(f"Unknown volatility bucket: {bucket!r}")


def atr_trail_level(running_high: float, atr14: float, multiplier: float) -> float:
    """Compute the raw ATR-based trailing stop level.

        trail_level = running_high − (atr14 × multiplier)

    'running_high' is the highest close since entry (stored as peak_price).
    The trail follows the running high and never reverses — the caller must
    compare this value against the current stop before committing any DB update.
    """
    return round(running_high - atr14 * multiplier, 4)


def active_stop(cost_floor_price: float, atr_trail: float) -> float:
    """Active stop = max(cost floor, raw ATR trail level).

    The cost floor is the lower bound: once trailing is active the stop cannot
    be placed below break-even.  As the running high rises, the ATR trail level
    eventually overtakes the cost floor and becomes the binding constraint.
    """
    return round(max(cost_floor_price, atr_trail), 4)


def trail_trigger_reached(
    current_price: float,
    entry_price: float,
    risk_per_share: float,
    trail_trigger_r: float,
) -> bool:
    """Return True once the position has gained enough to activate the trail.

    Trail activates when:
        current_price >= entry_price + (trail_trigger_r × risk_per_share)

    With trail_trigger_r=1.5 and risk_per_share=€5, a €100 entry triggers at €107.50.
    """
    trigger_price = entry_price + trail_trigger_r * risk_per_share
    return current_price >= trigger_price


def stop_proximity_ratio(
    current_price: float,
    stop_price: float,
    entry_price: float,
) -> float:
    """Return how close the current price is to the stop, as a 0–1+ ratio.

        proximity_ratio = (current_price − stop_price) / (entry_price − stop_price)

    Interpretation:
        0.0  — price is at the stop
        1.0  — price is back at entry
        >1.0 — price is in profit territory

    Guard: if entry_price == stop_price the denominator would be zero (corrupt
    position data).  Returns 1.0 in that case to prevent an accidental time-exit.
    """
    denominator = entry_price - stop_price
    if abs(denominator) < 1e-8:
        return 1.0
    return (current_price - stop_price) / denominator
