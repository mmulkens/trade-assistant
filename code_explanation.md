# Trade Assistant — Code Explanation

This document explains every module in the Trade Assistant codebase step by step. It is written for someone new to Python. Part 1 covers the Python language features and library patterns you will encounter throughout the code. Part 2 walks through every module in pipeline order.

---

## Part 1 — Python Foundations

### 1.1 Modules and Imports

Python code is organised into **modules** (individual `.py` files) and **packages** (folders containing an `__init__.py` file). You bring code from one module into another using `import`.

```python
import json                         # import the entire standard-library json module
from pathlib import Path            # import just the Path class from pathlib
from datetime import date, datetime # import two things from one module
```

**Relative imports** use a dot to mean "from the same package". You see this inside packages like `data_fetcher/`:

```python
from . import cache as cache_store  # import cache.py from this same folder
from .providers import BaseProvider  # import the BaseProvider class from providers.py
```

**`TYPE_CHECKING` imports** are a special pattern to avoid circular imports at runtime:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from signal_engine.engine import Signal  # only imported when a type checker runs
```

Because `TYPE_CHECKING` is `False` at runtime, the import is never actually executed. The annotation is still useful for documentation and tools like pylance. You will see this used in `layer.py` and `executor.py`.

---

### 1.2 Classes

A **class** is a blueprint for creating objects. An object bundles data (attributes) and behaviour (methods) together.

```python
class DataFetcher:
    def __init__(self, config: dict, provider: BaseProvider, logger: Logger) -> None:
        self._provider = provider   # store for later use
        self._logger = logger
        self._batch_size = config["data_fetcher"]["batch_size"]
```

- `__init__` is the **constructor** — it runs when you create an instance: `fetcher = DataFetcher(config, provider, logger)`.
- `self` is the instance itself. Every method receives it as the first argument. `self._provider` means "the `_provider` attribute of this specific object".
- The underscore prefix `_provider` is a convention meaning "private — intended for internal use only". Python does not enforce this; it is purely a communication to other developers.
- `-> None` is a **return type annotation** (see section 1.4).

To use the class:

```python
fetcher = DataFetcher(config, provider, logger)   # creates an instance
summary = fetcher.run(tickers)                    # calls the run() method on it
```

---

### 1.3 Dataclasses

A `@dataclass` is a shortcut for writing a class whose only purpose is to hold data. Instead of writing `__init__` manually, Python generates it for you.

```python
from dataclasses import dataclass, field

@dataclass
class TickerResult:
    ticker: str
    status: str
    rows_added: int
    error: Optional[str] = None   # default value = None if not provided
```

This is equivalent to:

```python
class TickerResult:
    def __init__(self, ticker: str, status: str, rows_added: int, error=None):
        self.ticker = ticker
        self.status = status
        self.rows_added = rows_added
        self.error = error
```

You create an instance like this:

```python
result = TickerResult(ticker="AAPL", status="delta", rows_added=3)
```

`field(default_factory=list)` is used when the default value should be a new list (or dict) for every instance. Writing `issues: list[str] = []` would share the same list across all instances — a classic Python bug. `field(default_factory=list)` creates a fresh empty list each time:

```python
@dataclass
class ValidationResult:
    valid: bool
    issues: list[str] = field(default_factory=list)
```

---

### 1.4 Type Hints

Type hints tell you (and your IDE) what type each variable, parameter, and return value is expected to be. They are not enforced at runtime — they are documentation and tooling aids.

```python
def size_position(
    entry: float,          # entry is a float (decimal number)
    stop: float,
    portfolio_value: float,
    max_position_risk_pct: float,
    liquidity_class: str,  # str = text string
    thin_size_multiplier: float,
) -> SizingResult:         # returns a SizingResult object
```

Common types used in this codebase:

| Annotation | Meaning |
|---|---|
| `int` | Whole number: `5`, `-3`, `200` |
| `float` | Decimal number: `1.5`, `720.35` |
| `str` | Text: `"AAPL"`, `"open"` |
| `bool` | `True` or `False` |
| `dict` | Key-value mapping: `{"key": "value"}` |
| `list[str]` | A list whose items are strings |
| `tuple[bool, str]` | A fixed-length pair: `(True, "")` or `(False, "reason")` |
| `Optional[str]` | Either a string or `None` |
| `pd.DataFrame` | A pandas table (see section 1.9) |
| `pd.Series` | A pandas column (see section 1.9) |
| `Logger` | Python's built-in logging object |

`Optional[X]` is equivalent to `X | None`. You use it when a value might legitimately not exist yet (e.g. `earnings_flag` is `None` until IBKR provides data).

---

### 1.5 `__init__.py` and `__main__.py`

**`__init__.py`** makes a folder into a Python package. It can be empty, or it can expose things from submodules. In this codebase, all `__init__.py` files are empty — they just mark the folder as a package so that `from signal_engine.engine import Signal` works.

**`__main__.py`** is the file that runs when you execute a package with `python -m package_name`. For example, `python -m signal_engine` runs `signal_engine/__main__.py`. It defines a `main()` function that parses command-line arguments and calls into the package's public classes.

The pattern at the bottom of every `__main__.py`:

```python
if __name__ == "__main__":
    main()
```

`__name__` is a special variable set by Python. When you run a file directly, Python sets `__name__` to `"__main__"`. When the file is imported by another module, `__name__` is set to the module's name. This guard prevents `main()` from being called accidentally when the file is imported.

---

### 1.6 f-strings

f-strings format values directly into text. The `f` prefix enables it; curly braces `{}` contain expressions:

```python
ticker = "AAPL"
price = 1234.5
print(f"Ticker: {ticker}, price: €{price:.2f}")
# Output: Ticker: AAPL, price: €1234.50
```

Format specifiers after `:`:
- `.2f` — two decimal places
- `,.2f` — thousands separator AND two decimal places: `1,234.50`
- `>8.2f` — right-align in a field 8 characters wide
- `<14` — left-align in a field 14 characters wide
- `:+,.2f` — always show sign: `+1,234.50` or `-56.78`

Multi-line f-strings with parentheses (used in notify.py):

```python
text = (
    f"🟢 SIM FILL · {sig.ticker}\n"
    f"Entry: €{sig.entry_price:,.2f} · Stop: €{sig.stop_price:,.2f}\n"
    f"Shares: {decision.shares}"
)
```

The `\n` is a newline character. Adjacent string literals are automatically concatenated.

---

### 1.7 List Comprehensions

A compact way to build a list by transforming or filtering another sequence:

```python
# Standard for-loop:
result = []
for r in self.results:
    if r.status == "error":
        result.append(r.ticker)

# Equivalent list comprehension:
result = [r.ticker for r in self.results if r.status == "error"]
```

In this codebase you see them frequently:

```python
# Build all OHLCV rows for SQLite insertion
rows = [
    (s.signal_timestamp.isoformat(), s.ticker, s.entry_price, ...)
    for s in signals
]

# Lowercase all column names
df.columns = [c.lower() for c in df.columns]
```

**Dict comprehensions** work the same way but produce a dictionary:

```python
future_to_ticker = {
    executor.submit(self._fetch_one, ticker, full_refresh): ticker
    for ticker in batch
}
```

This creates a dict where each key is a `Future` object and each value is the ticker string. Used in `fetcher.py` to track which future belongs to which ticker.

---

### 1.8 `with` Statements (Context Managers)

A `with` block guarantees that a resource is properly cleaned up, even if an error occurs. The resource provides an `__enter__` method (runs at the start) and `__exit__` method (runs at the end, including on errors).

```python
with open("config.yaml", encoding="utf-8") as fh:
    config = yaml.safe_load(fh)
