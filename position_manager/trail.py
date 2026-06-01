# ---------------------------------------------------------------------------
# trail.py — Pure ATR trailing-stop math for the Position Manager
#
# Responsibilities:
#   - Calculate the cost-break-even floor (entry + 2× TOB)
#   - Classify a stock's volatility bucket from its ATR% reading
#   - Map a volatility bucket to the corresponding ATR multiplier from config
#   - Compute the raw ATR trail level from the running high
#   - Compute the active stop as max(cost_floor, atr_trail_level)
#   - Test whether the trail trigger price has been reached
#   - Calculate price proximity to stop (for time-exit gate PM-11)
#
# Design rules (Section 11 of trade_assistant_design.md):
#   - Stop only ever moves up — the caller (manager.py) is responsible for
#     enforcing the "never down" invariant before calling update_position_stop().
#     These functions are pure math and do not apply that rule themselves.
#   - Trail activates at trail_trigger_r × initial_risk above entry (default 1.5R).
#     The cost floor is applied simultaneously, making the position risk-free
#     from the moment the trail triggers.
#   - ATR multipliers are tiered by volatility bucket to avoid noise-stops on
#     high-beta names while keeping tight trails on low-volatility stocks.
#
# What this module does NOT do:
#   - Read from or write to any database
#   - Call any external APIs
#   - Enforce the "stop never moves down" invariant (manager.py owns that)
#   - Log anything (all logging is in manager.py)
# ---------------------------------------------------------------------------


def calc_cost_floor(entry_price: float, tob_pct: float) -> float:
    """Minimum exit price that covers both sides of the TOB transaction cost.

    The Belgian Stock Exchange Transaction Tax (TOB) applies to both the
    entry and exit leg.  The cost floor is the exit price at which the net
    proceeds after paying both TOBs are exactly zero — i.e. the break-even
    point inclusive of transaction costs:

        cost_floor = entry_price × (1 + 2 × tob_pct / 100)

    Example:  entry = €100.00, tob_pct = 0.35
        cost_floor = 100 × (1 + 0.007) = €100.70

    When the stop is moved here at 1.5R (PM-02), the worst-case outcome on
    this position is approximately zero net loss regardless of where the
    trailing stop eventually triggers — all further capital is protected.
    """
    return round(entry_price * (1.0 + 2.0 * tob_pct / 100.0), 4)


def classify_volatility_bucket(
    atr_pct: float,
    low_threshold_pct: float,
    high_threshold_pct: float,
) -> str:
    """Classify a stock's daily volatility into one of three named buckets.

    ATR% = ATR14 / current_close × 100.  Thresholds come from config section
    position_manager.atr_buckets:
        low_threshold_pct   (default 1.5%)
        high_threshold_pct  (default 3.0%)

    Returns:
        'low'    — ATR% < low_threshold_pct  → tight trail (2.0× ATR default)
        'medium' — low ≤ ATR% < high         → standard trail (2.5× ATR default)
        'high'   — ATR% ≥ high_threshold_pct → loose trail (3.0× ATR default)

    Rationale: low-volatility stocks are steadier compounders — a tight trail
    captures more of the move.  High-beta names need a wider trail to survive
    intraday swings without being stopped out on normal noise.
    """
    if atr_pct < low_threshold_pct:
        return "low"
    if atr_pct < high_threshold_pct:
        return "medium"
    return "high"


def get_atr_multiplier(bucket: str, atr_buckets_cfg: dict) -> float:
    """Return the ATR multiplier for the given volatility bucket.

    Reads from the position_manager.atr_buckets section of config.yaml:
        low_multiplier    (default 2.0)
        medium_multiplier (default 2.5)
        high_multiplier   (default 3.0)

    Raises ValueError for any unrecognised bucket name so that a typo or
    config error surfaces immediately rather than silently applying the wrong
    multiplier to a live position.
    """
    if bucket == "low":
        return float(atr_buckets_cfg["low_multiplier"])
    if bucket == "medium":
        return float(atr_buckets_cfg["medium_multiplier"])
    if bucket == "high":
        return float(atr_buckets_cfg["high_multiplier"])
    raise ValueError(f"Unknown volatility bucket: {bucket!r}")


def calc_atr_trail_level(running_high: float, atr14: float, multiplier: float) -> float:
    """Compute the raw ATR-based trailing stop level.

        trail_level = running_high − (atr14 × multiplier)

    'running_high' is the highest close price seen since the position was
    opened (stored as peak_price in risk_positions, updated daily by the
    manager).  The trail follows the running high and never reverses — the
    caller is responsible for comparing this value against the current stop
    before committing any update to the database.

    Edge case: this can return a value ≤ 0 if ATR is extremely large relative
    to the running high (exotic or illiquid instruments).  The manager guards
    against setting a stop at or below zero before calling update_position_stop.
    """
    return round(running_high - atr14 * multiplier, 4)


def calc_active_stop(cost_floor: float, atr_trail_level: float) -> float:
    """Active stop = the higher of the cost floor and the raw ATR trail level.

    The cost floor is the lower bound: once the trail is active the stop
    cannot be placed below the break-even level (PM-06 design requirement).
    As the position advances and the running high rises, the ATR trail level
    eventually overtakes the cost floor and becomes the binding constraint.

        active_stop = max(cost_floor, atr_trail_level)
    """
    return round(max(cost_floor, atr_trail_level), 4)


def is_trail_trigger_reached(
    current_price: float,
    entry_price: float,
    risk_per_share: float,
    trail_trigger_r: float,
) -> bool:
    """Return True once the position has gained enough to activate the trail.

    The trail activates when:
        current_price >= entry_price + (trail_trigger_r × risk_per_share)

    With the default trail_trigger_r = 1.5 and risk_per_share = €5:
        A stock entered at €100 triggers at €107.50.

    When this returns True, manager.py simultaneously:
        1. Moves the stop to the cost floor (PM-02)
        2. Begins ATR trailing from the running high (PM-03)
    """
    trigger_price = entry_price + trail_trigger_r * risk_per_share
    return current_price >= trigger_price


def calc_stop_proximity_ratio(
    current_price: float,
    stop_price: float,
    entry_price: float,
) -> float:
    """Return how close the current price is to the stop, as a 0–1+ ratio.

    proximity_ratio = (current_price − stop_price) / (entry_price − stop_price)

    Interpretation:
        0.0  — price is at the stop (would be stopped out now)
        1.0  — price is back at entry (no profit, no loss vs initial stop)
        >1.0 — price is in profit territory above entry

    The time-exit logic (PM-11) uses this to decide whether a stalled position
    is close enough to its stop to justify closing early:
        proximity_ratio <= stop_proximity_pct / 100  →  close (stop likely coming)
        otherwise                                    →  hold (let stop manage exit)

    Guard: if entry_price == stop_price the denominator would be zero (corrupt
    position data).  Return 1.0 in that case to signal "not near stop" and
    prevent an accidental time-exit close on bad data.
    """
    denominator = entry_price - stop_price
    if abs(denominator) < 1e-8:
        return 1.0
    return (current_price - stop_price) / denominator
