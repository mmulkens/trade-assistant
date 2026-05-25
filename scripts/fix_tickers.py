#!/usr/bin/env python3
"""
Transform ticker files:
  - Replace Dutch country names with 2-letter ISO codes
  - Append Yahoo Finance exchange suffix to each ticker
  - Remove non-equity rows (currencies, cash balances, futures)

Run from the project root:
    python scripts/fix_tickers.py
"""
import csv
from pathlib import Path

# Dutch country name → ISO 2-letter code (None = remove row)
COUNTRY_ISO: dict[str, str | None] = {
    "Italië": "IT",
    "Ierland": "IE",
    "Nederland": "NL",
    "België": "BE",
    "Spanje": "ES",
    "Frankrijk": "FR",
    "Duitsland": "DE",
    "Oostenrijk": "AT",
    "Finland": "FI",
    "Portugal": "PT",
    "Verenigd Koninkrijk": "GB",
    "Zweden": "SE",
    "Europese Unie": None,  # currencies / cash / futures — skip
}

# ISO code → Yahoo Finance exchange suffix
ISO_SUFFIX: dict[str, str] = {
    "IT": ".MI",
    "IE": ".IR",
    "NL": ".AS",
    "BE": ".BR",
    "ES": ".MC",
    "FR": ".PA",
    "DE": ".DE",
    "AT": ".VI",
    "FI": ".HE",
    "PT": ".LS",
    "GB": ".L",
    "SE": ".ST",
}

# Tickers where the base symbol used in the source file differs from Yahoo Finance
TICKER_OVERRIDES: dict[str, str] = {
    "STMMI": "STM",     # STMicroelectronics — Yahoo uses STM, not STMMI
    "NDA FI": "NDA-FI", # Nordea Bank Finland — space replaced with hyphen
    "SHELL": "SHEL",    # Shell plc — Yahoo uses SHEL for the London listing
}

# Full ticker + country overrides where the source file has the wrong exchange/country
# Format: source_ticker → (yf_ticker_with_suffix, correct_iso)
FULL_OVERRIDES: dict[str, tuple[str, str]] = {
    "MT": ("MT.AS", "NL"),     # ArcelorMittal: primary listing is Amsterdam, not Paris
    "TUI1": ("TUI1.DE", "DE"), # TUI: primary listing is Frankfurt, not London
}


def transform(src: Path) -> None:
    if not src.exists():
        print(f"Skipping {src} — file not found")
        return

    rows: list[dict] = []
    skipped: list[str] = []

    with src.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            country_nl = row["country"].strip()
            iso = COUNTRY_ISO.get(country_nl)

            if iso is None:
                skipped.append(row["ticker"].strip())
                continue

            src_ticker = row["ticker"].strip()
            if src_ticker in FULL_OVERRIDES:
                yf_ticker, iso = FULL_OVERRIDES[src_ticker]
            else:
                base = TICKER_OVERRIDES.get(src_ticker, src_ticker)
                suffix = ISO_SUFFIX[iso]
                yf_ticker = base + suffix
            rows.append({
                "ticker": yf_ticker,
                "name": row["name"].strip(),
                "country": iso,
            })

    with src.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["ticker", "name", "country"], delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)

    print(f"{src}: {len(rows)} tickers written, {len(skipped)} non-equity rows removed")
    if skipped:
        print(f"  Removed: {', '.join(skipped)}")


if __name__ == "__main__":
    base = Path(__file__).parent.parent / "data_fetcher" / "tickers"
    transform(base / "eurostoxx600.txt")
    transform(base / "eu_custom.txt")
