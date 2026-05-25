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
    Export Windows root + intermediate CA certs to a temp PEM file so that
    curl_cffi's bundled libcurl can verify TLS chains that include corporate or
    AV-injected root certificates not present in certifi's bundle.
    Returns the path to the temp file.
    """
    chunks: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            for cert_der, _encoding, _trust in ssl.enum_certificates(store):
                b64 = base64.b64encode(cert_der).decode("ascii")
                # PEM requires 64-character line wrapping
                wrapped = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
                chunks.append(f"-----BEGIN CERTIFICATE-----\n{wrapped}\n-----END CERTIFICATE-----")
        except Exception:
            pass

    import certifi
    chunks.append(Path(certifi.where()).read_text(encoding="ascii"))

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".pem", delete=False, encoding="ascii"
    )
    tmp.write("\n\n".join(chunks))
    tmp.close()
    # libcurl on Windows needs forward slashes in the CA bundle path
    return tmp.name.replace("\\", "/")


_CA_BUNDLE = _build_ca_bundle()

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class BaseProvider(ABC):
    @abstractmethod
    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """Return a DataFrame with lowercase OHLCV columns and a DatetimeIndex named 'date'."""


class YFinanceProvider(BaseProvider):
    def __init__(self) -> None:
        # Browser impersonation avoids Yahoo rate-limits; custom CA bundle trusts
        # corporate/AV-injected root certs that aren't in certifi's default bundle.
        self._session = cffi_requests.Session(
            impersonate="chrome", verify=_CA_BUNDLE
        )

    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        t = yf.Ticker(ticker, session=self._session)
        df = t.history(
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
        )
        if df.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df.index.name = "date"
        return df[OHLCV_COLUMNS].copy()


class EODHDProvider(BaseProvider):
    """Drop-in replacement for YFinanceProvider using the EODHD API (DF-11)."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        raise NotImplementedError(
            "EODHD provider is not yet implemented. "
            "Set data_fetcher.provider to 'yfinance' in config.yaml."
        )


def get_provider(config: dict) -> BaseProvider:
    df_cfg = config.get("data_fetcher", {})
    name = df_cfg.get("provider", "yfinance")
    if name == "yfinance":
        return YFinanceProvider()
    if name == "eodhd":
        api_key = df_cfg.get("eodhd_api_key", "")
        return EODHDProvider(api_key)
    raise ValueError(f"Unknown data provider: {name!r}")