# File is automatically closed here, even if yaml.safe_load() raised an exception
```

For SQLite:

```python
with sqlite3.connect(db_path) as conn:
    conn.execute("UPDATE ...")
# Transaction is automatically committed (or rolled back on error)
# Connection is closed
```

For the thread pool executor:

```python
with ThreadPoolExecutor(max_workers=self._workers) as executor:
    ...
# All threads are waited for and the pool is shut down
```

---

### 1.9 Exception Handling

`try/except` catches errors so the program can react to them rather than crashing:

```python
try:
    result = some_function()
except Exception as exc:
    logger.warning({"event": "error", "error": str(exc)})
    # continue running...
```

You can catch specific exception types:

```python
except urllib.error.URLError as exc:
    logger.warning({"event": "telegram_send_error", "error": str(exc)})
except Exception as exc:
    # Catch-all: anything else that wasn't caught above
    logger.warning({"event": "telegram_send_error", "error": str(exc)})
```

The `str(exc)` converts the exception to a human-readable message. The `raise` keyword re-raises an exception (not used here, but common elsewhere in Python).

---

### 1.10 `@property`, `@staticmethod`, and `@abstractmethod`

**`@property`** lets you define a method that is accessed like an attribute (no parentheses):

```python
@property
def attempted(self) -> int:
    return len(self.results)

# Called as: summary.attempted   (not summary.attempted())
```

**`@staticmethod`** marks a method that does not need `self` — it is just a function that logically belongs to the class:

```python
@staticmethod
def _parse_date(iso_str: str) -> datetime.date:
    dt = datetime.fromisoformat(iso_str)
    return dt.date()

# Called as: PositionManager._parse_date("2025-06-01T...")
# Or on an instance: self._parse_date("2025-06-01T...")
```

**`@abstractmethod`** (combined with inheriting from `ABC`) defines an interface — a method that subclasses MUST implement:

```python
from abc import ABC, abstractmethod

class BaseProvider(ABC):
    @abstractmethod
    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        ...  # no implementation here

class YFinanceProvider(BaseProvider):
    def fetch(self, ticker, start, end):
        ...  # must implement this
```

If you try to create an instance of `BaseProvider` directly (without implementing `fetch`), Python raises a `TypeError`. This enforces a contract: any `BaseProvider` is guaranteed to have a `fetch()` method.

---

### 1.11 Truthy/Falsy Values and `not`

Python treats certain values as `False` even without an explicit comparison:
- `None`, `0`, `0.0`, `""` (empty string), `[]` (empty list), `{}` (empty dict) → all falsy
- Everything else → truthy

```python
if not signals:         # True if signals is an empty list
    return

if existing is not None:  # explicit None check (preferred over `if existing:` for DataFrames)
    ...
```

The `or` operator returns the first truthy value:

```python
run_type = row["run_type"] or "eod"  # use row value if present, else default to "eod"
```

---

### 1.12 Unpacking and `**dict`

**Tuple unpacking** assigns multiple values at once:

```python
macd_line, signal_line, histogram = ind.macd(close, 12, 26, 9)

pos_id, entry_price, shares, entry_commission = row
```

**Dictionary unpacking** with `**` spreads a dict's key-value pairs into a function call or another dict:

```python
extra = {"pending_signals": 0}
self._logger.info({
    "event": "time_exit_hold",
    "ticker": ticker,
    **extra,         # expands to: "pending_signals": 0
})
```

---

## Part 2 — Library Reference

### 2.1 pandas

pandas is the core data-manipulation library. The two main objects are:

- `pd.DataFrame` — a 2D table with labeled columns and a row index. In this project, the row index is a `DatetimeIndex` (one row per trading day) and the columns are `open`, `high`, `low`, `close`, `volume`.
- `pd.Series` — a 1D column. `df["close"]` returns the close column as a Series.

**Selecting rows by position (`.iloc`):**

```python
df["close"].iloc[-1]    # last value (most recent day)
df["close"].iloc[-2]    # second-to-last value (yesterday)
df["close"].iloc[-10:]  # last 10 rows (a slice)
df["high"].iloc[-(N+1):-1]  # rows from position -(N+1) up to but not including -1
```

Negative indices count from the end. `-1` is the last element, `-2` is second to last, etc. Slices work the same as Python list slices: `[start:stop]` is inclusive of `start`, exclusive of `stop`.

**Exponential Moving Average (`.ewm`):**

```python
series.ewm(span=period, adjust=False).mean()
```

`ewm` stands for Exponentially Weighted Moving average. `span` controls the decay speed (larger span = slower decay = smoother). `adjust=False` uses the recursive formula that matches trading platforms. `.mean()` computes the actual average values, returning a new Series of the same length.

```python
series.ewm(alpha=1/period, adjust=False).mean()
```

`alpha` is an alternative way to specify decay (Wilder's ATR smoothing). `alpha = 1/14` for a 14-period ATR.

**Shifting (`.shift`):**

```python
close.shift(1)  # shifts all values one row down; first row becomes NaN
```

Used in ATR calculation to get "yesterday's close" aligned with "today's high and low".

**Row-wise operations:**

```python
pd.concat([series_a, series_b, series_c], axis=1).max(axis=1)
```

`pd.concat(..., axis=1)` joins multiple Series side-by-side into a DataFrame. `.max(axis=1)` then takes the maximum value across each row. Used in ATR to find the largest of three True Range components.

**Aligning indices (`.reindex`):**

```python
benchmark_close.reindex(close.index, method="ffill")
```

Makes the benchmark index match the stock's index, filling any gaps by carrying the last known value forward (`ffill` = forward-fill).

**Concatenating and deduplicating:**

```python
combined = pd.concat([existing, new])
combined = combined[~combined.index.duplicated(keep="last")].sort_index()
```

`~` inverts a boolean mask. `combined.index.duplicated(keep="last")` returns `True` for the *first* occurrence of any duplicate date (the one to drop). `~` flips it so we keep the last occurrence.

**Parquet files:**

```python
df.to_parquet(path)          # save DataFrame to disk
pd.read_parquet(path)        # load it back
```

Parquet is a binary columnar file format — much faster and smaller than CSV for numerical data.

**Timezone handling:**

```python
df.index = pd.to_datetime(df.index).tz_localize(None)
```

Parquet sometimes re-attaches a UTC timezone when loading. `.tz_localize(None)` strips it, giving a plain naive datetime index. Consistent timezone handling prevents subtle comparison errors.

---

### 2.2 SQLite and `sqlite3`

SQLite is a file-based relational database. No server is needed — the whole database is a single file. The `sqlite3` module is Python's built-in interface.

**Opening a connection:**

```python
with sqlite3.connect(db_path) as conn:
    conn.execute("UPDATE risk_positions SET status = 'closed' WHERE id = ?", (pos_id,))
```

The `?` placeholders prevent SQL injection. Values are passed as a tuple. The `with` block auto-commits on success, auto-rolls-back on exception.

**`sqlite3.Row` — dict-like row access:**

```python
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM signals").fetchall()
row["ticker"]   # access by column name instead of row[2]
```

**Fetching results:**

```python
conn.execute(...).fetchone()   # returns one row, or None
conn.execute(...).fetchall()   # returns a list of rows
```

**`executemany` for bulk inserts:**

```python
conn.executemany(INSERT_SIGNAL, rows)
```

`rows` is a list of tuples. One SQL statement is executed for each tuple. More efficient than a loop of individual `execute()` calls.

**`COALESCE` in SQL:**

```python
"SELECT COALESCE(SUM(risk_amount), 0.0) FROM risk_positions WHERE status = 'open'"
```

`COALESCE(expr, default)` returns the default if `expr` is NULL. When no open positions exist, `SUM()` returns NULL — `COALESCE` converts it to `0.0` so Python always gets a number.

---

### 2.3 PyYAML

`yaml.safe_load(file_handle)` parses a YAML file into a plain Python dict. "Safe" means it will not execute arbitrary Python code embedded in the YAML (unlike `yaml.load`).

```python
with open("config.yaml", encoding="utf-8") as fh:
    config = yaml.safe_load(fh)

