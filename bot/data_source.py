"""External data sources (yfinance primary) for IB bot.

Designed to offload heavy historical-bar requests from the IBKR socket so the
shared pacing pool (60 req/10s) stays reserved for order execution + position
queries. yfinance handles ~99% of bar fetches; IBKR is fallback only.

Coverage:
  • US stocks/ETFs              → yfinance "AAPL", "SPY"
  • CME futures continuous       → yfinance "ES=F", "NQ=F", ...
  • Micro futures (no separate yf series) → use full counterpart "ES=F", etc.
  • CME FX futures (6E/6J/...)   → use spot equivalent "EURUSD=X", ...
  • IDEALPRO FX spot              → yfinance "EURUSD=X"

Cache strategy:
  • in-memory: (symbol, interval) → (DataFrame, fetched_at) TTL = bar_ms / 2
  • disk: data/yf_cache/<interval>/<symbol>.parquet (resilient to restarts)
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Lock
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# Silence yfinance — it logs "delisted/no data" as ERROR for any symbol it can't
# fetch (cross FX, illiquid stocks, etc). These are NORMAL data-availability
# misses, not bot errors. We surface our own warnings.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("yfinance.utils").setLevel(logging.CRITICAL)
logging.getLogger("yfinance.data").setLevel(logging.CRITICAL)
logging.getLogger("yfinance.shared").setLevel(logging.CRITICAL)

# Bar duration in ms (mirror bot.config.TF_MS to avoid circular import)
_TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000, "1w": 604_800_000,
}

# yfinance native intervals (only these can be downloaded directly)
_YF_NATIVE_INTERVAL = {
    "1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m", "30m": "30m",
    "60m": "60m", "1h": "60m", "1d": "1d", "1wk": "1wk", "1mo": "1mo",
}

# Futures: our symbol → yfinance continuous-front-month ticker
# Microsexisting mapping: micros use full underlying for bars (same price series, just smaller mult)
_FUT_YF_MAP = {
    "ES": "ES=F", "NQ": "NQ=F", "YM": "YM=F", "RTY": "RTY=F",
    "MES": "ES=F", "MNQ": "NQ=F", "MYM": "YM=F", "M2K": "RTY=F",
    "CL": "CL=F", "NG": "NG=F", "BZ": "BZ=F", "RB": "RB=F", "HO": "HO=F",
    "MCL": "CL=F", "MNG": "NG=F",
    "GC": "GC=F", "SI": "SI=F", "HG": "HG=F", "PL": "PL=F", "PA": "PA=F",
    "MGC": "GC=F", "SIL": "SI=F", "MHG": "HG=F",
    "ZN": "ZN=F", "ZB": "ZB=F", "ZF": "ZF=F", "ZT": "ZT=F",
    "ZC": "ZC=F", "ZS": "ZS=F", "ZW": "ZW=F", "ZL": "ZL=F", "ZM": "ZM=F",
    "KC": "KC=F", "CT": "CT=F", "SB": "SB=F", "CC": "CC=F",
    # FX futures → spot proxy (price series identical for pattern detection)
    "6E": "EURUSD=X", "6J": "JPY=X", "6B": "GBPUSD=X", "6A": "AUDUSD=X",
    "6C": "CAD=X", "6S": "CHF=X", "6N": "NZDUSD=X",
    "EUR_FX": "EURUSD=X", "JPY_FX": "JPY=X", "GBP_FX": "GBPUSD=X", "AUD_FX": "AUDUSD=X",
}

# FX spot: our symbol → yfinance =X form
_FX_SPOT_YF_MAP = {
    "EUR.USD": "EURUSD=X", "USD.JPY": "JPY=X", "GBP.USD": "GBPUSD=X",
    "AUD.USD": "AUDUSD=X", "USD.CAD": "CAD=X", "USD.CHF": "CHF=X",
    "NZD.USD": "NZDUSD=X",
    "EURUSD": "EURUSD=X", "USDJPY": "JPY=X", "GBPUSD": "GBPUSD=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "CAD=X", "USDCHF": "CHF=X",
    "NZDUSD": "NZDUSD=X",
}


_CCY_CODES = {
    "USD","EUR","JPY","GBP","AUD","CAD","CHF","NZD",
    "CNH","HKD","SGD","ZAR","MXN","TRY","SEK","NOK","DKK",
    "PLN","HUF","CZK","RUB","BRL","INR","KRW","TWD","THB","ILS","RON","CLP","COP","PEN","ARS","CNY"
}


def to_yf_ticker(symbol: str) -> str:
    """Map our bot symbol → yfinance ticker.

    Examples:
      AAPL    → "AAPL"        (stock)
      SPY     → "SPY"         (ETF)
      ES      → "ES=F"        (CME continuous future)
      MES     → "ES=F"        (micro uses full underlying for bars)
      6E      → "EURUSD=X"    (FX future → spot proxy)
      EUR.USD → "EURUSD=X"    (IDEALPRO spot)
      GBP.AUD → "GBPAUD=X"    (any "XYZ.ABC" cross pair)
      ^VIX    → "^VIX"        (index, pass-through)
    """
    s = symbol.strip()
    if not s:
        return s
    # Indices (^VIX, ^GSPC)
    if s.startswith("^"):
        return s
    # Futures (explicit map for collisions & micros)
    if s in _FUT_YF_MAP:
        return _FUT_YF_MAP[s]
    # FX spot (explicit map for legacy aliases)
    if s in _FX_SPOT_YF_MAP:
        return _FX_SPOT_YF_MAP[s]
    # FX spot — generic "XYZ.ABC" pair (any currency-code combo)
    if "." in s:
        parts = s.split(".")
        if len(parts) == 2 and parts[0] in _CCY_CODES and parts[1] in _CCY_CODES:
            # Yahoo Finance FX convention:
            #   USD-base pairs: "EURUSD=X" (foreign/USD direct), "JPY=X" (USD/JPY shortcut)
            #   Cross pairs:    "GBPAUD=X" (full code, no shortcut)
            base, quote = parts
            # Special-case USD/X: yfinance uses short form "X=X" (e.g. USD.JPY → JPY=X)
            if base == "USD":
                return f"{quote}=X"
            return f"{base}{quote}=X"
    # Default: stock/ETF — symbol as-is
    return s


class YFinanceSource:
    """yfinance bar fetcher with in-memory + disk cache.

    Returns DataFrame compatible with bot's expected format:
      columns = [time, Open, High, Low, Close, Volume]
      time = pd.Timestamp UTC
      sorted ascending
    """

    def __init__(self, cache_dir: Path = Path("data/yf_cache"), disk_cache: bool = True) -> None:
        self.cache_dir = cache_dir
        self.disk_cache = disk_cache
        if disk_cache:
            cache_dir.mkdir(parents=True, exist_ok=True)
        # in-memory: (yf_ticker, interval) → (df, fetched_ts)
        self._mem: dict[tuple[str, str], tuple[pd.DataFrame, float]] = {}
        self._lock = Lock()
        self._fail_cache: dict[tuple[str, str], float] = {}  # last fail ts
        self._FAIL_BACKOFF = 600.0  # don't re-try a failing ticker for 10min

    # ---------- yfinance import (lazy) ----------

    @staticmethod
    def _yf():
        try:
            import yfinance as yf  # type: ignore
            return yf
        except ImportError:
            return None

    # ---------- candles ----------

    def candles(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        """Fetch OHLCV bars. Returns empty DataFrame on failure (caller falls back)."""
        ticker = to_yf_ticker(symbol)
        if not ticker:
            return self._empty()

        # In-memory cache: TTL = bar_ms / 2
        bar_ms = _TF_MS.get(interval, 3_600_000)
        ttl = bar_ms / 2000.0  # seconds
        now = time.time()
        key = (ticker, interval)

        with self._lock:
            cached = self._mem.get(key)
        if cached is not None:
            df, fetched = cached
            if (now - fetched) < ttl:
                return df.tail(limit).copy()

        # Fail-backoff: skip recently-failed tickers
        last_fail = self._fail_cache.get(key, 0.0)
        if (now - last_fail) < self._FAIL_BACKOFF:
            # serve stale memcache if exists
            if cached is not None:
                return cached[0].tail(limit).copy()
            return self._empty()

        # Disk cache: load if exists and within bar_ms
        df_disk = self._load_disk(ticker, interval)
        if df_disk is not None and not df_disk.empty:
            last_ts = pd.Timestamp(df_disk["time"].iloc[-1])
            age_ms = (pd.Timestamp.utcnow() - last_ts.tz_convert("UTC") if last_ts.tzinfo else
                      pd.Timestamp.utcnow().tz_localize(None) - last_ts).total_seconds() * 1000
            if age_ms < bar_ms:
                with self._lock:
                    self._mem[key] = (df_disk, now)
                return df_disk.tail(limit).copy()

        # Fresh fetch from yfinance
        try:
            df = self._fetch_yf(ticker, interval, limit)
        except Exception as e:
            log.warning("yf fetch %s %s failed: %s", ticker, interval, e)
            self._fail_cache[key] = now
            if cached is not None:
                return cached[0].tail(limit).copy()
            if df_disk is not None:
                return df_disk.tail(limit).copy()
            return self._empty()

        if df.empty:
            self._fail_cache[key] = now
            return df_disk.tail(limit).copy() if df_disk is not None else self._empty()

        with self._lock:
            self._mem[key] = (df, now)
        if self.disk_cache:
            self._save_disk(ticker, interval, df)
        return df.tail(limit).copy()

    def _fetch_yf(self, ticker: str, interval: str, limit: int) -> pd.DataFrame:
        yf = self._yf()
        if yf is None:
            return self._empty()

        # Native yf interval mapping
        bar_ms = _TF_MS.get(interval, 3_600_000)
        # For 4h: download 1h and resample
        if interval == "4h":
            return self._fetch_4h(ticker, limit)

        yf_interval = _YF_NATIVE_INTERVAL.get(interval)
        if yf_interval is None:
            log.debug("yf unsupported TF %s for %s", interval, ticker)
            return self._empty()

        # Period: yfinance max for 1h is "730d"; smaller TFs even less. Pick wisely.
        if interval in ("1m",):
            period = "7d"
        elif interval in ("2m", "5m", "15m", "30m"):
            period = "60d"
        elif interval in ("1h", "60m"):
            # 1h: max 730d; we ask 60d to fit limit≈200×1h=8d with buffer
            period = "60d"
        elif interval == "1d":
            # bot uses 1d for higher_pairs ref; 5y is plenty for 200 bars
            period = "5y"
        else:
            period = "2y"

        raw = yf.download(
            tickers=ticker, period=period, interval=yf_interval,
            auto_adjust=False, prepost=False, threads=False, progress=False,
        )
        return self._normalize_yf(raw)

    def _fetch_4h(self, ticker: str, limit: int) -> pd.DataFrame:
        """Aggregate 4h bars from 1h. Aligns to UTC 00:00 boundary."""
        yf = self._yf()
        if yf is None:
            return self._empty()
        raw = yf.download(
            tickers=ticker, period="60d", interval="60m",
            auto_adjust=False, prepost=False, threads=False, progress=False,
        )
        df_1h = self._normalize_yf(raw)
        if df_1h.empty:
            return df_1h
        # Resample 1h → 4h, UTC-aligned
        df_1h = df_1h.set_index("time")
        df_4h = df_1h.resample("4h", origin="epoch").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum",
        }).dropna(subset=["Open"]).reset_index()
        return df_4h

    @staticmethod
    def _normalize_yf(raw: Optional[pd.DataFrame]) -> pd.DataFrame:
        if raw is None or raw.empty:
            return YFinanceSource._empty()
        df = raw.copy()
        # Flatten MultiIndex columns (yfinance returns multi-level when tickers list)
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        # Ensure required columns
        for c in ("Open", "High", "Low", "Close", "Volume"):
            if c not in df.columns:
                return YFinanceSource._empty()
        # Time → UTC pd.Timestamp
        df = df.reset_index()
        time_col = "Datetime" if "Datetime" in df.columns else ("Date" if "Date" in df.columns else df.columns[0])
        df["time"] = pd.to_datetime(df[time_col], utc=True)
        df = df[["time", "Open", "High", "Low", "Close", "Volume"]].copy()
        df = df.dropna(subset=["Open"]).reset_index(drop=True)
        for c in ("Open", "High", "Low", "Close", "Volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["Close"]).reset_index(drop=True)

    @staticmethod
    def _empty() -> pd.DataFrame:
        return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close", "Volume"])

    # ---------- disk cache ----------

    def _disk_path(self, ticker: str, interval: str) -> Path:
        safe = ticker.replace("=", "_eq_").replace("^", "_idx_").replace(".", "_dot_").replace("/", "_")
        return self.cache_dir / interval / f"{safe}.parquet"

    def _load_disk(self, ticker: str, interval: str) -> Optional[pd.DataFrame]:
        if not self.disk_cache:
            return None
        p = self._disk_path(ticker, interval)
        if not p.exists():
            return None
        try:
            return pd.read_parquet(p)
        except Exception:
            return None

    def _save_disk(self, ticker: str, interval: str, df: pd.DataFrame) -> None:
        if not self.disk_cache or df.empty:
            return
        p = self._disk_path(ticker, interval)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            df.to_parquet(p, index=False)
        except Exception as e:
            log.debug("disk save %s failed: %s", p, e)


    # ---------- batch warmup ----------

    def warmup_batch(self, symbols: list[str], interval: str, batch_size: int = 50) -> int:
        """Fetch bars for many symbols in batches via yf.download multi-ticker.

        Single batch call returns DataFrame with multi-level columns
        (ticker → OHLCV). Splits and caches per-symbol. Returns count succeeded.

        For 4h: downloads 1h source and resamples (yfinance has no native 4h).
        """
        yf = self._yf()
        if yf is None:
            return 0

        # For 4h: download 1h, resample per-symbol
        is_4h = (interval == "4h")
        yf_interval = "60m" if is_4h else _YF_NATIVE_INTERVAL.get(interval)
        if yf_interval is None:
            log.warning("warmup_batch: unsupported interval %s", interval)
            return 0

        if yf_interval == "1d":
            period = "5y"
        elif yf_interval == "60m":
            period = "60d"
        else:
            period = "30d"

        # Map symbols → yf tickers; track originals
        sym_to_yf = {s: to_yf_ticker(s) for s in symbols}
        yf_to_syms: dict[str, list[str]] = {}  # multiple bot symbols may map to same yf ticker
        for s, yt in sym_to_yf.items():
            yf_to_syms.setdefault(yt, []).append(s)

        unique_yfs = list(yf_to_syms.keys())
        ok = 0
        for i in range(0, len(unique_yfs), batch_size):
            chunk = unique_yfs[i:i + batch_size]
            try:
                raw = yf.download(
                    tickers=" ".join(chunk), period=period, interval=yf_interval,
                    group_by="ticker", auto_adjust=False, prepost=False,
                    threads=False, progress=False,
                )
            except Exception as e:
                log.warning("warmup batch %d-%d failed: %s", i, i + batch_size, e)
                continue
            if raw is None or raw.empty:
                continue

            # Multi-ticker download returns columns (ticker, OHLCV). For 1 ticker,
            # flat columns. Handle both.
            multi_idx = hasattr(raw.columns, "levels") and len(raw.columns.levels) > 1
            for yf_ticker in chunk:
                try:
                    if multi_idx:
                        if yf_ticker not in raw.columns.get_level_values(0):
                            continue
                        sub = raw[yf_ticker].copy()
                    else:
                        sub = raw.copy()
                    sub = sub.reset_index()
                    time_col = "Datetime" if "Datetime" in sub.columns else (
                        "Date" if "Date" in sub.columns else sub.columns[0])
                    sub["time"] = pd.to_datetime(sub[time_col], utc=True)
                    sub = sub[["time", "Open", "High", "Low", "Close", "Volume"]]
                    sub = sub.dropna(subset=["Open", "Close"]).reset_index(drop=True)
                    if sub.empty:
                        continue

                    # If 4h: resample
                    if is_4h:
                        sub = sub.set_index("time")
                        sub = sub.resample("4h", origin="epoch").agg({
                            "Open": "first", "High": "max", "Low": "min",
                            "Close": "last", "Volume": "sum",
                        }).dropna(subset=["Open"]).reset_index()

                    # Cache & save for each bot-symbol mapped to this yf ticker
                    now = time.time()
                    for bot_sym in yf_to_syms.get(yf_ticker, []):
                        key = (yf_ticker, interval)  # cache key is yf_ticker
                        with self._lock:
                            self._mem[key] = (sub, now)
                        if self.disk_cache:
                            self._save_disk(yf_ticker, interval, sub)
                    ok += 1
                except Exception as e:
                    log.debug("warmup parse %s failed: %s", yf_ticker, e)

        log.info("warmup_batch: %s interval=%s — %d/%d ok", "yf", interval, ok, len(unique_yfs))
        return ok


# Module-level singleton (created lazily by callers)
_yf_source: Optional[YFinanceSource] = None


def get_yf_source() -> YFinanceSource:
    global _yf_source
    if _yf_source is None:
        _yf_source = YFinanceSource()
    return _yf_source
