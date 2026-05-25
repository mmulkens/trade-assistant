# Signal Engine — Refinement Specification
_Identified during first live scan: 2026-05-25_
_Feed this to the coding session in VS Code to resolve signal quality issues._
_No code from prior sessions referenced — pseudocode only._

---

## Background

First live scan produced 51 signals across Strategy A (EMA Pullback) and
Strategy B (Breakout). Four quality issues were identified through visual
chart inspection and signal output review.

---

## Issue 1 — Downtrending stocks passing the trend filter

**Observed:** IVG.MI fired as a pullback signal despite being in a clear
downtrend (price had dropped ~30% in recent weeks).

**Root cause:** The moving average stack check (price > MA50 > MA150 > MA200)
uses lagging indicators. After a sharp drop, the MA ordering can persist for
several bars before the slower MAs catch up to the price collapse. During
this window, a stock in freefall can technically pass the trend gate.

**Fix — add two additional guards to the trend filter:**

Guard A: The 21 EMA must itself be above MA50.
The fast EMA21 reprices quickly. In a downtrending stock it will already be
below MA50 while the slower MAs are still catching up. This is the most
effective single guard against this class of false signal.

Guard B (optional, add if Guard A alone is insufficient): Price must not be
more than X% below its N-day high (e.g. more than 20% below the 20-day high).
This catches freefall situations before any MA has adjusted.

```
PSEUDO:
ema21_above_ma50 = EMA21 > MA50
→ if false: fail trend filter

optional:
drawdown_from_recent_high = (highest_high_last_20_bars - price) / highest_high_last_20_bars
→ if drawdown > threshold (e.g. 0.20): fail trend filter
```

---

## Issue 2 — MACD pullback confirmation too loose

**Observed:** IVG.MI's MACD histogram had been green (positive) for several
bars, yet it passed the pullback MACD confirmation check.

**Root cause:** The confirmation only checked that the histogram improved
from the previous bar (yesterday < today). It did not verify:
- That the histogram is still negative (momentum recovering, not already extended)
- That there was an actual prior downtrend in the histogram to reverse

This means the check fires on already-positive histograms (wrong market
condition for a pullback entry) and on single-bar noise with no prior decline.

**What we actually want:**
The histogram should have been declining (getting more negative) for at least
one prior bar, and is now starting to recover toward zero — still negative,
but less so. This is the "dark red transitioning to light red" pattern.
Three bars of histogram are needed to confirm this shape.

```
PSEUDO:
hist_minus_3 → hist_minus_2: declining (hist[-2] < hist[-3])  ← prior downtrend
hist_minus_2 → hist_minus_1: improving (hist[-1] > hist[-2])  ← downtrend ending
hist_minus_1 < 0                                               ← still in weakness

All three must be true for MACD pullback confirmation to pass.
```

**Note:** Two consecutive improving bars (the stricter version) are NOT
required. One improving bar is sufficient — provided it was preceded by at
least one declining bar, and the histogram remains negative.

---

## Issue 3 — EMA pullback fires without confirmed touch or recovery

**Observed:** Pullback signals firing when price was near but had not yet
touched the EMA21, or when price was below the EMA with no recovery candle.

**Root cause:** The proximity check (price within X% of EMA21) is too loose.
It fires on approach from above without contact, and does not require evidence
that the EMA acted as support.

**What we actually want:**
The EMA21 must have been touched or shallowly breached, AND price must show
recovery by closing above the EMA. Two valid cases:

```
Case A — Touch or breach + recovery on the SAME bar:
  Low of today <= EMA21   (candle touched or pierced the EMA)
  AND
  Close of today > EMA21  (price recovered above it by end of day)
  → Classic wick rejection at EMA support

Case B — Breach on PRIOR bar, recovery candle TODAY:
  Low of yesterday < EMA21  OR  Close of yesterday < EMA21
  (price went through or closed below EMA on prior bar)
  AND
  Close of today > EMA21
  (price has reclaimed the EMA today)
  → One-bar-delayed recovery, equally valid

PSEUDO:
same_bar_recovery  = (today.low <= EMA21) AND (today.close > EMA21)
prior_bar_breach   = (yesterday.low < EMA21) OR (yesterday.close < EMA21)
prior_bar_recovery = prior_bar_breach AND (today.close > EMA21)

ema_signal = same_bar_recovery OR prior_bar_recovery
→ if false: Strategy A does not fire
```