# config is now a dict:
config["risk"]["max_position_risk_pct"]   # → 1.5
config["signal_engine"]["benchmark"]      # → "^GSPC"
```

Nested YAML keys become nested Python dicts. Lists in YAML become Python lists.

---

### 2.4 `pathlib.Path`

`Path` is an object-oriented interface for file system paths. It works correctly on both Windows and Linux (handles `\` vs `/` automatically).

```python
from pathlib import Path

path = Path("./cache") / "AAPL.parquet"   # builds: ./cache/AAPL.parquet
path.exists()                              # True if the file is on disk
path.parent.mkdir(parents=True, exist_ok=True)  # create directory tree
path.read_text(encoding="utf-8")           # read whole file as string
```

`parents=True` creates all missing parent directories. `exist_ok=True` does not raise an error if the directory already exists.

---

### 2.5 `concurrent.futures` — Thread Pools

`ThreadPoolExecutor` runs multiple function calls concurrently in separate threads. Used in `fetcher.py` to fetch multiple tickers in parallel, dramatically reducing total fetch time.

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

with ThreadPoolExecutor(max_workers=8) as executor:
    # Submit tasks — each returns a Future object immediately
    future_to_ticker = {
        executor.submit(self._fetch_one, ticker, full_refresh): ticker
        for ticker in batch
    }
    # Process results as they complete (not in submission order)
    for future in as_completed(future_to_ticker):
        ticker = future_to_ticker[future]
        try:
            result = future.result()    # blocks until this future is done
        except Exception as exc:
            ...                         # handle error for this ticker
```

`as_completed()` returns futures in the order they finish, not the order they were submitted. This means a fast ticker doesn't wait for a slow one.

---

### 2.6 `argparse` — Command-Line Argument Parsing

`argparse` parses `sys.argv` (the command-line arguments) and gives you a structured object.

```python
parser = argparse.ArgumentParser(description="Trade Assistant — Signal Engine")

# Optional flag (--dry-run stores True when present, else False)
parser.add_argument("--dry-run", action="store_true")

# Optional argument with a value (--config path/to/file)
parser.add_argument("--config", default="config.yaml")

# Positional arguments (zero or more tickers)
parser.add_argument("tickers", nargs="*", metavar="TICKER")

args = parser.parse_args()
# args.dry_run    → True or False
# args.config     → "config.yaml" or whatever was passed
# args.tickers    → ["AAPL", "MSFT"] or []
```

**Subcommands** (used in `risk_layer/__main__.py`):

```python
subparsers = parser.add_subparsers(dest="command")
subparsers.add_parser("status")
close_p = subparsers.add_parser("close")
close_p.add_argument("ticker")
close_p.add_argument("price", type=float)
```

`python -m risk_layer status` → `args.command == "status"`
`python -m risk_layer close AAPL 150.0 stop` → `args.command == "close"`, `args.ticker == "AAPL"`, `args.price == 150.0`

---

### 2.7 `logging` — Structured Logging

Python's built-in `logging` module provides a hierarchical, configurable logging system.

```python
logger = logging.getLogger("signal_engine")   # named logger
logger.setLevel(logging.DEBUG)                # capture DEBUG and above
logger.info({"event": "signal_fired", "ticker": "AAPL"})  # log a dict
```

Log levels in order of severity: `DEBUG` < `INFO` < `WARNING` < `ERROR` < `CRITICAL`.

A `Handler` directs log output to a destination (file, console, network). A `Formatter` controls the output format. In this project, the custom `_JsonFormatter` in `json_logger.py` serialises each log record as a single JSON object, written to a `.jsonl` file (one JSON object per line).

---

## Part 3 — Module Walkthroughs

The modules are presented in pipeline order: Data Fetcher → Signal Engine → Risk Layer → Sim Executor → Position Manager.

---

### 3.1 `utils/json_logger.py`

**Purpose:** A single shared function that every component uses to get a pre-configured logger.

```python
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
```

`_JsonFormatter` inherits from `logging.Formatter` (the leading underscore means it is private to this module). It overrides the `format()` method so that log records are serialised as JSON instead of the default plain-text format.

Inside `format()`:

```python
payload = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "level": record.levelname,
    "component": record.name,
}
if isinstance(record.msg, dict):
    payload.update(record.msg)
else:
    payload["msg"] = record.getMessage()
```

`isinstance(record.msg, dict)` checks whether the message is already a dict. Every component in this project calls `logger.info({"event": "...", "key": value})` — passing a dict directly. `payload.update(record.msg)` merges that dict into the JSON payload, so all structured fields appear at the top level of the JSON object rather than nested under a `"msg"` key.

```python
if record.exc_info:
    payload["exc"] = self.formatException(record.exc_info)
return json.dumps(payload)
```

If there is an attached exception (from `logger.exception()`), it is formatted and included. `json.dumps()` converts the dict to a JSON string.

```python
def get_logger(component: str, log_dir: str) -> logging.Logger:
    logger = logging.getLogger(component)
    if logger.handlers:
        return logger
```

`logging.getLogger(component)` returns the same logger every time you call it with the same name. The `if logger.handlers: return logger` guard prevents duplicate handlers being added if the logger is requested more than once in the same process.

```python
    log_path = Path(log_dir) / f"{component}_{date.today().isoformat()}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger.propagate = False
```

Two handlers are added: one writes to a dated file (e.g. `signal_engine_2025-06-03.jsonl`), one writes to the console. `logger.propagate = False` prevents the messages being passed up to the root logger (which would cause double-printing).

---

### 3.2 `data_fetcher/providers.py`

**Purpose:** Defines the interface for data providers and implements the Yahoo Finance adapter.

**`_build_ca_bundle()`** — runs at import time (the line `_CA_BUNDLE = _build_ca_bundle()` is at module level, outside any function, so it executes the moment the file is imported).

```python
for store in ("ROOT", "CA"):
    for cert_der, _encoding, _trust in ssl.enum_certificates(store):
```

`ssl.enum_certificates(store)` is a Windows-only function that reads from the OS certificate store. It yields 3-tuples; `_encoding` and `_trust` are ignored (the `_` prefix is a Python convention for "I don't need this value"). `cert_der` is the certificate in DER (binary) format.

```python
b64 = base64.b64encode(cert_der).decode("ascii")
wrapped = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
```

DER must be converted to PEM (text) format. PEM is just base64-encoded DER with a header, footer, and lines no longer than 64 characters. `range(0, len(b64), 64)` generates indices `0, 64, 128, 192, ...` — every 64th character. `b64[i:i+64]` extracts each 64-character chunk.

```python
tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False, encoding="ascii")
tmp.write("\n\n".join(chunks))
tmp.close()
return tmp.name.replace("\\", "/")
```

`NamedTemporaryFile` creates a file with a random name in the system temp directory. `delete=False` means the file persists after `.close()`. The path is returned with forward slashes because libcurl (inside curl_cffi) requires them even on Windows.

**`BaseProvider`** — the abstract base class:

```python
class BaseProvider(ABC):
    @abstractmethod
    def fetch(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        ...
```

Any class that inherits from `BaseProvider` must implement `fetch()`. This is a contract: any code that receives a `BaseProvider` knows it can call `.fetch()` on it, regardless of whether it is a `YFinanceProvider` or `EODHDProvider`.

**`YFinanceProvider.fetch()`**:

```python
t = yf.Ticker(ticker, session=self._session)
df = t.history(start=start.isoformat(), end=end.isoformat(), auto_adjust=True)
```

