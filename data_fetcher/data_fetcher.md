# Data Fetcher

Downloads and maintains a local Parquet cache of daily OHLCV data for the
full Eurostoxx 600 universe plus a custom list of additional EU names.
It is the first stage in the Trade Assistant pipeline and provides the
historical price data consumed by every downstream component.

---

## Architecture

```
config.yaml
    │
    ▼
__main__.py          ← CLI, argument parsing, watchlist loading
    │
    ▼
DataFetcher          ← Orchestrates batching, threading, caching
  ├── BaseProvider   ← Interface: fetch(ticker, start, end) → DataFrame
  │     └── YFinanceProvider  ← Yahoo Finance via yfinance + curl_cffi
  │         (EODHDProvider)   ← Stub for future EODHD migration
  │
  └── cache module   ← Parquet read / merge / validate / write
        ./cache/<TICKER>.parquet
```

---

## Files

| File | Purpose |
|---|---|
| `__main__.py` | CLI entry point; resolves the ticker list and wires up dependencies |
| `fetcher.py` | `DataFetcher` class; batch + thread orchestration, retry logic |
| `providers.py` | `YFinanceProvider` + SSL fix + `EODHDProvider` stub |
| `cache.py` | Parquet load / save / merge / validate helpers |
| `__init__.py` | Re-exports `DataFetcher`, `FetchSummary` for use by other modules |
| `tickers/eurostoxx600.txt` | 298 STOXX 600 constituents (TSV: ticker, name, ISO country) |
| `tickers/eu_custom.txt` | Custom EU names (same format; currently empty) |

---

## Ticker files

Both files are tab-separated with a header row:

```
ticker	name	country
ASML.AS	ASML HOLDING NV	NL
SAP.DE	SAP	DE
```

- **ticker** — Yahoo Finance symbol including exchange suffix (`.AS`, `.DE`, `.PA`, etc.)
- **name** — company name (informational only, not used at runtime)
- **country** — ISO 3166-1 alpha-2 code

To regenerate the files from a fresh source (e.g. after an index rebalance),
update the raw data, run `python scripts/fix_tickers.py`, then re-run the
full-refresh fetch.

Exchange suffix mapping used in the files:

| Country | Exchange | Suffix |
|---|---|---|
| NL | Euronext Amsterdam | `.AS` |
| BE | Euronext Brussels | `.BR` |
| FR | Euronext Paris | `.PA` |
| DE | XETRA (Frankfurt) | `.DE` |
| IT | Borsa Italiana (Milan) | `.MI` |
| ES | BME (Madrid) | `.MC` |
| AT | Wiener Börse (Vienna) | `.VI` |
| FI | Nasdaq Helsinki | `.HE` |
| PT | Euronext Lisbon | `.LS` |
| GB | London Stock Exchange | `.L` |
| IE | Euronext Dublin | `.IR` |
| SE | Nasdaq Stockholm | `.ST` |

---

## Running

```bash
# Delta update — only fetches new bars since last cache entry
python -m data_fetcher

# Full re-download (use after changing history_days or to repair cache)
python -m data_fetcher --full-refresh

# Fetch specific tickers only
python -m data_fetcher ASML.AS SAP.DE MC.PA

# Use a plain-text ticker file (one symbol per line, # for comments)
python -m data_fetcher --ticker-file my_tickers.txt

# Alternative config path
python -m data_fetcher --config /path/to/config.yaml
```

Exit code is `0` when all tickers succeed, `1` when any ticker fails.

---

## Configuration (`config.yaml`)

```yaml
data_fetcher:
  provider: 'yfinance'          # 'yfinance' | 'eodhd'
  history_days: 300             # calendar days to fetch on first run
  workers: 8                    # concurrent threads per batch
  batch_size: 16                # tickers per batch
  batch_pause_seconds: 2        # sleep between batches (rate-limit courtesy)
  cache_dir: './cache'          # Parquet files written here
  max_retries: 3                # attempts per ticker before marking failed
  retry_backoff_seconds: 1.0    # sleep = attempt × this value

signal_engine:
  benchmark: '^STOXX50E'        # always fetched first, used for RS line

watchlist:
  eurostoxx600_file: './data_fetcher/tickers/eurostoxx600.txt'
  custom_file: './data_fetcher/tickers/eu_custom.txt'
  custom: []                    # inline list for quick additions
```

---

## Fetch logic per ticker

```
load cache
    │
    ├─ cache hit AND not full_refresh
    │       │
    │       ├─ last_date >= today  →  SKIP  (already current)
    │       └─ last_date < today   →  DELTA fetch from last_date+1
    │
    └─ cache miss OR full_refresh  →  FULL fetch of history_days
            │
            ▼
    provider.fetch() with retry
            │
            ▼
    merge / replace cache
            │
            ▼
    validate → log warnings
            │
            ▼
    save Parquet
```

---

## SSL fix (Windows)

yfinance switched its HTTP backend from `requests` to `curl_cffi` in 2024.
On Windows, antivirus and corporate proxy tools inject their own root CA into
the Windows certificate store to perform HTTPS inspection — but curl_cffi's
bundled libcurl does not read the Windows store and therefore fails to verify
the TLS chain (curl error 60).

`providers.py` works around this at module import time:

1. `ssl.enum_certificates("ROOT")` and `ssl.enum_certificates("CA")` read
   every certificate from the Windows trusted root and intermediate stores.
2. Each DER cert is converted to PEM (base64, 64-char line wrap).
3. The certifi bundle is appended for completeness.
4. Everything is written to a single temp `.pem` file.
5. The path (with forward slashes, as required by libcurl on Windows) is
   passed as `verify=` to `curl_cffi.Session(impersonate="chrome", ...)`.

`impersonate="chrome"` is a separate but equally important setting: it makes
curl_cffi mimic a real Chrome TLS fingerprint (JA3 hash, cipher suite order,
ALPN), which bypasses Yahoo Finance's bot-detection rate-limiter.

---

## Cache layout

```
./cache/
    ASML.AS.parquet
    SAP.DE.parquet
    _STOXX50E.parquet      ← ^ replaced with _ in filename
    ...
```

Each Parquet file contains a DatetimeIndex named `date` (timezone-naive) and
columns `open`, `high`, `low`, `close`, `volume`.  All prices are
split- and dividend-adjusted (`auto_adjust=True` in yfinance).

---

## Known issues

| Ticker | Issue | Status |
|---|---|---|
| `STM.PA` | Yahoo Finance returning empty for the Euronext Paris listing | Will self-heal on next delta run |
