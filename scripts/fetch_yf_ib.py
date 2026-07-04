"""Fetch yfinance bars for IB backtest universe.

Downloads:
  • top-1000 stocks  (from data/ib_universe_top1000.json)
  • 38 FX spot       (mapped to "XXXYYY=X" via bot.data_source.to_yf_ticker)
  • 52 futures       (continuous front-month, "XX=F")
  • ^VIX             (for VIX-sizing backtest)

Resolutions:
  • 1h × ~1y (yfinance free: max 730d at 1h)
  • 4h derived from 1h via resample
  • 1d × 5y

Saves parquet to data/yf_cache_ib/{1h,4h,1d}/<ticker>.parquet
(matches bot/data_source.py YFinanceSource layout — drop-in usable by honest_replay_ib.py).

Threaded (default 16 workers — yfinance is I/O bound).
"""
from __future__ import annotations
import os, sys, json, time, traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
warnings.filterwarnings("ignore")

import pandas as pd

HL_ROOT = Path(os.environ.get("HL_ROOT", str(Path(__file__).parent.parent.parent)))
sys.path.insert(0, str(HL_ROOT / "hyperliquid_bot"))
from bot.data_source import to_yf_ticker  # noqa: E402

CACHE = HL_ROOT / "hyperliquid_bot" / "data" / "yf_cache_ib"
TOP_PATH = HL_ROOT / "hyperliquid_bot" / "data" / "ib_universe_top1000.json"
FULL_PATH = HL_ROOT / "hyperliquid_bot" / "data" / "ib_universe.json"

WORKERS = int(os.environ.get("FETCH_WORKERS", "16"))
PERIOD_1H = os.environ.get("PERIOD_1H", "1y")    # yf free max 730d
PERIOD_1D = os.environ.get("PERIOD_1D", "5y")
SKIP_IF_EXISTS = os.environ.get("SKIP_IF_EXISTS", "1") == "1"
# Only re-fetch syms whose sec_type is in this set (comma-separated). Empty = all.
ONLY_TYPES = set(filter(None, os.environ.get("ONLY_TYPES", "").split(",")))