`yf.Ticker()` creates a yfinance Ticker object. Passing `session=self._session` makes it use our custom curl_cffi session (with the correct SSL cert bundle) instead of creating its own. `t.history()` downloads the OHLCV data. `auto_adjust=True` returns split- and dividend-adjusted prices.

```python
df.columns = [c.lower() for c in df.columns]
df.index = pd.to_datetime(df.index).tz_localize(None)
df.index.name = "date"
return df[OHLCV_COLUMNS].copy()
```

yfinance returns title-case columns (`Open`, `High`, etc.) and a timezone-aware index. We normalise both to the project standard. `.copy()` returns an independent copy — without it, the slice `df[OHLCV_COLUMNS]` would be a view into the original DataFrame and modifications to it could unexpectedly affect the original.

**`get_provider()`** — factory function:

```python
def get_provider(config: dict) -> BaseProvider:
    name = config.get("data_fetcher", {}).get("provider", "yfinance")
    if name == "yfinance":
        return YFinanceProvider()
    if name == "eodhd":
        api_key = config.get("data_fetcher", {}).get("eodhd_api_key", "")
        return EODHDProvider(api_key)
    raise ValueError(f"Unknown data provider: {name!r}")
```

The `{name!r}` in the f-string uses the `repr()` representation, which wraps strings in quotes. So an unknown value `"foobar"` would appear as `'foobar'` in the error message, making it unambiguous.

---

### 3.3 `data_fetcher/cache.py`

**Purpose:** All reading, writing, merging, and validating of Parquet files is centralised here.

```python
def _cache_path(ticker: str, cache_dir: str) -> Path:
    safe_name = ticker.replace("^", "_").replace("/", "_").replace(":", "_")
    return Path(cache_dir) / f"{safe_name}.parquet"
```

`.replace()` is called chained — each call returns a new string with the substitution applied. `^GSPC` becomes `_GSPC`, so the cache file is `_GSPC.parquet`. Windows does not allow `^`, `/`, or `:` in filenames.

```python
def load(ticker: str, cache_dir: str) -> Optional[pd.DataFrame]:
    path = _cache_path(ticker, cache_dir)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    return df
```

Returns `None` on a cache miss (first run for this ticker). The return type `Optional[pd.DataFrame]` tells callers they must check for `None` before using the result.

**`validate()`** — data quality checks:

```python
null_count = int(df["close"].isna().sum())
```

`.isna()` returns a boolean Series (True where the value is null/NaN). `.sum()` counts the Trues (Python treats True as 1 and False as 0). `int()` converts from numpy integer to plain Python int.

```python
dupe_count = int(df.index.duplicated().sum())
```

`.duplicated()` marks duplicate index entries as True (the first occurrence is False).

```python
deltas = df.index.to_series().diff().dropna()
large_gaps = deltas[deltas > pd.Timedelta(days=_LARGE_GAP_DAYS)]
```

`.diff()` subtracts each element from the next, producing a Series of `Timedelta` objects (time differences). `.dropna()` removes the first element (which is NaN because there is no "previous" for it). `pd.Timedelta(days=5)` creates a 5-day duration. The boolean mask `deltas > pd.Timedelta(days=5)` selects only gaps larger than 5 days.

**`merge()`**:

```python
combined = pd.concat([existing, new])
combined = combined[~combined.index.duplicated(keep="last")].sort_index()
rows_added = max(len(combined) - before, 0)
```

`pd.concat([existing, new])` stacks two DataFrames on top of each other (vertically, same columns). The deduplication step keeps the last occurrence of any duplicate date — this means a corrected bar from the provider overwrites the stale cached value. `max(..., 0)` ensures we never return a negative row count if the new data had fewer rows than expected.

---

### 3.4 `data_fetcher/fetcher.py`

**Purpose:** Orchestrates the full watchlist fetch: batching, parallelism, retries, caching.

**`FetchSummary`** uses `@property` methods so the summary statistics are computed lazily from the underlying `results` list:

```python
@property
def succeeded(self) -> int:
    return sum(1 for r in self.results if r.status != "error")
```

`sum(1 for r in ... if ...)` is a generator expression (like a list comprehension but without the `[]`). It yields `1` for each result that is not an error, then sums them. This is equivalent to `len([r for r in self.results if r.status != "error"])` but uses less memory.

**`DataFetcher.run()`**:

```python
all_tickers = list(dict.fromkeys([self._benchmark] + tickers))
```

`dict.fromkeys(iterable)` creates a dict from a list, using each item as a key with `None` as the value. Because dict keys are unique and insertion-ordered (since Python 3.7), this removes duplicates while preserving order. Wrapping it in `list()` converts it back. The benchmark ticker is prepended by `[self._benchmark] + tickers` so it is always fetched first.

```python
batches = [
    all_tickers[i: i + self._batch_size]
    for i in range(0, len(all_tickers), self._batch_size)
]
```

`range(0, N, batch_size)` generates `0, batch_size, 2*batch_size, ...` up to `N`. The list comprehension slices `all_tickers` into chunks of `batch_size`.

**`_run_batch()`**:

```python
future_to_ticker = {
    executor.submit(self._fetch_one, ticker, full_refresh): ticker
    for ticker in batch
}
```

`executor.submit(fn, arg1, arg2)` schedules `fn(arg1, arg2)` to run in a thread and returns a `Future` object immediately. The dict maps each `Future` to the ticker it corresponds to, so when a future completes we can look up which ticker it was for.

```python
for future in as_completed(future_to_ticker):
    ticker = future_to_ticker[future]
    try:
        results.append(future.result())
    except Exception as exc:
        ...
```

`as_completed()` is an iterator that yields futures as they finish. `future.result()` retrieves the return value of `_fetch_one()`, or re-raises any exception that occurred in the thread. Wrapping in `try/except` ensures one failed ticker does not abort the rest of the batch.

**`_fetch_one()`** — decides what to fetch for one ticker:

```python
if existing is not None and not full_refresh:
    last_date = cache_store.get_last_date(existing)
    if last_date >= date.today():
        return TickerResult(ticker=ticker, status="skipped", rows_added=0)
    fetch_start = last_date + timedelta(days=1)
    mode = "delta"
else:
    fetch_start = date.today() - timedelta(days=self._history_days)
    mode = "full_fetch"
```

`timedelta(days=1)` is a `datetime.timedelta` object representing one day. Adding it to a `date` advances the date by one day.

**`_fetch_with_retry()`**:

```python
for attempt in range(1, self._max_retries + 1):
    try:
        return self._provider.fetch(ticker, start, end)
    except Exception as exc:
        last_error = exc
        if attempt < self._max_retries:
            time.sleep(self._retry_backoff * attempt)
```

`range(1, 4)` produces `1, 2, 3` — so `attempt` is 1-indexed which makes the log messages more readable. `time.sleep(backoff * attempt)` produces exponential-ish back-off: 1s, 2s, 3s waits between attempts.

---

### 3.5 `data_fetcher/__main__.py`

**Purpose:** CLI entry point for `python -m data_fetcher`.

**`_load_watchlist()`**:

```python
for key, path in wl.items():
    if key.endswith("_file") and path and Path(path).exists():
        tickers.extend(_tickers_from_file(path))
```

`dict.items()` returns `(key, value)` pairs. Any config key ending in `_file` is treated as a ticker file path. `list.extend()` appends all items from another iterable (like `+=` for lists). Files that do not exist on disk are silently skipped — this is intentional so that adding a new watchlist source is a config-only change with no code breakage.

---

### 3.6 `signal_engine/indicators.py`

**Purpose:** Pure mathematical functions for computing technical indicators. No side effects — every function takes data in and returns data out.

**`ema(series, period)`**:

