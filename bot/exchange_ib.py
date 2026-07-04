"""Interactive Brokers adapter (ib_async wrapper) — mirrors HLClient interface.

Connects to IB Gateway over TCP (default localhost:4002 paper, 4001 live).
Multi-asset: stocks (SMART) + index/commodity/agri/bond/crypto futures (CME/NYMEX/CBOT/ICE).

Symbol namespace (matches data/ib_universe/*.json from yfinance backtest):
  Stocks            → Stock(SYM, "SMART", "USD")
  ES/NQ/YM/RTY      → ContFuture(SYM, "CME")
  CL/BZ/NG/PL/PA    → ContFuture(SYM, "NYMEX")
  GC/SI/HG          → ContFuture(SYM, "COMEX")
  ZB/ZN/ZF/ZT/ZC/ZS/ZW → ContFuture(SYM, "CBOT")
  KC/CT/SB          → ContFuture(SYM, "ICE")
  BTC_FUT/ETH_FUT   → ContFuture(BRR/ETH, "CME")  # CME BTC/ETH index futures

Multiplier: futures имеют contract multiplier (ES=50, NQ=20, CL=1000, GC=100). Бот при
sizing должен умножать notional/risk на multiplier. Для stocks multiplier=1.

Pacing: IB API throttles at ~60 req / 10s. We use ib_insync's built-in throttling
plus min-TTL gate on candles cache (mirror Kraken).

FX rip (2026-05-12): forex spot, FX CFD и FX futures (6E/6J/6B/6A/6C/6S/6N) удалены —
бот не торгует валютами ни в каком виде. См. data/manual_ops/2026-05-12_ib_rip_fx_shorts/.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import pandas as pd

from bot.config import Settings, TF_MS

log = logging.getLogger(__name__)

# Lazy import to keep bot startable even if ib_async absent (for non-IB bots).
# ib_async is the maintained fork of ib_insync; API surface identical.
_ib_mod = None
def _import_ib():
    global _ib_mod
    if _ib_mod is None:
        try:
            import ib_async as _m  # type: ignore
        except ImportError:
            import ib_insync as _m  # type: ignore  # legacy fallback
        _ib_mod = _m
    return _ib_mod


@dataclass(frozen=True)
class AssetMeta:
    name: str
    sz_decimals: int
    max_leverage: int
    multiplier: float = 1.0       # contract size — 1 for stocks, 50 for ES, etc.
    min_tick: float = 0.01
    exchange: str = "SMART"       # routing exchange (SMART for stocks, CME/NYMEX/... for futures)
    sec_type: str = "STK"         # IB security type: STK / FUT / CRYPTO


# ---------------------------------------------------------------------------
# Symbol → Contract mapping
# ---------------------------------------------------------------------------

_STOCKS = {
    "AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","AMD","INTC","MU",
    "AVGO","QCOM","ORCL","ADBE","CRM","NFLX","COST","WMT","JPM","BAC",
    "GS","JNJ","UNH","V","MA","XOM","CVX","LLY","ABBV","MRK",
    "PFE","DIS","NKE","HD","MCD","KO","PG",
}
_INDEX_FUT_CME = {"ES","NQ","YM","RTY"}
_COMMOD_FUT_NYMEX = {"CL","BZ","NG","PL","PA"}
_COMMOD_FUT_COMEX = {"GC","SI","HG"}
_BOND_FUT_CBOT = {"ZB","ZN","ZF","ZT"}
_AGRI_FUT_CBOT = {"ZC","ZS","ZW"}
_AGRI_FUT_ICE = {"KC","CT","SB"}
# Crypto BRR (Bitcoin Reference Rate) on CME — full-size BTC, MBT micro
_CRYPTO_FUT_MAP = {"BTC_FUT":"MBT", "ETH_FUT":"MET"}  # default to micros for size flexibility

# Contract multipliers (per IB Contract.multiplier as float)
_MULTIPLIERS = {
    # Stocks: 1
    # Index futures (E-mini)
    "ES": 50.0, "NQ": 20.0, "YM": 5.0, "RTY": 50.0,
    # Energy (NYMEX)
    "CL": 1000.0, "BZ": 1000.0, "NG": 10000.0,
    # Metals (NYMEX/COMEX)
    "GC": 100.0, "SI": 5000.0, "HG": 25000.0, "PL": 50.0, "PA": 100.0,
    # Bonds (CBOT) — face value / 100 × tick
    "ZB": 1000.0, "ZN": 1000.0, "ZF": 1000.0, "ZT": 2000.0,
    # Agri
    "ZC": 50.0, "ZS": 50.0, "ZW": 50.0,
    "KC": 37500.0, "CT": 50000.0, "SB": 112000.0,
    # Crypto micros
    "MBT": 0.1,  # MBT = 0.1 BTC
    "MET": 0.1,  # MET = 0.1 ETH
}


# Micro / mini futures multipliers (extended set used in auto-universe)
_MULTIPLIERS.update({
    "MES": 5.0, "MNQ": 2.0, "MYM": 0.5, "M2K": 5.0, "EMD": 100.0,
    "MCL": 100.0, "MNG": 2500.0,
    "MGC": 10.0, "SIL": 1000.0, "MHG": 2500.0,
    "UB": 1000.0, "TN": 1000.0,
    "ZR": 2000.0, "ZO": 50.0,
    "LE": 400.0, "GF": 500.0, "HE": 400.0,
    "OJ": 15000.0,
})

# Extended futures-by-exchange routing for build_ib_universe output.
# IMPORTANT — these are the *primary listing exchange* per IBKR contract-search:
#   - YM/MYM (Dow E-mini + Micro) trade on **CBOT**, not CME (common mistake)
#   - EMD (E-mini S&P MidCap 400) on CME (correct)
#   - MNG (Micro Henry Hub Natural Gas) on NYMEX (correct, but IBKR routes via "NYMEX" not "NYMEX_NTM")
#   - SIL (E-mini Silver) on COMEX, ContFuture qualified as 1000oz
_FUT_EXCH = {
    # CME equity-index (full + micro)
    "ES": "CME", "NQ": "CME", "RTY": "CME", "EMD": "CME",
    "MES": "CME", "MNQ": "CME", "M2K": "CME",
    # CBOT Dow (full + micro) — NOT CME
    "YM": "CBOT", "MYM": "CBOT",
    # NYMEX energy
    "CL": "NYMEX", "BZ": "NYMEX", "NG": "NYMEX", "RB": "NYMEX", "HO": "NYMEX",
    "MCL": "NYMEX", "MNG": "NYMEX",
    # COMEX/NYMEX metals
    "GC": "COMEX", "SI": "COMEX", "HG": "COMEX", "PL": "NYMEX", "PA": "NYMEX",
    "MGC": "COMEX", "SIL": "COMEX", "MHG": "COMEX",
    # CBOT bonds/rates
    "ZN": "CBOT", "ZB": "CBOT", "ZF": "CBOT", "ZT": "CBOT", "UB": "CBOT", "TN": "CBOT",
    # CBOT grains
    "ZC": "CBOT", "ZS": "CBOT", "ZW": "CBOT", "ZL": "CBOT", "ZM": "CBOT", "ZR": "CBOT", "ZO": "CBOT",
    # ICE softs
    "KC": "NYBOT", "CT": "NYBOT", "SB": "NYBOT", "CC": "NYBOT", "OJ": "NYBOT",
    # CME livestock
    "LE": "CME", "GF": "CME", "HE": "CME",
}


# Universe type hint cache — IBClient.__init__ loads from data/ib_universe.json.
# When set, takes priority over symbol-based routing.
# Solves ticker collisions: "ES" = Eversource Energy stock OR E-mini S&P futures;
# universe says which one is intended.
_TYPE_HINTS: dict[str, str] = {}


def _load_type_hints() -> dict[str, str]:
    """Read symbol → type mapping from data/ib_universe.json. Returns {} if missing."""
    try:
        import json
        from pathlib import Path
        p = Path("data/ib_universe.json")
        if not p.exists():
            return {}
        data = json.loads(p.read_text())
        return {x["sym"]: x.get("type", "") for x in data.get("symbols", [])}
    except Exception:
        return {}


def _make_contract(coin: str):
    """Map our coin namespace -> IB Contract. Returns (contract, sec_type, exch, multiplier).

    Routing priority:
      0. Universe type hint (data/ib_universe.json) — resolves stock/future collisions
      1. CME-group futures (predefined roster)
      2. Default: Stock(sym, "SMART", "USD")

    FX rip 2026-05-12: forex spot, FX CFD и FX futures (6E/6J/6B/6A/...) удалены.
    """
    ib = _import_ib()
    sym = coin

    # 0) Universe-type override (when universe says "ES is a stock", honor that
    #    even though _FUT_EXCH would route to futures — Eversource Energy stock
    #    vs E-mini S&P 500 futures is a real disambiguation case).
    hint = _TYPE_HINTS.get(sym)
    if hint in ("stock", "etf"):
        return ib.Stock(sym, "SMART", "USD"), "STK", "SMART", 1.0
    if hint == "future":
        if sym in _FUT_EXCH:
            exch = _FUT_EXCH[sym]
            return ib.ContFuture(sym, exch), "FUT", exch, _MULTIPLIERS.get(sym, 1.0)
        # Fall through to legacy futures mapping

    # 1) Futures — explicit by-exchange routing (when no hint, or hint=future)
    if sym in _FUT_EXCH:
        exch = _FUT_EXCH[sym]
        return ib.ContFuture(sym, exch), "FUT", exch, _MULTIPLIERS.get(sym, 1.0)

    if sym in _CRYPTO_FUT_MAP:
        ib_sym = _CRYPTO_FUT_MAP[sym]
        return ib.ContFuture(ib_sym, "CME"), "FUT", "CME", _MULTIPLIERS.get(ib_sym, 1.0)

    # 2) Default: Stock(sym, "SMART", "USD") — handles all US-listed equities/ETFs
    return ib.Stock(sym, "SMART", "USD"), "STK", "SMART", 1.0


# ---------------------------------------------------------------------------
# IB TF mapping
# ---------------------------------------------------------------------------

# IB barSize strings for reqHistoricalData
_TF_TO_IB = {
    "1m": "1 min", "5m": "5 mins", "15m": "15 mins", "30m": "30 mins",
    "1h": "1 hour", "2h": "2 hours", "4h": "4 hours",
    "1d": "1 day", "1w": "1 week",
}

# IB durationStr — how far back to ask. Match limit*bar_seconds.
def _duration_for(interval: str, limit: int) -> str:
    bar_ms = TF_MS.get(interval, 3600_000)
    seconds = limit * bar_ms / 1000
    days = max(1, int(seconds / 86400) + 1)
    if days <= 365:
        return f"{days} D"
    years = max(1, int(days / 365) + 1)
    return f"{years} Y"


# ---------------------------------------------------------------------------
# IBClient
# ---------------------------------------------------------------------------

CANDLES_LIMIT = 200


class IBClient:
    """ib_insync-based adapter mirroring HLClient interface."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        ib = _import_ib()

        # Load universe-type hints (stocks vs futures) from JSON; needed for
        # ticker-collision disambiguation in _make_contract (ES = stock OR future).
        global _TYPE_HINTS
        _TYPE_HINTS = _load_type_hints()
        log.info("loaded %d universe-type hints", len(_TYPE_HINTS))

        self.host = os.getenv("IB_GATEWAY_HOST", "127.0.0.1")
        self.port = int(os.getenv("IB_GATEWAY_PORT", "4002"))  # 4002 paper / 4001 live
        self.client_id = int(os.getenv("IB_CLIENT_ID", "1"))
        self.account = os.getenv("IB_ACCOUNT", "")  # account code, blank = first

        self.ib = ib.IB()
        self._connect()

        # Caches (mirror Kraken bar-aligned cache)
        self._candles_cache: dict[tuple[str, str], tuple[int, pd.DataFrame, float]] = {}
        self._cache_lock = threading.Lock()
        self._meta_cache: dict[str, AssetMeta] = {}
        self._mark_cache: dict[str, tuple[float, float]] = {}  # coin -> (price, ts)
        self._positions_cache: tuple[float, dict[str, dict]] | None = None
        self._account_summary_cache: tuple[float, dict] | None = None
        # IB STK tier-1: $0.005/share + SEC 0.00229%/FINRA TAF.
        # Round-trip on $50 stock ≈ 0.02% — bot's default 0.10% inflates drag_R 5×.
        # Env-tunable via IB_FEE_RT_ESTIMATE (default 0.0002 = 2bps).
        self._fee_rt_estimate = float(os.getenv("IB_FEE_RT_ESTIMATE", "0.0002"))

    def _connect(self) -> None:
        log.info("IB connect: %s:%s clientId=%s", self.host, self.port, self.client_id)
        self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=15)
        if self.ib.isConnected():
            log.info("IB connected. Server version=%s.", self.ib.client.serverVersion())
            try:
                # Account lacks live market-data subscription -> Error 10089 spam.
                # 3 = DELAYED, suppresses subscription-required errors.
                self.ib.reqMarketDataType(int(os.getenv("IB_MARKET_DATA_TYPE", "3")))
            except Exception as e:
                log.warning("reqMarketDataType failed: %s", e)
        else:
            raise RuntimeError(f"IB Gateway connect failed at {self.host}:{self.port}")

    def _ensure_connected(self) -> None:
        if not self.ib.isConnected():
            log.warning("IB connection lost — reconnecting")
            self._connect()

    # ----- universe -----

    def available_coins(self) -> list[str]:
        """Load coin universe from data/ib_universe.json (built by scripts/build_ib_universe.py).

        Returns list of symbols, crypto-related already filtered out. Bot iterates this
        list and applies LIQUIDITY_MIN_4H_USD live filter per-coin during scan.
        """
        import json
        from pathlib import Path
        coins_file = os.getenv("COINS_FILE", "data/ib_universe.json")
        p = Path(coins_file)
        if not p.exists():
            log.warning("COINS_FILE %s not found — returning empty universe; run scripts/build_ib_universe.py first", coins_file)
            return []
        try:
            data = json.loads(p.read_text())
            symbols = [x["sym"] for x in data.get("symbols", [])]
            log.info("loaded %d symbols from %s", len(symbols), coins_file)
            return symbols
        except Exception as e:
            log.error("failed to load %s: %s", coins_file, e)
            return []

    # ----- meta / asset -----

    def get_meta(self, force: bool = False) -> dict[str, AssetMeta]:
        # Building meta lazily — IB returns ContractDetails on demand only
        return self._meta_cache

    def asset(self, coin: str) -> AssetMeta:
        m = self._meta_cache.get(coin)
        if m is not None:
            return m
        contract, sec_type, exch, mult = _make_contract(coin)
        # Qualify to fill conId/details
        try:
            self.ib.qualifyContracts(contract)
        except Exception as e:
            log.warning("qualifyContracts(%s) failed: %s", coin, e)
        # min tick — try ContractDetails
        min_tick = 0.01
        try:
            details = self.ib.reqContractDetails(contract)
            if details:
                min_tick = float(details[0].minTick or 0.01)
        except Exception:
            pass
        # max_leverage: stocks 4 (Reg-T intraday), futures via marginRequirement
        # (placeholder — real value via reqMarginRequirement; for now safe defaults)
        max_lev = 4 if sec_type == "STK" else 10
        m = AssetMeta(
            name=coin, sz_decimals=0 if sec_type == "FUT" else 2,
            max_leverage=max_lev, multiplier=mult, min_tick=min_tick,
            exchange=exch, sec_type=sec_type,
        )
        self._meta_cache[coin] = m
        return m

    # ----- candles -----

    @staticmethod
    def _cache_offset_ms(coin: str, interval: str, bar_ms: int) -> int:
        import hashlib
        raw = int(hashlib.md5(f"{coin}|{interval}".encode()).hexdigest()[:8], 16)
        cap_ms = min(30_000, bar_ms // 2)
        return raw % max(1, cap_ms) if cap_ms > 0 else 0

    def candles(self, coin: str, interval: str, limit: int = CANDLES_LIMIT) -> pd.DataFrame:
        """Fetch OHLCV bars. Routes through yfinance to preserve IBKR pacing for execution.

        Flow:
          1. Bot's in-memory cache (TTL = bar_ms)
          2. yfinance (with its own disk+mem cache in data_source.py)
          3. IBKR reqHistoricalData (fallback if yfinance fails)
        """
        bar_ms = TF_MS.get(interval)
        if bar_ms is None:
            return self._fetch_candles_direct(coin, interval, limit)

        now_ms = int(time.time() * 1000)
        offset_ms = self._cache_offset_ms(coin, interval, bar_ms)
        effective_now_ms = now_ms - offset_ms
        current_bar_start = (effective_now_ms // bar_ms) * bar_ms
        cache_key = (coin, interval)

        with self._cache_lock:
            cached = self._candles_cache.get(cache_key)
        if cached is not None and cached[0] >= current_bar_start:
            return cached[1].copy()
        if cached is not None:
            min_ttl = float(os.getenv("CANDLES_MIN_TTL_SEC", "30"))
            if min_ttl > 0 and (now_ms / 1000.0 - cached[2]) < min_ttl:
                return cached[1].copy()

        # 1) Try yfinance first (heavy lifting, doesn't hit IBKR pacing)
        df = self._fetch_yfinance(coin, interval, limit)

        # 2) IBKR fallback DISABLED by default — preserves pacing budget for execution.
        #    Symbols yf can't fetch are simply skipped this cycle (return empty df).
        #    Set IB_BARS_FALLBACK=true ONLY for diagnostic; production stays off.
        if df.empty and os.getenv("IB_BARS_FALLBACK", "false").lower() in ("true", "1", "yes"):
            log.debug("yf empty for %s %s → IBKR fallback (IB_BARS_FALLBACK=true)", coin, interval)
            self._ensure_connected()
            df = self._fetch_candles_direct(coin, interval, limit)

        if df.empty:
            return df
        last_bar_t_ms = current_bar_start
        try:
            last_bar_t_ms = int(pd.Timestamp(df["time"].iloc[-1]).value // 10**6)
        except Exception:
            pass
        with self._cache_lock:
            self._candles_cache[cache_key] = (last_bar_t_ms, df, time.time())
        return df

    def _fetch_yfinance(self, coin: str, interval: str, limit: int) -> pd.DataFrame:
        """Fetch bars via yfinance (no IBKR pacing consumption)."""
        try:
            from bot.data_source import get_yf_source
            return get_yf_source().candles(coin, interval, limit)
        except Exception as e:
            log.debug("yfinance fetch %s %s failed: %s", coin, interval, e)
            return pd.DataFrame(columns=["time","Open","High","Low","Close","Volume"])

    def _fetch_candles_direct(self, coin: str, interval: str, limit: int) -> pd.DataFrame:
        contract, _, _, _ = _make_contract(coin)
        self.ib.qualifyContracts(contract)
        ib_tf = _TF_TO_IB.get(interval)
        if ib_tf is None:
            log.warning("IB unsupported TF %s — returning empty", interval)
            return pd.DataFrame(columns=["time","Open","High","Low","Close","Volume"])
        duration = _duration_for(interval, limit)
        try:
            bars = self.ib.reqHistoricalData(
                contract, endDateTime="", durationStr=duration, barSizeSetting=ib_tf,
                whatToShow="TRADES", useRTH=False, formatDate=2,  # 2 = epoch seconds
                keepUpToDate=False,
            )
        except Exception as e:
            log.warning("reqHistoricalData(%s, %s) failed: %s", coin, interval, e)
            return pd.DataFrame(columns=["time","Open","High","Low","Close","Volume"])
        if not bars:
            return pd.DataFrame(columns=["time","Open","High","Low","Close","Volume"])
        rows = []
        for b in bars:
            # b.date is datetime when formatDate=2 still returns datetime in ib_insync
            ts = pd.Timestamp(b.date, tz="UTC") if not isinstance(b.date, pd.Timestamp) else b.date
            rows.append([ts, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume or 0)])
        df = pd.DataFrame(rows, columns=["time","Open","High","Low","Close","Volume"])
        return df.tail(limit).reset_index(drop=True)

    def last_4h_volume_usd(self, coin: str) -> float | None:
        with self._cache_lock:
            cached = self._candles_cache.get((coin, "4h"))
        if cached is None:
            return None
        df = cached[1]
        if df.empty:
            return None
        try:
            vol = float(df["Volume"].iloc[-1])
            px = float(df["Close"].iloc[-1])
        except (TypeError, ValueError):
            return None
        if vol <= 0 or px <= 0:
            return None
        # For futures: volume is contracts; multiply by px * multiplier for USD notional
        meta = self.asset(coin)
        return vol * px * (meta.multiplier if meta.sec_type == "FUT" else 1.0)

    # ----- account / positions -----

    def funding_rate(self, coin: str) -> float:
        # IB futures: no funding, only basis (ignored for short holds).
        return 0.0

    def _account_summary(self, ttl: float = 1.0) -> dict:
        now = time.time()
        if self._account_summary_cache and (now - self._account_summary_cache[0]) < ttl:
            return self._account_summary_cache[1]
        self._ensure_connected()
        rows = self.ib.accountSummary(account=self.account or "")
        out: dict[str, dict] = {}
        for r in rows:
            out.setdefault(r.account, {})[r.tag] = {"value": r.value, "currency": r.currency}
        self._account_summary_cache = (now, out)
        return out

    def account_value(self) -> float:
        s = self._account_summary()
        if not s:
            return 0.0
        # Use NetLiquidation tag as account total. Pick first account if IB_ACCOUNT not set.
        acct_key = self.account or next(iter(s.keys()))
        v = s.get(acct_key, {}).get("NetLiquidation", {}).get("value")
        return float(v) if v else 0.0

    def spot_usdc(self) -> float:
        s = self._account_summary()
        if not s:
            return 0.0
        acct_key = self.account or next(iter(s.keys()))
        v = s.get(acct_key, {}).get("CashBalance", {}).get("value")
        return float(v) if v else 0.0

    def open_positions(self, ttl: float = 10.0) -> dict[str, dict]:
        now = time.time()
        if self._positions_cache and (now - self._positions_cache[0]) < ttl:
            return self._positions_cache[1]
        self._ensure_connected()
        positions = self.ib.positions(account=self.account or "")
        # Reverse-map IB symbol -> our coin
        ib_sym_to_coin = {}
        for c in _STOCKS:
            ib_sym_to_coin[c] = c
        for c in _INDEX_FUT_CME | _COMMOD_FUT_NYMEX | _COMMOD_FUT_COMEX | _BOND_FUT_CBOT | _AGRI_FUT_CBOT | _AGRI_FUT_ICE:
            ib_sym_to_coin[c] = c
        for our, ib_s in _CRYPTO_FUT_MAP.items():
            ib_sym_to_coin[ib_s] = our

        out: dict[str, dict] = {}
        for p in positions:
            ib_sym = p.contract.symbol
            coin = ib_sym_to_coin.get(ib_sym, ib_sym)
            sz = float(p.position)
            if sz == 0:
                continue
            entry = float(p.avgCost) / float(p.contract.multiplier or 1)
            out[coin] = {
                "coin": coin,
                "size": abs(sz),
                "side": "long" if sz > 0 else "short",
                "entry_px": entry,
                "_raw": p,
            }
        self._positions_cache = (now, out)
        return out

    def invalidate_positions_cache(self) -> None:
        self._positions_cache = None
        self._account_summary_cache = None

    def position_liquidation(self, coin: str) -> dict | None:
        # IB doesn't expose per-position liq price directly; margin requirements
        # vary. For our use case (Reg-T stocks intraday + futures with own margin),
        # liq simulation is bot-side. Return None — caller falls back to no-liq guard.
        return None

    def user_fills(self, ttl_sec: float = 60.0) -> list[dict]:
        self._ensure_connected()
        fills = self.ib.fills()
        out = []
        for f in fills:
            out.append({
                "coin": f.contract.symbol,
                "px": float(f.execution.price),
                "sz": float(f.execution.shares),
                "side": "buy" if f.execution.side == "BOT" else "sell",
                "time": f.execution.time,
                "_raw": f,
            })
        return out

    # ----- pricing -----

    def slip_per_side(self, coin: str, notional_usd: float) -> float:
        # Approximate slippage from IB-observed spread. For deep stocks/futures
        # the realized slippage is ~0.005-0.05% per side; we conservatively return
        # 0.05% (= 0.0005). Bot uses this only for liquidity drag check.
        return float(os.getenv("IB_DEFAULT_SLIP", "0.0005"))

    def mark_price(self, coin: str, ttl: float = 5.0) -> float:
        now = time.time()
        c = self._mark_cache.get(coin)
        if c and (now - c[1]) < ttl:
            return c[0]
        self._ensure_connected()
        contract, _, _, _ = _make_contract(coin)
        self.ib.qualifyContracts(contract)
        ticker = self.ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
        # ib_insync auto-updates ticker; wait briefly for fill
        for _ in range(20):
            self.ib.sleep(0.1)
            if ticker.last == ticker.last and ticker.last:  # not NaN, not zero
                break
            if ticker.marketPrice() == ticker.marketPrice():
                break
        px = ticker.marketPrice() if ticker.marketPrice() == ticker.marketPrice() else ticker.last
        if not px or px != px:
            # Fallback to last close from candles cache
            df = self.candles(coin, "1h", 5)
            px = float(df["Close"].iloc[-1]) if not df.empty else 0.0
        self._mark_cache[coin] = (float(px), now)
        try:
            self.ib.cancelMktData(contract)
        except Exception:
            pass
        return float(px)

    def round_price(self, coin: str, px: float) -> float:
        meta = self.asset(coin)
        tick = meta.min_tick or 0.01
        return round(px / tick) * tick

    # ----- order placement -----

    def update_leverage(self, coin: str, leverage: int, is_cross: bool = True) -> dict | None:
        # IB doesn't expose per-coin leverage knob (margin = exchange/account level).
        # Stocks: Reg-T = 4x intraday / 2x overnight (account-level). Futures:
        # exchange initial margin (per-contract, fixed). No-op here.
        return {"status": "ok", "note": "IB margin = account/exchange level, no per-coin lever"}

    def _floor_size_for_ib(self, coin: str, sz: float) -> tuple[float, str | None]:
        """IB API rejects fractional for STK (10243). Floor to int.
        Returns (final_size, error_reason_or_None)."""
        abs_sz = abs(float(sz))
        try:
            sec = self.asset(coin).sec_type
        except Exception:
            sec = None
        if sec in ("STK", "FUT"):
            final = float(int(abs_sz))  # truncate fractional
            if final < 1:
                return 0.0, f"size {abs_sz:.4f} {sec} floors to 0 (< 1 unit min)"
            return final, None
        return abs_sz, None

    def market_open(self, coin: str, is_buy: bool, sz: float) -> dict:
        self._ensure_connected()
        ib = _import_ib()
        contract, _, _, _ = _make_contract(coin)
        self.ib.qualifyContracts(contract)
        sz_final, err = self._floor_size_for_ib(coin, sz)
        if err:
            return self._error_resp(err, status="SizeTooSmall")
        action = "BUY" if is_buy else "SELL"
        order = ib.MarketOrder(action, sz_final)
        if self.account:
            order.account = self.account
        trade = self.ib.placeOrder(contract, order)
        # Wait briefly for fill ack
        for _ in range(50):
            self.ib.sleep(0.1)
            if trade.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                break
        return self._wrap_order_resp(trade)

    def market_close(self, coin: str) -> dict:
        positions = self.open_positions(ttl=0.0)
        p = positions.get(coin)
        if not p:
            return {"status": "ok", "note": "no open position"}
        is_buy = (p["side"] == "short")  # close = opposite
        return self.market_open(coin, is_buy, p["size"])

    def trigger_sl(self, coin: str, is_buy: bool, sz: float, trigger_px: float) -> dict:
        # is_buy here means "SL order will be a BUY" → close short. For closing long
        # SL is SELL trigger (is_buy=False from caller).
        self._ensure_connected()
        ib = _import_ib()
        contract, _, _, _ = _make_contract(coin)
        self.ib.qualifyContracts(contract)
        sz_final, err = self._floor_size_for_ib(coin, sz)
        if err:
            return self._error_resp(err, status="SizeTooSmall")
        action = "BUY" if is_buy else "SELL"
        order = ib.StopOrder(action, sz_final, self.round_price(coin, trigger_px))
        order.tif = "GTC"  # SL must survive EOD (default DAY → naked overnight)
        if self.account:
            order.account = self.account
        trade = self.ib.placeOrder(contract, order)
        return self._wrap_order_resp(trade)

    def trigger_tp(self, coin: str, is_buy: bool, sz: float, trigger_px: float) -> dict:
        self._ensure_connected()
        ib = _import_ib()
        contract, _, _, _ = _make_contract(coin)
        self.ib.qualifyContracts(contract)
        sz_final, err = self._floor_size_for_ib(coin, sz)
        if err:
            return self._error_resp(err, status="SizeTooSmall")
        action = "BUY" if is_buy else "SELL"
        order = ib.LimitOrder(action, sz_final, self.round_price(coin, trigger_px))
        order.tif = "GTC"  # TP must survive EOD (default DAY → re-place needed each day)
        if self.account:
            order.account = self.account
        trade = self.ib.placeOrder(contract, order)
        return self._wrap_order_resp(trade)

    def cancel_order(self, coin: str, oid) -> dict:
        self._ensure_connected()
        for tr in self.ib.trades():
            if tr.order.orderId == int(oid):
                self.ib.cancelOrder(tr.order)
                return {"status": "ok", "oid": oid}
        return {"status": "error", "message": f"oid {oid} not found"}

    def cancel_sl_order(self, coin: str, oid) -> dict:
        return self.cancel_order(coin, oid)

    def list_open_sl_orders(self, coin: str) -> list[str]:
        self._ensure_connected()
        out = []
        for tr in self.ib.openTrades():
            if tr.contract.symbol == coin and tr.order.orderType == "STP":
                out.append(str(tr.order.orderId))
        return out

    def _wrap_order_resp(self, trade) -> dict:
        st = trade.orderStatus.status
        # Cancelled/Inactive must NOT be reported as "resting" — caller treats
        # resting as success-in-progress. Phantom-trade root cause 2026-05-11.
        if st in ("Cancelled", "ApiCancelled", "Inactive", "ApiPending"):
            err_msg = ""
            try:
                err_msg = (trade.log[-1].message or "") if trade.log else ""
            except Exception:
                pass
            return {
                "status": "error",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{
                        "error": f"order {st}: {err_msg}",
                        "oid": str(trade.order.orderId),
                    }]}
                },
                "_status": st,
            }
        ok = st in ("Filled", "Submitted", "PreSubmitted", "PendingSubmit")
        return {
            "status": "ok" if ok else "error",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [{
                        "filled" if st == "Filled" else "resting": {
                            "oid": str(trade.order.orderId),
                            "totalSz": str(trade.orderStatus.filled or trade.order.totalQuantity),
                            "avgPx": str(trade.orderStatus.avgFillPrice or 0),
                        }
                    }]
                }
            },
            "_status": st,
        }

    def _error_resp(self, message: str, status: str = "Error") -> dict:
        return {
            "status": "error",
            "response": {
                "type": "order",
                "data": {"statuses": [{"error": message}]},
            },
            "_status": status,
        }

    # HL-compat surface. `client.info.user_state(...)` reaches user_state below
    # via property → self routing.

    @property
    def info(self):
        return self

    def user_state(self, address: str = "") -> dict:
        """HL-compat shim. Builds {crossMaintenanceMarginUsed, assetPositions}
        from IB accountSummary. Used by trader.py MM cap check and main.py
        maintenance-margin alert."""
        try:
            summary = self.ib.accountSummary(self.account)
            mm = 0.0
            for av in summary:
                if av.tag == "MaintMarginReq":
                    try:
                        mm = float(av.value)
                    except (TypeError, ValueError):
                        pass
                    break
            return {"crossMaintenanceMarginUsed": str(mm), "assetPositions": []}
        except Exception as e:
            log.debug("IB user_state failed: %s", e)
            return {"crossMaintenanceMarginUsed": "0", "assetPositions": []}

    def spot_user_state(self, address: str = "") -> dict:
        return {}

    def compute_realized_pnl(
        self,
        fills,
        coin: str,
        direction: str,
        size: float,
        trade_open_iso: str | None = None,
    ):
        # IB-specific realized PnL: match close-side fills by coin/direction/time
        # and derive exit_px (weighted avg) + realizedPNL from commissionReport.
        # Returns (pnl_dollars_or_None, avg_exit_px_or_None).
        if not fills:
            return None, None
        open_ms = 0
        if trade_open_iso:
            try:
                from datetime import datetime as _dt, timezone as _tz
                _t = _dt.fromisoformat(str(trade_open_iso).replace("Z", "+00:00"))
                if _t.tzinfo is None:
                    _t = _t.replace(tzinfo=_tz.utc)
                open_ms = int(_t.timestamp() * 1000) - 60_000
            except Exception:
                open_ms = 0
        expected_side = "sell" if direction == "long" else "buy"
        matching = []
        for f in fills:
            if f.get("coin") != coin:
                continue
            if f.get("side") != expected_side:
                continue
            t_val = f.get("time")
            t_ms = 0
            try:
                if hasattr(t_val, "timestamp"):
                    t_ms = int(t_val.timestamp() * 1000)
                elif isinstance(t_val, (int, float)):
                    t_ms = int(t_val) if t_val > 1e11 else int(t_val * 1000)
            except Exception:
                t_ms = 0
            if open_ms and t_ms and t_ms < open_ms:
                continue
            matching.append(f)
        if not matching:
            return None, None
        total_sz = 0.0
        weighted = 0.0
        realized_sum = 0.0
        has_realized = False
        for f in matching:
            try:
                sz = float(f.get("sz", 0))
                px = float(f.get("px", 0))
            except (TypeError, ValueError):
                continue
            total_sz += sz
            weighted += px * sz
            raw = f.get("_raw")
            if raw is not None:
                cr = getattr(raw, "commissionReport", None)
                if cr is not None:
                    rp = getattr(cr, "realizedPNL", None)
                    if rp is not None and rp == rp:
                        try:
                            realized_sum += float(rp)
                            has_realized = True
                        except (TypeError, ValueError):
                            pass
        avg_px = (weighted / total_sz) if total_sz > 0 else None
        pnl = realized_sum if has_realized else None
        return pnl, avg_px