def load_universe() -> list[tuple[str, str]]:
    """Return list of (sym, type). Top-1000 stocks + 38 FX + 52 futures + ^VIX index."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    top = json.loads(TOP_PATH.read_text())
    for s in top["symbols"]:
        sym = s["sym"]
        if sym not in seen:
            out.append((sym, s["type"]))
            seen.add(sym)
    # add ALL futures from full universe (top1000 reports 0 futures)
    full = json.loads(FULL_PATH.read_text())
    for s in full["symbols"]:
        if s["type"] == "future" and s["sym"] not in seen:
            out.append((s["sym"], "future"))
            seen.add(s["sym"])
    # VIX as index (special; not in universe, fetched directly)
    out.append(("^VIX", "index"))
    return out


def fetch_one(sym: str, sec_type: str) -> tuple[str, str, str]:
    """Fetch 1h+1d for one symbol, derive 4h. Return (sym, status, detail)."""
    import yfinance as yf  # local import — survive thread spawn under Windows
    ticker = to_yf_ticker(sym)
    if not ticker:
        return sym, "skip", "to_yf_ticker empty"

    paths = {
        "1h": CACHE / "1h" / f"{ticker}.parquet",
        "4h": CACHE / "4h" / f"{ticker}.parquet",
        "1d": CACHE / "1d" / f"{ticker}.parquet",
    }
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)
    if SKIP_IF_EXISTS and all(p.exists() and p.stat().st_size > 1000 for p in paths.values()):
        return sym, "skip_cached", "all_present"

    try:
        df_1h = yf.download(
            ticker, period=PERIOD_1H, interval="1h",
            auto_adjust=False, progress=False, threads=False,
        )
        df_1d = yf.download(
            ticker, period=PERIOD_1D, interval="1d",
            auto_adjust=False, progress=False, threads=False,
        )
    except Exception as e:
        tb = traceback.format_exc().splitlines()[-1]
        return sym, "fail", f"yf:{type(e).__name__}:{e} | {tb}"

    if df_1h is None or len(df_1h) == 0:
        return sym, "no_data_1h", f"empty df ({ticker})"

    # Multi-symbol mode (when ticker has multi-index columns).
    # yfinance 1.3 returns MultiIndex even for single ticker; flatten by selecting level-1 == ticker.
    def _flatten(df: pd.DataFrame) -> pd.DataFrame:
        if isinstance(df.columns, pd.MultiIndex):
            try:
                return df.xs(ticker, axis=1, level=1, drop_level=True)
            except KeyError:
                return df.droplevel(0, axis=1)
        return df
    try:
        df_1h = _flatten(df_1h)
        df_1d = _flatten(df_1d)
    except Exception as e:
        tb = traceback.format_exc().splitlines()[-1]
        return sym, "fail", f"flatten:{type(e).__name__}:{e} | {tb}"

    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.rename_axis("time").reset_index()
        # Match bot schema: time, Open, High, Low, Close, Volume
        keep = ["time", "Open", "High", "Low", "Close", "Volume"]
        for c in keep:
            if c not in df.columns:
                df[c] = None
        return df[keep]

    df_1h = _normalize(df_1h)
    df_1d = _normalize(df_1d)

    # 4h derived from 1h
    def _resample_4h(df_1h_norm: pd.DataFrame) -> pd.DataFrame:
        if df_1h_norm.empty:
            return df_1h_norm
        d = df_1h_norm.set_index("time")
        r = d.resample("4h", origin="epoch").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna(subset=["Close"]).reset_index()
        return r

    df_4h = _resample_4h(df_1h)

    df_1h.to_parquet(paths["1h"], index=False)
    df_4h.to_parquet(paths["4h"], index=False)
    df_1d.to_parquet(paths["1d"], index=False)
    return sym, "ok", f"1h={len(df_1h)} 4h={len(df_4h)} 1d={len(df_1d)}"


def main():
    syms = load_universe()
    if ONLY_TYPES:
        before = len(syms)
        syms = [(s, t) for s, t in syms if t in ONLY_TYPES]
        print(f"[fetch_yf_ib] ONLY_TYPES={sorted(ONLY_TYPES)} filtered {before} -> {len(syms)}", flush=True)
    t0 = time.time()
    print(f"[fetch_yf_ib] {len(syms)} symbols  cache={CACHE}  workers={WORKERS}  "
          f"period_1h={PERIOD_1H}  period_1d={PERIOD_1D}  skip_if_exists={SKIP_IF_EXISTS}", flush=True)
    by_status: dict[str, int] = {}
    fails: list[tuple[str, str]] = []

    first_fail_printed = False
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_one, sym, t): sym for sym, t in syms}
        for i, fut in enumerate(as_completed(futs), 1):
            sym = futs[fut]
            try:
                _, status, detail = fut.result()
            except Exception as e:
                tb = traceback.format_exc().splitlines()[-1]
                status, detail = "exception", f"{type(e).__name__}:{e} | {tb}"
            by_status[status] = by_status.get(status, 0) + 1
            if status in ("fail", "no_data_1h", "exception"):
                fails.append((sym, f"{status}:{detail}"))
                if not first_fail_printed:
                    print(f"  FIRST FAIL: {sym}  status={status}  detail={detail}", flush=True)
                    first_fail_printed = True
            if i % 50 == 0 or i == len(syms):
                print(f"  {i}/{len(syms)}  elapsed={time.time()-t0:.0f}s  {dict(sorted(by_status.items()))}", flush=True)

    print(f"\nDone in {time.time()-t0:.1f}s  total={len(syms)}", flush=True)
    print(f"Status: {dict(sorted(by_status.items()))}")
    if fails:
        print(f"\nFails ({len(fails)}):")
        for s, d in fails[:30]:
            print(f"  {s}  {d}")
        if len(fails) > 30:
            print(f"  ... {len(fails)-30} more")


if __name__ == "__main__":
    main()