```python
return series.ewm(span=period, adjust=False).mean()
```

Returns a new Series the same length as `series`. The first values will be numerically close to the actual price (the EMA needs many bars of "warm-up" before it stabilises). That is why the engine enforces `_min_bars` — to ensure all indicators are warmed up before any signal is evaluated.

**`macd(close, fast, slow, signal_period)`**:

```python
ema_fast = ema(close, fast)
ema_slow = ema(close, slow)
macd_line = ema_fast - ema_slow       # Series - Series = Series (element-wise subtraction)
signal_line = ema(macd_line, signal_period)
histogram = macd_line - signal_line
return macd_line, signal_line, histogram
```

Subtracting two Series performs element-wise arithmetic — each bar's EMA fast minus that same bar's EMA slow. The function returns three separate Series as a 3-tuple; the caller unpacks them: `macd_line, _, histogram = ind.macd(...)`.

**`atr(df, period)`**:

```python
high = df["high"]
low = df["low"]
close = df["close"]
prev_close = close.shift(1)

tr = pd.concat(
    [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
    axis=1,
).max(axis=1)

return tr.ewm(alpha=1 / period, adjust=False).mean()
```

True Range is the maximum of three expressions for each bar. `pd.concat(..., axis=1)` creates a 3-column DataFrame where each column is one of the three TR components. `.max(axis=1)` then reduces across columns, returning one value (the max) per row.

**`rs_line(close, benchmark_close)`**:

```python
aligned = benchmark_close.reindex(close.index, method="ffill")
return close / aligned
```

The benchmark (^GSPC) trades on NYSE calendar. Individual stocks may trade on different exchanges with slightly different holidays. `.reindex(..., method="ffill")` aligns the benchmark to the stock's date index, filling any missing benchmark days by repeating the previous close.

---

### 3.7 `signal_engine/strategy_a.py`

**Purpose:** Evaluates the EMA Pullback setup for one ticker.

```python
def evaluate(self, df, close, ema21, ema50, ema100, ema200, histogram) -> tuple[bool, str]:
```

Returns a `(fired, reason)` tuple. `(True, "")` means the strategy fired. `(False, "reason_code")` identifies exactly which condition failed.

**Pullback condition:**

```python
low_today       = float(df["low"].iloc[-1])
low_yesterday   = float(df["low"].iloc[-2])
close_yesterday = float(df["close"].iloc[-2])

same_bar_recovery  = low_today <= e21 < close
prior_breach       = (low_yesterday < e21) or (close_yesterday < e21)
prior_bar_recovery = prior_breach and (close > e21)

if not (same_bar_recovery or prior_bar_recovery):
    return False, "strategy_a:no_ema21_touch_or_recovery"
```

`low_today <= e21 < close` is Python's **chained comparison** — equivalent to `(low_today <= e21) and (e21 < close)`. It checks that the low touched the EMA but the close recovered above it.

The `float()` calls convert from numpy scalars to plain Python floats. This is good defensive practice — numpy scalars behave slightly differently from Python floats in edge cases.

**MACD histogram shape:**

```python
h1 = float(histogram.iloc[-1])   # today
h2 = float(histogram.iloc[-2])   # yesterday
h3 = float(histogram.iloc[-3])   # two days ago

if h1 >= 0:
    return False, "strategy_a:histogram_not_negative"
if h2 >= h3:
    return False, "strategy_a:histogram_no_prior_decline"
if h1 <= h2:
    return False, "strategy_a:histogram_not_improving"
```

The strategy checks for a "dark red to light red" pattern: the histogram was getting more negative (`h2 < h3`) and is now ticking up (`h1 > h2`), but still negative (`h1 < 0`). Each condition is checked with an early return — if any fails, we stop immediately and return the reason code.

---

### 3.8 `signal_engine/strategy_b.py`

**Purpose:** Evaluates the 50-Day Breakout setup for one ticker.

**Breakout condition with freshness check:**

```python
prior_high_today = float(df["high"].iloc[-(self._breakout_period + 1):-1].max())
if close <= prior_high_today:
    return False, "strategy_b:no_50d_breakout"
```

`df["high"].iloc[-(N+1):-1]` selects the N bars ending at yesterday (today is excluded to avoid look-ahead bias). `.max()` finds the highest high in that window. If today's close does not exceed it, no breakout.

```python
prior_high_yesterday = float(df["high"].iloc[-(self._breakout_period + 2):-2].max())
close_yesterday = float(df["close"].iloc[-2])
if close_yesterday > prior_high_yesterday:
    return False, "strategy_b:stale_breakout"
```

The freshness check asks: "Was yesterday *already* a breakout day?" If yesterday's close exceeded the N-day high as it stood at yesterday's close (a window shifted back one more bar), then this is already day 2 or later of a breakout. We only want day 1.

**Volume confirmation:**

```python
avg_volume   = float(df["volume"].iloc[-self._volume_avg_days:].mean())
today_volume = float(df["volume"].iloc[-1])

if avg_volume > 0 and today_volume < self._volume_multiplier * avg_volume:
    ratio = round(today_volume / avg_volume, 2)
    return False, f"strategy_b:low_volume_{ratio}x"
```

The `avg_volume > 0` guard prevents division by zero on tickers with no volume history. Including the ratio in the reason code (`low_volume_0.85x`) makes it easy to see how close to the threshold the ticker was, without needing to re-run the calculation.

---

### 3.9 `signal_engine/engine.py`

**Purpose:** Orchestrates the daily scan — loads data, calls strategies, assembles Signal objects.

**`Signal` dataclass** is the output contract. Every field must be populated before the Risk Layer will accept it. The `@dataclass` decorator generates `__init__` from the field annotations.

**`SignalEngine.__init__()`** extracts all parameters from config up front, so `scan()` and its helpers have no config dependency:

```python
se = config["signal_engine"]
self._ema_period: int = se["ema_period"]
self._macd_fast: int = se.get("macd_fast", 12)
```

`.get("key", default)` returns `default` if the key is absent. Used for optional config keys where a sensible default exists, so the config file doesn't need to specify them explicitly.

**`_min_bars` calculation:**

```python
self._min_bars: int = max(
    self._macd_slow + self._macd_signal_period + 10,
    100 + 10,
    self._swing_low_period,
    self._strat_a.min_bars_required,
    self._strat_b.min_bars_required,
)
```

`max(a, b, c, ...)` returns the largest value. This ensures all indicators are fully warmed up before any signal can be evaluated. The `+ 10` adds a safety buffer.

**`scan()` — bear regime gate:**

```python
if regime == "bear":
    self._logger.info({
        "event": "regime_filter_active",
        "regime": "bear",
        "tickers_skipped": len(tickers),
    })
    return []
```

An early `return []` (empty list) is the cleanest way to short-circuit the scan. No tickers are evaluated in a bear market.

**Stop price — 3-step calculation:**

```python
entry     = close
swing_low = float(df["low"].iloc[-self._swing_low_period:].min())

atr_val      = float(atr_series.iloc[-1])
atr_stop_val = entry - self._stop_atr_mult * atr_val
stop = min(swing_low, atr_stop_val)
stop_method  = "swing_low" if swing_low <= atr_stop_val else "atr_floor"

max_risk    = entry * (self._stop_hard_cap_pct / 100)
stop_capped = (entry - stop) > max_risk
if stop_capped:
    stop        = entry - max_risk
    stop_method = "hard_cap"
stop = round(stop, 4)
```

`min(a, b)` returns the lower value — the wider (more conservative) stop. The hard cap then overrides this if the stop would be more than 8% away from entry.

**Conviction and liquidity annotation:**

```python
near_52wk  = close >= float(df["high"].iloc[-bars_for_52wk:].max()) * (1 - self._near_52wk_pct / 100)
conviction = "elevated" if (a_fired and b_fired) or near_52wk else "standard"
```

