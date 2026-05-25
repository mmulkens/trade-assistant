# Signal Engine — Open Design Questions

Design decisions made during the initial build session (2026-05-25), grouped by topic.
Settled decisions are noted for context; open questions are the ones worth revisiting.

---

## Strategy A — EMA Pullback

### Uptrend definition
**Settled:** `close > EMA50`, `EMA21 > EMA50`, `EMA50 > EMA100 > EMA200`

**Open:** Is the 4-condition EMA stack strict enough, or should higher highs / higher lows be required as a structural confirmation on top of the EMA alignment? (Was considered and rejected as harder to implement robustly, but not permanently closed.)

---

### Pullback tolerance
**Settled:** Close within 2% of EMA21, **OR** intraday wick touches EMA21 with close above it.

**Open:** Should the 2% tolerance be adaptive — tighter during strong trends (low ATR environment) and looser during volatile periods — or is a flat 2% sufficient for all market conditions?

---

### MACD histogram "turning up"
**Settled:** Histogram is negative AND `h[-1] > h[-2] > h[-3]` (two consecutive rising bars, still below zero).

**Open:** Should there be a minimum magnitude requirement on the turn (e.g. the rise must be at least X% of the recent average histogram range), or is any two-bar improvement sufficient regardless of size?

---

## Strategy B — Breakout

### Volume confirmation on the breakout bar
**Settled:** No volume filter currently — breakout is purely price-based (close above 50-day high).

**Open:** Should the breakout bar require elevated volume (e.g. volume > 1.5× 20-day average) as a confirmation? Classic breakout methodology (O'Neil / IBD) treats this as near-mandatory. EOD data makes it easy to add.

---

### MACD line "rising" — how many bars?
**Settled:** Single bar: `macd_line[-1] > macd_line[-2]`

**Open:** Should this require two consecutive rising bars (matching the histogram condition in Strategy A), or is one bar enough given the breakout itself is already a strong independent signal?

---

## Stop Calculation

### Structural stop placement
**Settled:** Lowest low of the last `swing_low_period` bars (default: 10).

**Open:** Should the stop be placed at the raw minimum low, or at the *second-lowest* low to avoid parking the stop at an obvious round-number level where stop-hunters typically cluster orders?

---

### ATR floor multiplier
**Settled:** `min(structural_stop, entry − 1.5 × ATR14)` — take the wider of the two stops.

**Open:** Is 1.5× the right multiplier across all EU equity names? Some practitioners use 2× for high-volatility small-caps and 1× for large-cap defensives. Should this be a per-liquidity-class parameter (`thin` gets a larger multiplier than `liquid`)?

---

### Hard-cap flag in signal payload
**Settled:** Hard cap silently clamps the stop at 8% from entry with no special flag.

**Open:** Should signals that hit the hard cap carry a `stop_capped: true` annotation in the payload, so the Risk Layer can optionally reject them outright or apply a further position-size reduction?

---

## Signal Output & Infrastructure

### Daily JSON snapshot alongside SQLite
**Settled:** Output is SQLite only (`./data/signals.db`).

**Open:** Should the engine also write a daily JSON snapshot file (e.g. `./signals/YYYY-MM-DD.json`) for quick human inspection without needing a SQL client?

---

### Earnings flag — interim data source
**Settled:** `earnings_flag` is always `None` until IBKR `reqFundamentalData` is integrated.

**Open:** Is there a free or low-cost earnings calendar API worth wiring up as a temporary replacement before IBKR is live? (e.g. a public financial data provider with EU earnings dates)

---

## Market Regime

### Regime based on EMA slope vs. position
**Settled:** Bear = `^STOXX50E close < EMA200` (above/below only, no slope component).

**Open:** Should the regime also factor in the slope or rate of change of the EMA, to distinguish a "just crossed below" state (potentially a false signal) from a "deeply below and declining" state (confirmed bear)?

---

### Bear-regime exception for high-RS stocks
**Settled:** Bear regime is a hard gate — no signals fire for any ticker.

**Open:** Should stocks with significantly high RS (strongly outperforming the index) still be allowed to signal even in a bear regime? These are the stocks that tend to lead the next bull market. If so, what RS threshold would qualify for the exception?

---

## Liquidity

### Exchange-specific turnover thresholds
**Settled:** Single flat threshold: avg 20-day turnover < €1M/day = `thin`.

**Open:** Should the threshold be exchange-specific? A €1M/day name on Euronext Amsterdam trades in a very different order-book environment from one on Euronext Lisbon or the Vienna Stock Exchange. A per-exchange threshold table might better reflect actual execution risk.
