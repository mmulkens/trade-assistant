"""
providers.py — Data source adapters for the Data Fetcher.

Responsibilities:
  - Define a common BaseProvider interface (fetch OHLCV for a ticker + date range).
  - Implement YFinanceProvider backed by the yfinance library.
  - Stub EODHDProvider for future migration (DF-11).
  - Handle the Windows SSL certificate problem that affects curl_cffi.

SSL background
--------------
yfinance switched its HTTP backend from `requests` to `curl_cffi` in 2024.
curl_cffi bundles its own libcurl with its own TLS stack — it does NOT
automatically use the Windows certificate store or Python's ssl module.
On machines where HTTPS traffic is intercepted and re-signed by antivirus or
corporate proxy software, the intercepting root CA exists in the Windows store
but NOT in certifi's static bundle.  curl_cffi therefore fails to verify the
chain and raises curl error 60 (SSL_CACERT).

Fix: at module load time we call _build_ca_bundle(), which:
  1. Enumerates every root and intermediate certificate from the Windows
     "ROOT" and "CA" stores via ssl.enum_certificates().
  2. Converts each DER-encoded cert to PEM (base64, 64-char line wrap).
  3. Appends certifi's built-in bundle for completeness.
  4. Writes everything to a single temp PEM file.
  5. Returns the path with forward slashes (libcurl on Windows requires this).
The resulting path is passed as verify= to every curl_cffi Session.

Browser impersonation
---------------------
Yahoo Finance rate-limits plain HTTP clients aggressively.  curl_cffi's
impersonate="chrome" option makes libcurl mimic a real Chrome TLS fingerprint
(JA3 hash, ALPN, cipher order), which bypasses this rate-limiting.
"""

import base64
import ssl
import tempfile
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf
from curl_cffi import requests as cffi_requests


def _build_ca_bundle() -> str:
    """
    Build a combined CA bundle PEM file from the Windows certificate store
    plus certifi's default bundle and return the file path.

    Called once at module import.  The temp file persists for the lifetime of
    the process; the OS cleans it up on reboot.
    """
    chunks: list[str] = []

    # Pull every trusted root and intermediate cert from the Windows store.
    # ssl.enum_certificates() yields (cert_der_bytes, encoding, trust_set) tuples.
    # We ignore encoding/trust and export all of them — libcurl does its own
    # chain validation; we just need the anchors to be present.
    for store in ("ROOT", "CA"):
        try:
            for cert_der, _encoding, _trust in ssl.enum_certificates(store):
                b64 = base64.b64encode(cert_der).decode("ascii")
                # PEM spec: base64 body must be wrapped at 64 characters per line
                wrapped = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
                chunks.append(
                    f"-----BEGIN CERTIFICATE-----\n{wrapped}\n-----END CERTIFICATE-----"
                )
        except Exception:
            # ssl.enum_certificates is Windows-only; on other platforms this is a no-op
            pass

    # Always include certifi's bundle so well-known public CAs are covered
    # even if the Windows store is missing or unavailable.
    import certifi
    chunks.append(Path(certifi.where()).read_text(encoding="ascii"))

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".pem", delete=False, encoding="ascii"
    )
    tmp.write("\n\n".join(chunks))
    tmp.close()

    # libcurl on Windows requires forward slashes in file paths
    return tmp.name.replace("\\", "/")


# Built once at import time; reused by every YFinanceProvider instance.
_CA_BUNDLE = _build_ca_bundle()

# The only columns we store in the Parquet cache.
# Volume is kept as-is (raw share count); all prices are split- and
# dividend-adjusted because yfinance fetches with auto_adjust=True.
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class BaseProvider(ABC):
    """
    Common interface for all data sources.

    Any provider must return a DataFrame with:
      - Index: DatetimeIndex named "date", timezone-naive, one row per trading day
      - Columns: open, high, low, close (float, EUR-adjusted), volume (int/float)
    """

    @abstractmethod
    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """
        Fetch daily OHLCV bars for `ticker` from `start` (inclusive) to `end` (exclusive).
        Returns an empty DataFrame with the correct columns if no data is available.
        """


class YFinanceProvider(BaseProvider):
    """
    Fetches historical OHLCV data from Yahoo Finance via the yfinance library.

    One shared curl_cffi Session is created per provider instance.  The session
    is reused across all fetch() calls so Yahoo's cookie/crumb authentication
    only happens once per run, not once per ticker.
    """

    def __init__(self) -> None:
        # impersonate="chrome" — mimics Chrome's TLS fingerprint to avoid Yahoo rate-limits.
        # verify=_CA_BUNDLE   — uses the Windows + certifi combined cert bundle (see module docstring).
        self._session = cffi_requests.Session(
            impersonate="chrome", verify=_CA_BUNDLE
        )

    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """
        Download daily bars from Yahoo Finance.

        auto_adjust=True: prices are adjusted for splits and dividends, so the
        time series is directly comparable across the full history window.
        The 'end' date is exclusive in the Yahoo API, which matches our convention
        of passing date.today() + 1 day to ensure today's bar is included.
        """
        t = yf.Ticker(ticker, session=self._session)
        df = t.history(
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
        )

        if df.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        # yfinance returns title-case columns (Open, High, …) and a tz-aware index;
        # normalise both to match the cache contract.
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df.index.name = "date"

        # Drop any extra columns yfinance may add (Dividends, Stock Splits, etc.)
        return df[OHLCV_COLUMNS].copy()


class EODHDProvider(BaseProvider):
    """
    Drop-in replacement for YFinanceProvider using the EODHD API (planned as DF-11).

    EODHD offers broader EU coverage and more reliable data for smaller-cap names.
    To switch: set data_fetcher.provider = 'eodhd' and add eodhd_api_key in config.yaml.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        raise NotImplementedError(
            "EODHD provider is not yet implemented. "
            "Set data_fetcher.provider to 'yfinance' in config.yaml."
        )


def get_provider(config: dict) -> BaseProvider:
    """
    Factory: instantiate the correct provider based on config.yaml.

    config.data_fetcher.provider: 'yfinance' (default) | 'eodhd'
    """
    df_cfg = config.get("data_fetcher", {})
    name = df_cfg.get("provider", "yfinance")
    if name == "yfinance":
        return YFinanceProvider()
    if name == "eodhd":
        api_key = df_cfg.get("eodhd_api_key", "")
        return EODHDProvider(api_key)
    raise ValueError(f"Unknown data provider: {name!r}")