`a_fired and b_fired` uses Python's boolean `and` — True only if both strategies fired. The `or` means elevation triggers on either condition.

---

### 3.10 `signal_engine/db.py`

**Purpose:** SQLite persistence for signals. Defines the schema and provides `init_db()` and `save_signals()`.

**Schema definition pattern:**

```python
_COLUMNS: list[tuple[str, str, str]] = [
    ("ticker", "TEXT NOT NULL", "Yahoo Finance ticker"),
    ("entry_price", "REAL", "last close at signal time"),
    ...
]
```

Storing the schema as a list of tuples (column_name, sql_type, comment) allows both the `CREATE TABLE` statement and the forward-migration to be generated from the same source of truth, so they can never get out of sync.

**Forward migration:**

```python
existing = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
for name, sql_type, _ in _MIGRATABLE_COLS:
    if name not in existing:
        base_type = sql_type.split()[0]
        conn.execute(f"ALTER TABLE signals ADD COLUMN {name} {base_type}")
```

`PRAGMA table_info(table)` returns one row per column; `row[1]` is the column name. Building a set `{row[1] for row in ...}` (a set comprehension) makes membership lookups instant. `sql_type.split()[0]` extracts just the type word (`"TEXT"` from `"TEXT NOT NULL"`), because `ALTER TABLE ADD COLUMN` does not support `NOT NULL` constraints.

**`save_signals()`:**

```python
rows = [
    (
        s.signal_timestamp.isoformat(),
        s.ticker,
        ...
        int(s.stop_capped),
        ...
    )
    for s in signals
]
with sqlite3.connect(db_path) as conn:
    conn.executemany(_INSERT_SIGNAL, rows)
```

SQLite has no native boolean type. `int(s.stop_capped)` converts `True → 1` and `False → 0`. When reading back, `bool(row["stop_capped"])` converts `1 → True` and `0 → False`.

---

### 3.11 `signal_engine/__main__.py`

**Purpose:** CLI for `python -m signal_engine`.

```python
tickers = args.tickers or _load_watchlist(config)
```

`args.tickers` is either a non-empty list (if the user passed tickers on the command line) or an empty list `[]` (if not). An empty list is falsy, so `or _load_watchlist(config)` provides the fallback.

**Formatted output table:**

```python
print(f"\n{'Ticker':<14} {'Type':<22} {'Conv':<10} {'Entry':>8} {'Stop':>8} {'Target':>8} {'Risk%':>6}  {'Flag'}")
print("-" * 90)
for s in signals:
    risk_pct = (s.entry_price - s.stop_price) / s.entry_price * 100
    flag = "⚠ CAP" if s.stop_capped else ""
    print(
        f"{s.ticker:<14} {s.signal_type:<22} {s.conviction:<10} "
        f"{s.entry_price:>8.2f} {s.stop_price:>8.2f} {s.target_price:>8.2f} "
        f"{risk_pct:>5.1f}%  {flag}"
    )
```

`"Ticker":<14` left-aligns the string in a 14-character field. `':>8.2f'` right-aligns a float in an 8-character field with 2 decimal places. This creates a tabular layout without any table library.

---

### 3.12 `risk_layer/calculator.py`

**Purpose:** Pure math functions for position sizing. No I/O, no state.

**`size_position()`**:

```python
effective_cap_pct = (
    max_position_risk_pct * thin_size_multiplier
    if liquidity_class == "thin"
    else max_position_risk_pct
)
```

This is a **conditional expression** (ternary operator): `value_if_true if condition else value_if_false`. Equivalent to:

```python
if liquidity_class == "thin":
    effective_cap_pct = max_position_risk_pct * thin_size_multiplier
else:
    effective_cap_pct = max_position_risk_pct
```

```python
from math import floor
shares = floor(max_risk_amount / risk_per_share)
```

`math.floor()` rounds *down* to the nearest integer. This guarantees we never buy more shares than the risk cap allows — even a fractional share over the limit would exceed the cap.

**`open_risk_pct()`**:

```python
def open_risk_pct(total_risk_amount: float, portfolio_value: float) -> float:
    if portfolio_value <= 0:
        return 0.0
    return round((total_risk_amount / portfolio_value) * 100, 4)
```

The guard `if portfolio_value <= 0` prevents division by zero. `round(value, 4)` rounds to 4 decimal places — enough precision for percentage display without floating-point noise.

---

### 3.13 `risk_layer/state.py`

**Purpose:** All SQLite operations for the Risk Layer. Two tables: `risk_positions` and `system_state`.

**`add_position()`** — inserting a new position:

```python
opened_at = datetime.now(timezone.utc).isoformat()
with sqlite3.connect(db_path) as conn:
    cur = conn.execute(_INSERT_POSITION, (...))
    return cur.lastrowid
```

`datetime.now(timezone.utc)` gets the current time in UTC (Coordinated Universal Time). `.isoformat()` formats it as `"2025-06-03T10:45:23.123456+00:00"`. Using UTC consistently means timestamps are unambiguous regardless of where the bot runs. `cur.lastrowid` returns the auto-incremented integer ID assigned to the new row.

**`get_daily_realized_pnl()`**:

```python
def get_daily_realized_pnl(db_path: str, for_date: Optional[date] = None) -> float:
    target = (for_date or date.today()).isoformat()
```

`for_date or date.today()` uses the provided date if given, or today's date as the default. This allows historical queries (used in testing) while defaulting to today in production.

**Trading pause — date-scoped approach:**

```python
def is_trading_paused(db_path: str) -> bool:
    row = conn.execute("SELECT value FROM system_state WHERE key = ?", (_PAUSE_KEY,)).fetchone()
    if row is None:
        return False
    return row[0] == date.today().isoformat()
```

The pause value stored is the ISO date string on which the limit was breached. If today's date matches the stored date, trading is paused. At midnight, `date.today()` returns a new date, so the comparison fails and the pause expires automatically — no cron job or reset needed.

**UPSERT pattern:**

```python
conn.execute(
    "INSERT INTO system_state (key, value) VALUES (?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
    (_PAUSE_KEY, today),
)
```

`ON CONFLICT DO UPDATE` is SQLite's UPSERT (update-or-insert). If the key already exists, it updates in-place. If it doesn't exist, it inserts. `excluded.value` refers to the value that would have been inserted. This avoids a separate check-then-insert or check-then-update pattern.

---

### 3.14 `risk_layer/layer.py`

**Purpose:** The pre-trade risk gate. Evaluates every Signal before it reaches the executor.

**`RiskDecision` dataclass:**

```python
@dataclass
class RiskDecision:
    approved: bool
    signal: "Signal"
    shares: int
    ...
    reject_reason: Optional[str]
```

The string `"Signal"` in quotes is a **forward reference** — at the time Python parses this line, `Signal` is not yet imported (it is imported conditionally under `TYPE_CHECKING`). The quotes defer evaluation until type-checking tools need it.

**`evaluate()` — inner `_reject()` helper:**

```python
def _reject(reason: str) -> RiskDecision:
    self._logger.info({...})
    return RiskDecision(approved=False, ..., reject_reason=reason)
```

A **nested function** defined inside `evaluate()`. It has access to variables from the enclosing scope (`signal`, `current_open_risk_pct`, `portfolio_value`, `self`) via Python's closure mechanism. This avoids repeating the logger call and the `RiskDecision` construction for every rejection.

**Check ordering — defensive programming:**

The six risk checks run in a deliberate order, cheapest first:
1. `is_trading_paused` — one DB read, no math
2. `has_open_position` — one DB read, no math
3. Daily P&L — one DB read + arithmetic
4. Position sizing — pure math
5. Per-trade cap check — comparison
6. Total open risk check — one DB read + arithmetic + comparison