**What must NOT fire:**
- Price approaching EMA from above without touching it
- Price below EMA with no recovery (ongoing breakdown, not a pullback)
- Price below EMA for multiple bars without reclaiming it

**Settings impact:** The `EMA_PROXIMITY_PCT` parameter and any
`BOUNCE_CANDLE_REQUIRED` flag should be removed or ignored. The touch/recovery
logic above fully replaces them. Close above EMA is now always required
(implicit in both Case A and Case B).

---

## Issue 4 — Stale breakout signals + inflated stop distances (related)

**Observed:** A5G.IR fired as a breakout signal 2-3 days after the actual
breakout occurred. By signal date, price had already moved up significantly.

**Root cause:** The N-day high condition remains technically true for several
days after the initial breakout — price is still above the N-day high. The
scanner has no mechanism to distinguish a fresh breakout from one that already
fired on a prior day.

**Why this also causes hard-cap inflation:**
The stop is placed below the swing low, which is a fixed structural level.
When the breakout is stale and price has risen since the breakout day, the
distance from current entry to the fixed swing low inflates as a percentage
of the (now higher) entry price. This is why a disproportionate number of
stale breakout signals hit the 8% stop hard cap — the geometry has
deteriorated since the actual breakout day.

Fixing stale detection resolves most hard-cap inflation organically.

**Fix — freshness check:**
The breakout is only "new today" if yesterday's close did NOT already satisfy
the same breakout condition. One additional check before the main signal logic:

```
PSEUDO:
n_day_high_as_of_yesterday = highest high of the N bars ending TWO days ago
                             (exclude yesterday and today from the window)

if yesterday.close > n_day_high_as_of_yesterday:
    → breakout already fired yesterday → skip, do not signal
```

**Volume confirmation reminder:**
Volume confirmation (today's volume > X × 20-day average volume) should also
be applied as a condition for Strategy B. If this is not yet wired in despite
being present in settings, it should be connected at the same time as the
freshness fix. A breakout without volume confirmation has significantly lower
follow-through probability.

---

## Issue 5 — Hard cap hits not visible in alert output

**Observed:** 20 of 51 signals hit the stop hard cap (8% from entry). These
were silently accepted with no indication in the alert output.

**Fix:** When the calculated stop distance equals or exceeds the hard cap,
add a visible warning flag to the alert output so the trader can review the
setup geometry before acting. During the alert phase (manual order placement),
this flag should prompt extra scrutiny rather than automatic rejection.

```
PSEUDO:
stop_distance_pct = (entry - stop) / entry * 100
if stop_distance_pct >= STOP_HARD_CAP_PCT:
    signal.stop_capped = True

In alert output:
if signal.stop_capped:
    show warning: "⚠️ Stop at hard cap — wide stop, verify setup geometry"
```

---

## Summary Table

| # | Issue | Affected component | Priority |
|---|---|---|---|
| 1 | Downtrending stocks pass trend filter | Trend filter | 🔴 Critical |
| 2 | MACD fires on positive or noisy histogram | MACD pullback confirmation | 🔴 Critical |
| 3 | EMA pullback fires without touch/recovery | Strategy A signal logic | 🔴 Critical |
| 4 | Stale breakouts fire days after the event | Strategy B signal logic | 🟠 High |
| 4b | Volume confirmation not wired in | Strategy B signal logic | 🟠 High |
| 5 | Hard cap hits not visible in alerts | Alert output | 🟡 Medium |

---

## Expected Outcome After Fixes

- Issues 1 + 2 together would have blocked IVG.MI at two independent gates
- Issue 3 makes the pullback signal definition precise and structurally meaningful
- Issue 4 eliminates late entries with deteriorated risk geometry
- Issue 4b adds the volume confirmation that makes breakouts meaningful
- The combination of 1-4 should significantly reduce total signal count
  and improve the quality of the remaining signals
- Issue 5 ensures any residual wide-stop signals are visible for human review
