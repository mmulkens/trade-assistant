from abc import ABC, abstractmethod
from datetime import date

import pandas as pd
import yfinance as yf

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class BaseProvider(ABC):
    @abstractmethod
    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """Return a DataFrame with lowercase OHLCV columns and a DatetimeIndex named 'date'."""


class YFinanceProvider(BaseProvider):
    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        t = yf.Ticker(ticker)
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