Each check returns early with a rejected decision if it fails, so subsequent (more expensive) checks are never reached.

**`open_position()`** vs `evaluate()`:

`evaluate()` is called before an order is placed. `open_position()` is called *after* a fill is confirmed. This separation matters: an approved signal might still fail to fill (market closed, order rejected by broker). Recording it in the DB prematurely would over-count open risk and block subsequent valid signals.

---

### 3.15 `risk_layer/__main__.py`

**Purpose:** CLI for `python -m risk_layer status/close/unpause`.

This module demonstrates **deferred imports** — imports inside the command handler functions rather than at the top of the file:

```python
def _cmd_status(config: dict) -> None:
    from utils.json_logger import get_logger
    from risk_layer.layer import RiskLayer
    from risk_layer import state as st
    ...
```

Deferring imports keeps startup fast (argument parsing and `--help` work without loading heavy modules) and avoids potential circular import issues during module initialisation.

**`subparsers`** pattern — the cleanest way to build multi-command CLIs:

```python
subparsers = parser.add_subparsers(dest="command")
subparsers.add_parser("status")
close_p = subparsers.add_parser("close")
close_p.add_argument("ticker")
close_p.add_argument("price", type=float)
close_p.add_argument("reason", choices=["stop", "target", "trail", "manual"])
```

`choices=["stop", ...]` makes argparse reject any value not in the list and display them in the help text automatically.

---

### 3.16 `sim_executor/fills.py`

**Purpose:** Calculates fill details and calls `risk_layer.open_position()` to persist the position.

```python
fill_price = decision.signal.entry_price
fill_timestamp = datetime.now(timezone.utc).isoformat()
entry_commission = round(fill_price * decision.shares * (tob_pct / 100), 2)
```

The TOB (Belgian stock exchange transaction tax) is calculated as a flat percentage of the total transaction value. `fill_price * decision.shares` is the total notional value. `tob_pct / 100` converts the percentage (0.35) to a decimal (0.0035). `round(..., 2)` gives cent precision.

```python
row_id = risk_layer.open_position(
    decision,
    fill_price=fill_price,
    fill_timestamp=fill_timestamp,
    entry_commission=entry_commission,
    bot_initiated=True,
    peak_price=fill_price,
)
```

`peak_price=fill_price` initialises the all-time high to the fill price. The Position Manager updates it daily as the position moves in our favour. Named (keyword) arguments are used here for clarity — the function signature has many optional parameters, and naming them prevents passing values in the wrong order.

---

### 3.17 `sim_executor/notify.py`

**Purpose:** Sends Telegram messages for fills and system alerts.

**`urllib` for HTTP (stdlib — no dependency on `requests`):**

```python
url = _API_BASE.format(token=bot_token)
payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
req = urllib.request.Request(
    url,
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
```

`_API_BASE.format(token=bot_token)` uses `.format()` (older Python string formatting, equivalent to an f-string). `json.dumps(dict)` converts a dict to a JSON string. `.encode()` converts the string to bytes (required for the `data=` parameter).

```python
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            logger.warning({...})
except urllib.error.URLError as exc:
    logger.warning({...})
except Exception as exc:
    logger.warning({...})
```

Three layers of protection: network errors (`URLError`), any other unexpected error (`Exception`). All are caught and logged but never re-raised — a notification failure must never abort a fill that has already been committed to the database. The database is the authoritative record; Telegram is best-effort.

---

### 3.18 `sim_executor/executor.py`

**Purpose:** Reads signals from `signals.db`, runs them through the Risk Layer, records fills.

**`_row_to_signal()`** — reconstructing a Signal from a database row:

```python
ts_str = row["signal_timestamp"]
ts = datetime.fromisoformat(ts_str)
if ts.tzinfo is None:
    ts = ts.replace(tzinfo=timezone.utc)
```

`datetime.fromisoformat()` parses ISO-8601 strings. Older rows written before timezone-aware timestamps were standardised may not have the `+00:00` suffix. The `if ts.tzinfo is None` check attaches UTC explicitly so the Risk Layer always receives a timezone-aware datetime.

```python
earnings_raw = row["earnings_flag"]
earnings_flag = None if earnings_raw is None else bool(earnings_raw)
```

`None if X is None else bool(X)` is the safe pattern for optional booleans stored as nullable integers in SQLite. `bool(1) → True`, `bool(0) → False`, but `bool(None) → False` which would lose the "unknown" distinction — hence the explicit `None` check.

**Idempotency design:**

```python
if not self._dry_run:
    self._mark_processed(row["id"])
```

This is always the *last* step in `_process_row()`. If the process crashes between recording the fill and marking the signal, the signal will be reprocessed on the next run — and the Risk Layer's duplicate check will catch it. This "mark last" pattern ensures at-most-once processing is achieved in practice, even without distributed coordination.

---

### 3.19 `sim_executor/__main__.py`

**Purpose:** CLI for `python -m sim_executor`.

```python
se_db.init_db(config["signal_engine"]["db_path"])
```

The Signal Engine's schema migration runs on every SX startup. This ensures the `processed` and `run_type` columns exist on any database created before SX was built — a forward-only migration that is safe to call repeatedly (it is idempotent).

```python
executor = SimExecutor(config, logger, dry_run=args.dry_run)
if args.watch:
    executor.run_watch()
else:
    executor.run_batch()
```

The `--dry-run` flag is passed through to `SimExecutor` which disables all writes, while still evaluating signals through the full Risk Layer logic. The `--watch` flag switches from one-shot batch to continuous polling.

---

### 3.20 `position_manager/trail.py`

**Purpose:** Pure math for the ATR trailing stop. No database access, no logging.

**`calc_cost_floor()`**:

```python
return round(entry_price * (1.0 + 2.0 * tob_pct / 100.0), 4)
```

The cost floor covers both entry and exit TOB (two sides of the transaction). With `tob_pct = 0.35`:
- `2 × 0.35 / 100 = 0.007`
- `entry × 1.007` = the price at which exit proceeds (after paying exit TOB) exactly equal the entry cost (after paying entry TOB)

**`classify_volatility_bucket()`**:

```python
if atr_pct < low_threshold_pct:
    return "low"
if atr_pct < high_threshold_pct:
    return "medium"
return "high"
```

A cascading if-else without an explicit `elif`. Python evaluates conditions top-to-bottom and returns immediately on the first match. If `atr_pct` is not less than `low_threshold_pct`, the first `return` is skipped and we reach the second check. If not less than `high_threshold_pct` either, the final `return "high"` is unconditional.

**`calc_atr_trail_level()`**:

```python
return round(running_high - atr14 * multiplier, 4)
```

Operator precedence: `atr14 * multiplier` is evaluated first (multiplication before subtraction, same as standard arithmetic). No parentheses needed.

**`calc_stop_proximity_ratio()`**:

```python
denominator = entry_price - stop_price
if abs(denominator) < 1e-8:
    return 1.0
return (current_price - stop_price) / denominator
```

`1e-8` is scientific notation for `0.00000001`. The guard against near-zero denominators uses `abs()` rather than `== 0` because floating-point arithmetic can produce tiny non-zero values where mathematically zero is expected.

---

### 3.21 `position_manager/state.py`

**Purpose:** SQLite write operations for the Position Manager. Does not own the schema — it writes to columns defined and migrated by `risk_layer/state.py`.

**`update_position_stop()`**:

```python
rowcount = conn.execute(
    """UPDATE risk_positions
       SET stop_price = ?, risk_per_share = ?, risk_amount = ?
       WHERE ticker = ? AND status = 'open'""",
    (new_stop, new_risk_per_share, max(0.0, new_risk_amount), ticker),
).rowcount
return rowcount > 0
```

`conn.execute().rowcount` is the number of rows affected by the UPDATE. Returning `rowcount > 0` lets the caller know whether the ticker had an open position to update. `max(0.0, new_risk_amount)` floors the risk at zero — once the stop is above entry, `entry - stop` is negative and `shares × risk_per_share` would be negative too, which would produce a nonsensical negative risk amount.

**`close_position_full()`** — why it exists separately from `risk_layer.state.close_position()`:

`risk_layer.state.close_position()` is intentionally minimal — it sets `status`, `close_price`, `realized_pnl`, and `close_reason`, which is all the Risk Layer needs for the daily loss limit check. The Position Manager needs to set additional fields: `gross_pnl`, `net_pnl`, `exit_commission`, `bot_initiated`, and `exit_note`. Putting PM concerns into the Risk Layer would violate the single-responsibility principle. The two functions write to the same row in the same database but stay in their own modules.

---

### 3.22 `position_manager/notify.py`

**Purpose:** Telegram messages for all Position Manager events: trail activation, trail update, time exit, and manual close.

```python
reason_labels = {
    "no_pending_signals":   "Signal queue empty — no opportunity cost",
    "open_risk_below_cap":  "Open risk below 6% cap — risk budget not constrained",
    "not_near_stop":        "Price not near stop — holding for stop-managed exit",
}
label = reason_labels.get(hold_reason, hold_reason)
```

`dict.get(key, default)` returns `default` if the key is absent. Using the raw `hold_reason` string as the default means unknown reason codes are still displayed rather than silently discarded.

Formatting P&L with sign handling:

```python
pnl_sign = "+" if net_pnl >= 0 else "−"
pnl_abs = abs(net_pnl)
text = f"Net P&L: {pnl_sign}€{pnl_abs:,.2f}"
```

This uses a Unicode minus sign `−` (not a hyphen `-`) for negative values, which looks better in Telegram messages. `abs()` gives the absolute value so the sign character and the number are formatted separately.

---

### 3.23 `position_manager/manager.py`

**Purpose:** Runs the daily EOD management cycle for all open positions.

**`run_eod()`**:

```python
for pos in positions:
    try:
        self._process_position(pos)
    except Exception as exc:
        self._logger.warning({"event": "position_processing_error", ...})
```

Each position is wrapped in a `try/except` so a crash on one position does not abort all the others. This is critical for a daily batch process — if AAPL's Parquet file is corrupted, MSFT should still be managed correctly.

**`_process_position()`** — reading position data:

```python
current_peak = float(pos["peak_price"] or pos.get("fill_price") or pos["entry_price"])
```

This cascading `or` handles the case where `peak_price` is NULL in the database (possible on old rows before the column was added). `.get("fill_price")` uses dict's `.get()` which returns `None` if the key is absent — though since `pos` comes from `sqlite3.Row` converted to a dict, all schema columns will be present (possibly as `None`).

**`_count_trading_days_since_open()`**:

```python
if df.index.tz is not None:
    idx_dates = df.index.tz_localize(None).normalize()
else:
    idx_dates = df.index.normalize()
entry_dt = pd.Timestamp(entry_date)
return int((idx_dates >= entry_dt).sum())
```

`df.index.normalize()` strips the time component from a `DatetimeIndex`, leaving just the date. `idx_dates >= entry_dt` produces a boolean Series (True for each bar on or after the entry date). `.sum()` counts the Trues. This accurately counts trading days without needing an external trading calendar library — the Parquet file already has one row per trading day, so counting rows is counting trading days.

**`_evaluate_time_exit()`** — three AND conditions:

```python
pending = pm_state.count_pending_signals(self._signals_db)
if pending == 0:
    self._log_time_hold(ticker, trading_days, "no_pending_signals", ...)
    return

open_risk_pct = pm_state.get_open_risk_pct(self._risk_db, self._portfolio_value)
if open_risk_pct < self._max_open_risk_pct:
    self._log_time_hold(ticker, trading_days, "open_risk_below_cap", ...)
    return

proximity = tr.calc_stop_proximity_ratio(current_price, pos["stop_price"], pos["entry_price"])
if not (proximity <= (self._stop_proximity_pct / 100.0)):
    self._log_time_hold(ticker, trading_days, "not_near_stop", ...)
    return

# All three conditions met — close the position
result = pm_state.close_position_full(...)
```

The early-return pattern on each condition keeps the code flat (avoids deeply nested `if/elif/else`). Each failure reason is logged with its specific detail so the trading diary shows exactly why a time exit was held.

---

### 3.24 `position_manager/__main__.py`

**Purpose:** CLI for `python -m position_manager`.

```python
def _load_config() -> dict:
    root = Path(__file__).parent.parent
    config_path = root / "config.yaml"
```

`__file__` is a special variable set to the path of the current file. `.parent` goes up one directory. `.parent.parent` goes up two — from `position_manager/__main__.py` to the project root. This makes the config loading path-independent: the module works correctly regardless of what directory the command is run from.

**Manual CLI parsing (instead of argparse):**

```python
argv = sys.argv[1:]

if not argv or argv[0] == "run":
    _cmd_run(config)
    return

cmd = argv[0].lower()
if cmd == "status":
    _cmd_status(config)
elif cmd == "close":
    _cmd_close(config, argv[1:])
```

`sys.argv` is the list of command-line arguments. `sys.argv[0]` is the script name, so `sys.argv[1:]` contains only the user-provided arguments. This module uses manual parsing instead of argparse because the `close` command's `NOTE` argument accepts multi-word text without quoting: `python -m position_manager close ASML.AS 72.50 closed early earnings risk`. Argparse would require quoting (`"closed early earnings risk"`), but this manual approach joins `argv[2:]` with spaces.

```python
note = " ".join(args[2:]) if len(args) > 2 else None
```

`" ".join(list)` concatenates a list of strings with a space separator. `["closed", "early", "earnings", "risk"]` becomes `"closed early earnings risk"`.

---

## Appendix — Quick Reference

### Python operators used in this codebase

| Expression | Meaning |
|---|---|
| `a or b` | `a` if truthy, else `b` |
| `a and b` | `b` if `a` is truthy, else `a` |
| `not a` | Logical NOT |
| `a is None` | Identity check (use for `None`, not `==`) |
| `a is not None` | Same |
| `a if cond else b` | Ternary/conditional expression |
| `~mask` | Invert a boolean pandas Series/array |
| `**dict` | Unpack dict into keyword arguments or another dict |
| `a <= b < c` | Chained comparison: `(a <= b) and (b < c)` |
| `f"{x:.2f}"` | Format float to 2 decimal places |
| `f"{x:,}"` | Format number with thousands separator |
| `1e-8` | Scientific notation: `0.00000001` |
| `[x for x in y if z]` | List comprehension |
| `{k: v for k, v in y}` | Dict comprehension |
| `sum(1 for x in y if z)` | Count items matching condition |

### Pipeline summary

```
python -m data_fetcher
    → Downloads OHLCV bars from Yahoo Finance
    → Caches to ./cache/<TICKER>.parquet

python -m signal_engine
    → Reads Parquet cache for all watchlist tickers
    → Runs Strategy A (EMA Pullback) and Strategy B (Breakout) per ticker
    → Saves fired signals to ./data/signals.db

python -m sim_executor
    → Reads unprocessed signals from ./data/signals.db
    → Passes each signal through RiskLayer.evaluate()
    → On approval: records position to ./data/risk.db
    → Marks signal processed = 1

python -m position_manager [run]
    → Reads open positions from ./data/risk.db
    → For each position: loads Parquet, updates peak, calculates ATR trail
    → Advances stop if trail has risen
    → Checks time-exit conditions on stalled positions
    → Sends Telegram notifications for trail/exit events
```
