"""IB honest replay — yfinance parquet, multi-sec-type leverage, VIX-series.

Adapted from honest_replay_kf.py 2026-05-12. Differences:
  • DATA: parquet at data/yf_cache_ib/{1h,4h,1d}/<yf_ticker>.parquet
          (built by scripts/fetch_yf_ib.py — yfinance dump 1y@1h, 5y@1d)
  • UNIVERSE: top-1000 stocks + 38 FX + 52 futures (~1090 instruments)
  • LEVERAGE: fixed per sec_type
      stock  ->2x   (IB Reg T)
      future ->15x  (avg CME initial-margin ratio)
      fx     ->30x  (CFD ESMA cap)
  • FEES:    stock 2 bps RT, future/fx 1 bps RT
  • VIX:     daily ^VIX series ->multiplies risk per trade if enabled
  • LONG-ONLY: stocks long-only; FX bidir (matches LONG_ONLY_EXEMPT_SEC_TYPES=CFD)
  • NO funding / HIP-3 / KF-adaptive-leverage / BTC-regime gates
  • Session-gate (STK RTH cutoff, FUT 23/5, FX 24/5) — SKIP in v1 (note in output)

Env override knobs the sweep uses:
  IB_DATA_DIR=yf_cache_ib  IB_OUT_PATH=data/honest_replay/ib.json  ACCOUNT_USD=240000
  BT_RISK=0.01  MAX_MARGIN_USED_PCT=0.50  STRUCT_BUFFER_PCT=0.003
  ATR_REGIME_THRESHOLD=0.7  MIN_RR=1.3  MIN_RR_COUNTERTREND=1.1
  VIX_SIZING_ENABLED=false  VIX_HIGH_THRESHOLD=25  VIX_LOW_THRESHOLD=14
  VIX_SIZE_HIGH=0.5  VIX_SIZE_LOW=1.5
  IB_WORKERS=N   SMOKE_COINS=AAPL,MSFT,EUR.USD  IB_START_TS=2025-05-12 IB_END_TS=2026-05-12
"""
from __future__ import annotations
import warnings; warnings.filterwarnings("ignore")
import os, sys, json, time, subprocess
from pathlib import Path
from collections import defaultdict

HL_ROOT = Path(os.environ.get("HL_ROOT", str(Path(__file__).parent.parent.parent)))
DATA_DIR = HL_ROOT / "hyperliquid_bot" / "data"

sys.path.insert(0, str(HL_ROOT / "scripts"))         # _live_env.py at HL/scripts/
sys.path.insert(0, str(HL_ROOT / "hyperliquid_bot")) # bot/ package (local Mac copy == live VPS via earlier rsync)
# NOTE 2026-05-12: activate_live_bot("ib") had rmtree race under Windows mp.spawn
# (Pool re-imports module per worker -> concurrent rmtree of /tmp/live_bots/ib).
# Removed; we trust local hyperliquid_bot/bot/ == live VPS for komp runs.
from _live_env import load_live_env, load_env_file  # noqa: E402

# IMPORTANT: capture parent env overrides BEFORE load_live_env (which uses override=True
# and would silently clobber any sweep settings the wrapper passed in).
_SWEEP_KEYS = (
    "ATR_REGIME_THRESHOLD", "VIX_SIZING_ENABLED", "VIX_HIGH_THRESHOLD", "VIX_LOW_THRESHOLD",
    "VIX_SIZE_HIGH", "VIX_SIZE_LOW", "STRUCT_BUFFER_PCT", "MIN_RR", "MIN_RR_COUNTERTREND",
    "BT_RISK", "RISK_PER_TRADE", "MAX_MARGIN_USED_PCT", "MIN_SL_DIST_PCT",
    "LIQUIDITY_RISK_MULT_HIGH", "LIQUIDITY_RISK_MULT_MID", "KF_COLD_COMBO_MIN_N",
    "ACCOUNT_USD", "IB_WORKERS", "IB_OUT_PATH", "SMOKE_COINS",
    "IB_START_TS", "IB_END_TS", "IB_DATA_DIR", "IB_DUMP_TRADES",
    "PYRAMID_ENABLED", "PYRAMID_TRIGGER_R", "MAX_PYRAMID_LEVELS",
    "EXIT_MODE", "LONG_ONLY", "LONG_ONLY_EXEMPT_SEC_TYPES",
    "FRESHNESS_DEDUPE_TTL_SEC", "BTC_REGIME_TICKER",
)
_PARENT_OVERRIDES = {k: os.environ[k] for k in _SWEEP_KEYS if k in os.environ}

ENV_FILE = os.environ.get("IB_ENV_FILE", "").strip()
if ENV_FILE:
    load_env_file(ENV_FILE)
    ENV_SOURCE = f"file:{ENV_FILE}"
else:
    try:
        load_live_env("ib")

# 2026-05-17: overlay honest-backtest constants on live env (aborts if missing).
# Per memory feedback_live_env_vs_backtest_overrides.md.
from _honest_overrides import apply_honest_overrides  # noqa: E402
_HONEST_APPLIED = apply_honest_overrides()

        ENV_SOURCE = "live:ib-bot"
    except Exception as e:
        print(f"[live_env] ib-bot unreachable ({e}); leaving env as-is", flush=True)
        ENV_SOURCE = "current_env"

# Restore parent overrides — sweep authority beats VPS baseline.
for _k, _v in _PARENT_OVERRIDES.items():
    os.environ[_k] = _v
if _PARENT_OVERRIDES:
    print(f"[live_env] restored {len(_PARENT_OVERRIDES)} parent overrides: "
          f"{sorted(_PARENT_OVERRIDES)}", flush=True)

import pandas as pd  # noqa: E402
import numpy as np  # noqa: E402

from bot.config import (  # noqa: E402
    Settings, PATTERN_MIN_RR,
    FORCE_LONG_COINS, FORCE_SHORT_COINS,
    PYRAMID_EXCLUDED_PATTERNS,
)
from bot.patterns_v2 import detect_all_v2  # noqa: E402
from bot.vstop_structure import find_structure_exit  # noqa: E402
from bot.swings import find_swings  # noqa: E402
from bot.risk import required_rr  # noqa: E402
from bot.trader import cold_combo_risk_mult  # noqa: E402
from bot.data_source import to_yf_ticker  # noqa: E402
from _equity_sim import compute_worst_trough, simulate_with_intrabar_dd  # noqa: E402

settings = Settings.from_env()

# --- Backtest constants (env-overridable) ---
ACCOUNT = float(os.environ.get("ACCOUNT_USD", "240000"))
RISK = float(os.environ.get("BT_RISK", str(settings.risk_per_trade if settings.risk_per_trade >= 0.001 else 0.01)))
# 0.01 default — user wants flat 1% no caps
MM_CAP = float(os.environ.get("MAX_MARGIN_USED_PCT", "0.50"))
STRUCT_BUFFER = float(os.environ.get("STRUCT_BUFFER_PCT", str(settings.struct_buffer_pct)))
MIN_RR = float(os.environ.get("MIN_RR", str(settings.min_rr)))
MIN_RR_CT = float(os.environ.get("MIN_RR_COUNTERTREND", str(settings.min_rr_countertrend)))

# VIX sizing
VIX_ON = os.environ.get("VIX_SIZING_ENABLED", "false").lower() in ("true", "1", "yes")
VIX_HIGH = float(os.environ.get("VIX_HIGH_THRESHOLD", "25.0"))
VIX_LOW = float(os.environ.get("VIX_LOW_THRESHOLD", "14.0"))
VIX_SH = float(os.environ.get("VIX_SIZE_HIGH", "0.5"))
VIX_SL = float(os.environ.get("VIX_SIZE_LOW", "1.5"))

# Sec_type ->effective leverage (margin = notional / lev)
EFF_LEV = {
    "stock":  6.0,    # IB Portfolio Margin (user confirmed enabled 2026-05-12).
                      # Reg T cash = 2x; PM (NLV ≥ $110k) typically 6-7x on liquid US.
    "fx":    30.0,    # CFD ESMA 30:1 (major majors)
    "future":15.0,    # CME avg initial-margin ratio
}
# Fees (round-trip, fraction of notional)
FEE_RT_BY = {
    "stock":  0.00020,   # 2 bps RT (post-fix 2026-05-12)
    "fx":     0.00010,   # 1 bps RT spread approx
    "future": 0.00010,   # 1 bps RT — futures fee/contract is small
}
# Per-side slippage (fraction of notional). Applied 2× in drag_R = (fee + 2*slip)/stop_pct.
# Top-1000 stocks: many mid/low-volume names → 5 bps conservative.
# FX majors+crosses through CFD: 2 bps per side (tight spreads on EUR.USD/USD.JPY,
#   wider on EUR.SEK/exotic). 2 bps is mean.
# Liquid futures (ES/NQ/CL/GC): ~0.5 bps per side. Less liquid (ZW/ZL/HG): 2-3 bps.
#   Mean 1 bps per side covers the universe.
SLIP_BY = {
    "stock":  0.00050,   # 5 bps per side -> 10 bps RT from slippage alone
    "fx":     0.00020,   # 2 bps per side
    "future": 0.00010,   # 1 bps per side
}
SLIP_DEFAULT = 0.00050   # fallback (treat unknown sec_type as a stock)

CACHE = Path(os.environ.get("IB_DATA_DIR", str(DATA_DIR / "yf_cache_ib")))
OUT_PATH = Path(os.environ.get(
    "IB_OUT_PATH", str(DATA_DIR / "honest_replay" / "ib.json")
))

# Patterns excluded — taken from PYRAMID_EXCLUDED_PATTERNS env var? In KF env it's
# pyramid-specific only. For base entries IB env has no global exclusion. Keep as-is.

# Time-window filter (sweep restricts to 1y)
IB_START_TS = os.environ.get("IB_START_TS", "").strip()
IB_END_TS = os.environ.get("IB_END_TS", "").strip()

# Smoke (single-symbol smoke)
SMOKE = set((os.environ.get("SMOKE_COINS") or "").split(",")) - {""}

PROD_WORKING_TF = "1h"
PROD_HIGHER_TF = "4h"


def _resolve_git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(HL_ROOT / "hyperliquid_bot"), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


# --- Universe & symbol typing ---
def load_universe() -> list[tuple[str, str]]:
    """Returns [(sym, sec_type), ...] — top1000 stocks + 38 FX + 52 futures."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    top = json.loads((DATA_DIR / "ib_universe_top1000.json").read_text())
    for s in top["symbols"]:
        if s["sym"] not in seen:
            out.append((s["sym"], s["type"]))
            seen.add(s["sym"])
    full = json.loads((DATA_DIR / "ib_universe.json").read_text())
    for s in full["symbols"]:
        if s["type"] == "future" and s["sym"] not in seen:
            out.append((s["sym"], "future"))
            seen.add(s["sym"])
    return out


UNIVERSE: list[tuple[str, str]] = load_universe()
if SMOKE:
    UNIVERSE = [(s, t) for s, t in UNIVERSE if s in SMOKE]


# --- VIX series ---
def _load_vix_series() -> pd.Series:
    """Daily VIX close, index = tz-aware datetime. Empty if missing."""
    p = CACHE / "1d" / "^VIX.parquet"
    if not p.exists():
        print(f"[vix] no series at {p} — VIX_SIZING will be inert", flush=True)
        return pd.Series(dtype=float)
    df = pd.read_parquet(p)
    if "time" not in df.columns or "Close" not in df.columns:
        return pd.Series(dtype=float)
    s = pd.Series(df["Close"].values, index=pd.to_datetime(df["time"], utc=True))
    s = s[s.notna()].sort_index()
    return s


_VIX_SERIES: pd.Series | None = None


def vix_size_at(ts) -> float:
    """Return VIX risk multiplier at timestamp (1.0 if VIX off / no data)."""
    global _VIX_SERIES
    if not VIX_ON:
        return 1.0
    if _VIX_SERIES is None:
        _VIX_SERIES = _load_vix_series()
    if _VIX_SERIES.empty:
        return 1.0
    ts_pd = pd.Timestamp(ts)
    if ts_pd.tzinfo is None:
        ts_pd = ts_pd.tz_localize("UTC")
    pos = _VIX_SERIES.index.searchsorted(ts_pd, side="right") - 1
    if pos < 0:
        return 1.0
    v = float(_VIX_SERIES.iloc[pos])
    if pd.isna(v):
        return 1.0
    if v > VIX_HIGH:
        return VIX_SH
    if v < VIX_LOW:
        return VIX_SL
    return 1.0


# --- Data loading (parquet) ---
def _load_tf(sym: str, tf: str) -> pd.DataFrame | None:
    ticker = to_yf_ticker(sym)
    p = CACHE / tf / f"{ticker}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if "time" not in df.columns:
        df = df.reset_index().rename(columns={"index": "time", "Datetime": "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.dropna(subset=["Close"]).sort_values("time").reset_index(drop=True)
    return df


def load_bars(sym: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Return (1h, 4h) frames or (None, None) if missing."""
    df_1h = _load_tf(sym, PROD_WORKING_TF)
    df_h = _load_tf(sym, PROD_HIGHER_TF)
    return df_1h, df_h


# --- Wrapped vstop structural exit (matches KF replay) ---
def find_struct_vstop_exit(df, entry_idx, direction, initial_stop,
                           buffer_pct=0.003, max_holding_bars=200):
    swings = find_swings(df, k=3, min_atr_mult=1.0)
    return find_structure_exit(
        df, swings, direction, entry_idx, initial_stop,
        buffer_pct=buffer_pct, swing_confirm_lag=3,
        max_holding_bars=max_holding_bars,
    )


# --- Signal generation: per-coin, returns list of base + pyramid addon trades ---
def _try_emit_signal(df, df_h, coin, sec_type, i, slip, allow_long, allow_short,
                     allow_patterns: set | None, sig_dir_filter: str | None):
    ts = df["time"].iloc[i]
    df_higher = df_h[df_h["time"] <= ts]
    if df_higher.empty or len(df_higher) < 30:
        return None
    df_curr = df.iloc[: i + 1]
    try:
        sigs = detect_all_v2(df_curr, df_higher, coin=coin, timeframe=PROD_WORKING_TF)
    except Exception:
        return None
    if not sigs:
        return None
    sig = sigs[0]
    if sig.direction == "long" and not allow_long:
        return None
    if sig.direction == "short" and not allow_short:
        return None
    if sig_dir_filter and sig.direction != sig_dir_filter:
        return None
    if allow_patterns is not None and sig.pattern not in allow_patterns:
        return None
    higher_trend = (
        "long" if sig.higher_trend == "up"
        else ("short" if sig.higher_trend == "down" else "unknown")
    )
    try:
        rr_min = required_rr(sig.direction, higher_trend, settings)
    except Exception:
        rr_min = settings.min_rr
    pattern_rr = PATTERN_MIN_RR.get(sig.pattern)
    if pattern_rr is not None:
        rr_min = max(rr_min, pattern_rr)
    if sig.rr < rr_min:
        return None
    stop_pct_pre = abs(sig.entry - sig.stop_loss) / sig.entry if sig.entry > 0 else 0.0
    fee_rt = FEE_RT_BY.get(sec_type, 0.0002)
    if stop_pct_pre > 0:
        drag_R = (fee_rt + 2 * slip) / stop_pct_pre
        if (sig.rr - drag_R) < settings.min_rr:
            return None
    # MIN_SL_DIST_PCT (0.5% generic, 0.1% for FX)
    min_sl_dist = 0.001 if sec_type == "fx" else float(os.environ.get("MIN_SL_DIST_PCT", "0.005"))
    if stop_pct_pre < min_sl_dist:
        return None
    # Gap-aware entry fill: signal at bar i Close → real fill at bar i+1 Open.
    # entry_idx for exit walk stays = i so bar i+1 itself is examined for
    # gap-through-stop (exit_px = Open[i+1] = entry_actual → pnl_$ = 0 gross).
    ni = i + 1
    if ni >= len(df):
        return None
    entry_actual = float(df["Open"].iloc[ni])

    try:
        exit_idx, exit_px, exit_reason = find_struct_vstop_exit(
            df, i, sig.direction, sig.stop_loss,
            buffer_pct=STRUCT_BUFFER, max_holding_bars=200,
        )
    except Exception:
        return None

    # Risk_unit sized off PLANNED entry (sig.entry) — actual fill slip flows into pnl.
    risk = abs(sig.entry - sig.stop_loss)
    if risk <= 0:
        return None
    stop_pct = risk / sig.entry
    cold_mult, _bt_n = cold_combo_risk_mult(sig.pattern, coin)
    vix_mult = vix_size_at(ts)
    rmult = cold_mult * vix_mult
    notional = ACCOUNT * RISK * rmult / stop_pct
    pnl_dir = (exit_px - entry_actual) if sig.direction == "long" else (entry_actual - exit_px)
    pnl_R_gross = pnl_dir / risk
    # Effective fee R (drag): notional * fee_rt / risk_$  (risk_$ = ACCOUNT*RISK*rmult)
    risk_usd = max(ACCOUNT * RISK * rmult, 1e-9)
    fee_R = (notional * (fee_rt + 2 * slip)) / risk_usd
    # Intra-trade worst trough — walks from i+1 = ni onwards (includes entry
    # bar's intrabar range), priced off actual fill.
    worst_R, worst_ts = compute_worst_trough(df, i, exit_idx, sig.direction, entry_actual, risk)
    return {
        "pattern": sig.pattern, "coin": coin, "sec_type": sec_type,
        "direction": sig.direction,
        "signal_ts": str(ts), "open_ts": str(df["time"].iloc[ni]),
        "close_ts": str(df["time"].iloc[exit_idx]),
        "signal_idx": int(i), "open_idx": int(ni), "exit_idx": int(exit_idx),
        "entry_planned": float(sig.entry), "entry": float(entry_actual),
        "stop_loss": float(sig.stop_loss),
        "initial_risk": float(risk),
        "pnl_R_gross": float(pnl_R_gross),
        "fee_R": float(fee_R),
        "pnl_R_net": float(pnl_R_gross - fee_R),
        "worst_R_unrealized": float(worst_R),
        "worst_ts": str(worst_ts) if worst_ts is not None else None,
        "risk_mult": float(rmult),
        "vix_mult": float(vix_mult),
        "stop_pct": float(stop_pct),
        "notional": float(notional),
        "rr": float(sig.rr),
        "higher_trend": higher_trend,
        "exit_reason": exit_reason,
    }


def gen(coin: str, sec_type: str) -> tuple[list[dict], str]:
    df, df_h = load_bars(coin)
    if df is None or df_h is None or len(df) < 200 or len(df_h) < 50:
        return [], "skip:no_or_short_data"
    slip = SLIP_BY.get(sec_type, SLIP_DEFAULT)
    trades: list[dict] = []

    # Direction allowance: stocks long-only; FX bidir; futures bidir
    allow_long = True
    allow_short = (sec_type != "stock")

    PYR_ENABLED = settings.pyramid_enabled
    PYR_TRIGGER_R = settings.pyramid_trigger_r
    PYR_MAX_LEVELS = settings.max_pyramid_levels

    open_until_idx = -1
    i = 60
    n = len(df) - 1
    while i < n:
        if i <= open_until_idx:
            i += 1
            continue
        t = _try_emit_signal(df, df_h, coin, sec_type, i, slip,
                             allow_long, allow_short,
                             allow_patterns=None, sig_dir_filter=None)
        if t is None:
            i += 1
            continue
        t["_pyr_level"] = 1
        t["_is_addon"] = False
        trades.append(t)
        chain_root = t["open_ts"]
        chain_dir = t["direction"]
        last_entry = t["entry"]
        last_initial_risk = t["initial_risk"]
        chain_max_exit_idx = t["exit_idx"]

        if PYR_ENABLED and PYR_MAX_LEVELS > 1:
            level = 1
            j = t["open_idx"] + 1
            scan_hi = t["exit_idx"]
            while j < scan_hi and level < PYR_MAX_LEVELS:
                addon = _try_emit_signal(
                    df, df_h, coin, sec_type, j, slip,
                    allow_long, allow_short,
                    allow_patterns=None, sig_dir_filter=chain_dir,
                )
                if addon is None:
                    j += 1
                    continue
                if addon["pattern"] in PYRAMID_EXCLUDED_PATTERNS:
                    j += 1
                    continue
                mark = addon["entry"]
                if chain_dir == "long":
                    unreal_R = (mark - last_entry) / last_initial_risk
                else:
                    unreal_R = (last_entry - mark) / last_initial_risk
                if unreal_R < PYR_TRIGGER_R:
                    j += 1
                    continue
                level += 1
                addon["_pyr_level"] = level
                addon["_is_addon"] = True
                addon["_chain_root_open_ts"] = chain_root
                trades.append(addon)
                last_entry = addon["entry"]
                last_initial_risk = addon["initial_risk"]
                if addon["exit_idx"] > chain_max_exit_idx:
                    chain_max_exit_idx = addon["exit_idx"]
                j = addon["open_idx"] + 1

        open_until_idx = chain_max_exit_idx
        i = chain_max_exit_idx + 1

    return trades, f"yf_{PROD_WORKING_TF}"


def _gen_one(args) -> tuple[str, str, list[dict], str]:
    coin, sec_type = args
    try:
        trs, src = gen(coin, sec_type)
    except Exception as e:
        return coin, sec_type, [], f"skip:engine_err:{type(e).__name__}:{e}"
    return coin, sec_type, trs, src


def main():
    print("=" * 72, flush=True)
    print(f"IB HONEST REPLAY  env_source={ENV_SOURCE}", flush=True)
    print(f"  cache={CACHE}  TF={PROD_WORKING_TF}/{PROD_HIGHER_TF}", flush=True)
    print(f"  ACCOUNT=${ACCOUNT:,.0f}  RISK={RISK}  MM_CAP={MM_CAP}  STRUCT_BUF={STRUCT_BUFFER}", flush=True)
    print(f"  MIN_RR={MIN_RR}  MIN_RR_CT={MIN_RR_CT}  ATR_REGIME={os.environ.get('ATR_REGIME_THRESHOLD','0.7')}", flush=True)
    print(f"  VIX_ON={VIX_ON}  HIGH={VIX_HIGH}->{VIX_SH}  LOW={VIX_LOW}->{VIX_SL}", flush=True)
    print(f"  Universe: {len(UNIVERSE)} syms", flush=True)
    print(f"  Window: [{IB_START_TS or '-inf'} ... {IB_END_TS or '+inf'}]", flush=True)
    print("=" * 72, flush=True)

    workers = int(os.environ.get("IB_WORKERS", "1"))
    workers = max(1, min(workers, len(UNIVERSE))) if UNIVERSE else 1
    print(f"\nWorkers={workers}", flush=True)

    t0 = time.time()
    all_trades: list[dict] = []
    sources: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    attempted = 0

    if workers == 1:
        results_iter = (_gen_one(a) for a in UNIVERSE)
    else:
        from multiprocessing import Pool
        pool = Pool(workers)
        results_iter = pool.imap_unordered(_gen_one, UNIVERSE)

    for coin, sec_type, trs, src in results_iter:
        attempted += 1
        if src.startswith("skip"):
            skipped.append((coin, src.split(":", 1)[1]))
        else:
            sources[coin] = src
            all_trades.extend(trs)
        if attempted % 50 == 0 or attempted == len(UNIVERSE):
            print(f"  {attempted}/{len(UNIVERSE)}  coin={coin}  src={src}  "
                  f"trades={len(all_trades)}  elapsed={time.time()-t0:.0f}s",
                  flush=True)

    if workers > 1:
        pool.close()
        pool.join()

    # Time-window filter
    if IB_START_TS or IB_END_TS:
        before = len(all_trades)
        start_pd = pd.Timestamp(IB_START_TS).tz_localize("UTC") if IB_START_TS else None
        end_pd = pd.Timestamp(IB_END_TS).tz_localize("UTC") if IB_END_TS else None
        def _in_window(t):
            ts = pd.Timestamp(t["open_ts"])
            if ts.tzinfo is None: ts = ts.tz_localize("UTC")
            if start_pd is not None and ts < start_pd: return False
            if end_pd is not None and ts > end_pd: return False
            return True
        all_trades = [t for t in all_trades if _in_window(t)]
        print(f"\n  Window filter: {before} ->{len(all_trades)}", flush=True)

    # --- Caps applied chronologically (MM cap + pyramid chain) ---
    all_trades.sort(key=lambda x: x["open_ts"])
    accepted: list[dict] = []
    rejected_mm = 0
    open_pos: list[dict] = []

    # Running equity (for MM% trajectory)
    eq = ACCOUNT
    peak = ACCOUNT
    max_dd = 0.0
    mm_samples: list[float] = []  # post-decision margin/account ratio
    accepted_chain_roots: set[str] = set()
    rejected_chain_roots: set[str] = set()
    rejected_addon_chain = 0

    def _close_expired(now_ts_pd):
        nonlocal eq, peak, max_dd
        new_open = []
        for op in open_pos:
            if pd.Timestamp(op["close_ts"]) > now_ts_pd:
                new_open.append(op)
            else:
                pnl = op["pnl_R_net"] * (op["eq_at_accept"] * RISK * op["risk_mult"])
                eq += pnl
                peak = max(peak, eq)
                dd = 100 * (peak - eq) / peak if peak > 0 else 0.0
                if dd > max_dd:
                    max_dd = dd
        open_pos.clear()
        open_pos.extend(new_open)

    for t in all_trades:
        ts_pd = pd.Timestamp(t["open_ts"])
        _close_expired(ts_pd)

        is_addon = bool(t.get("_is_addon"))
        chain_root = t.get("_chain_root_open_ts") if is_addon else t["open_ts"]
        if is_addon and (chain_root in rejected_chain_roots or chain_root not in accepted_chain_roots):
            rejected_addon_chain += 1
            continue

        # MM cap — leverage by sec_type
        eff_lev = EFF_LEV.get(t["sec_type"], 1.0)
        margin = t["notional"] / eff_lev
        cur_m = sum(op["margin"] for op in open_pos)
        if (cur_m + margin) / ACCOUNT > MM_CAP:
            rejected_mm += 1
            if not is_addon:
                rejected_chain_roots.add(chain_root)
            continue

        # Accept
        op = {
            "close_ts": t["close_ts"],
            "margin": margin,
            "eq_at_accept": eq,
            "pnl_R_net": t["pnl_R_net"],
            "risk_mult": t["risk_mult"],
        }
        open_pos.append(op)
        mm_samples.append((cur_m + margin) / ACCOUNT)
        if not is_addon:
            accepted_chain_roots.add(chain_root)
        t["_eq_at_accept"] = eq
        t["_mm_pct"] = 100 * (cur_m + margin) / ACCOUNT
        t["_margin"] = margin
        t["_eff_lev"] = eff_lev
        accepted.append(t)

    # Realize remaining
    end_ts = pd.Timestamp(max((op["close_ts"] for op in open_pos), default=str(pd.Timestamp.utcnow())))
    _close_expired(end_ts + pd.Timedelta(seconds=1))

    # Mark-to-market MaxDD using intra-trade worst troughs.
    # Above accept-loop tracks `max_dd` on REALIZED equity (close-to-close).
    # Live bot MM%/equity sees unrealized PnL of every open position every tick,
    # so realized-only DD systematically under-counts intra-trade valleys.
    # See scripts/_equity_sim.py + feedback_backtest_intrabar_maxdd.md.
    # RISK env may differ from settings.risk_per_trade — make a shim so the
    # sim uses the same risk_pct the accept loop did.
    class _SettingsShim:
        risk_per_trade = RISK
    _mtm_summary = simulate_with_intrabar_dd(accepted, ACCOUNT, _SettingsShim())
    max_dd_realized = max_dd  # close-only (legacy view)
    max_dd = _mtm_summary["max_dd_pct"]  # intra-bar (true)

    n = len(accepted)
    addons_n = sum(1 for x in accepted if x.get("_is_addon"))
    base_n = n - addons_n

    # Aggregates
    gross_R = sum(t["pnl_R_gross"] for t in accepted)
    fees_R = sum(t["fee_R"] for t in accepted)
    net_R = sum(t["pnl_R_net"] for t in accepted)
    wins = sum(1 for t in accepted if t["pnl_R_net"] > 0)

    if n > 0:
        final_eq = eq
        win_rate = 100 * wins / n
        ts_min = pd.to_datetime(min(t["open_ts"] for t in accepted))
        ts_max = pd.to_datetime(max(t["close_ts"] for t in accepted))
        days = max((ts_max - ts_min).days, 1)
        years = max(days / 365.25, 0.1)
        cagr = ((final_eq / ACCOUNT) ** (1 / years) - 1) * 100
    else:
        final_eq, win_rate, days, years, cagr = ACCOUNT, 0.0, 0, 0.0, 0.0
        ts_min = ts_max = None

    mm_avg = 100 * sum(mm_samples) / len(mm_samples) if mm_samples else 0.0
    mm_max = 100 * max(mm_samples) if mm_samples else 0.0

    by_pat: dict[str, list[dict]] = defaultdict(list)
    by_coin: dict[str, list[dict]] = defaultdict(list)
    by_sec: dict[str, list[dict]] = defaultdict(list)
    for t in accepted:
        by_pat[t["pattern"]].append(t)
        by_coin[t["coin"]].append(t)
        by_sec[t["sec_type"]].append(t)

    def stats(trs):
        if not trs:
            return {"n": 0}
        nn = len(trs)
        sumR = sum(t["pnl_R_net"] for t in trs)
        w = [t["pnl_R_net"] for t in trs if t["pnl_R_net"] > 0]
        l = [t["pnl_R_net"] for t in trs if t["pnl_R_net"] <= 0]
        return {
            "n": nn,
            "win_rate": round(100 * len(w) / nn, 2),
            "avg_R": round(sumR / nn, 4),
            "sum_R": round(sumR, 2),
            "avg_win": round(sum(w) / max(len(w), 1), 3),
            "avg_loss": round(sum(l) / max(len(l), 1), 3),
        }

    per_pattern = [{"pattern": p, **stats(trs)} for p, trs in sorted(by_pat.items())]
    per_sec = [{"sec_type": s, **stats(trs)} for s, trs in sorted(by_sec.items())]
    per_coin_all = sorted(
        [{"coin": c, **stats(trs)} for c, trs in by_coin.items()],
        key=lambda r: -r.get("sum_R", 0),
    )
    per_coin_top20 = per_coin_all[:20]

    out = {
        "exchange": "ib",
        "git_head": _resolve_git_head(),
        "env_source": ENV_SOURCE,
        "history_window": {
            "start": str(ts_min) if ts_min is not None else None,
            "end": str(ts_max) if ts_max is not None else None,
            "days": int(days), "years": round(years, 2),
        },
        "config_resolved": {
            "risk_per_trade": RISK,
            "min_rr": MIN_RR, "min_rr_countertrend": MIN_RR_CT,
            "atr_regime_threshold": float(os.environ.get("ATR_REGIME_THRESHOLD", "0.7")),
            "min_sl_dist_pct": float(os.environ.get("MIN_SL_DIST_PCT", "0.005")),
            "max_margin_used_pct": MM_CAP,
            "struct_buffer_pct": STRUCT_BUFFER,
            "vix_sizing_enabled": VIX_ON,
            "vix_high_threshold": VIX_HIGH, "vix_low_threshold": VIX_LOW,
            "vix_size_high": VIX_SH, "vix_size_low": VIX_SL,
            "fee_rt_by_sec": FEE_RT_BY,
            "slip_per_side_by_sec": SLIP_BY,
            "slippage_default_per_side": SLIP_DEFAULT,
            "eff_leverage": EFF_LEV,
            "account_starting_usd": ACCOUNT,
        },
        "coins_attempted": attempted,
        "coins_with_data": len(sources),
        "coins_skipped": [{"coin": c, "reason": r} for c, r in skipped[:200]],
        "skipped_total": len(skipped),
        "caps_applied": {
            "raw_signals": len(all_trades),
            "rejected_mm_cap": rejected_mm,
            "rejected_addon_chain": rejected_addon_chain,
            "pyramid_addons": addons_n,
            "accepted": n,
            "base_entries": base_n,
        },
        "pyramid": {
            "enabled": settings.pyramid_enabled,
            "trigger_R": settings.pyramid_trigger_r,
            "max_levels": settings.max_pyramid_levels,
            "excluded_patterns": sorted(PYRAMID_EXCLUDED_PATTERNS),
        },
        "summary": {
            "trades": n,
            "gross_R": round(gross_R, 2),
            "fees_R": round(fees_R, 2),
            "net_R": round(net_R, 2),
            "cagr_pct": round(cagr, 2),
            "max_dd_pct": round(max_dd, 2),
            "max_dd_realized_pct": round(max_dd_realized, 2),
            "maxdd_method": _mtm_summary["method"],
            "win_rate": round(win_rate, 2),
            "final_equity_usd": round(final_eq, 2),
            "mm_avg_pct": round(mm_avg, 2),
            "mm_max_pct": round(mm_max, 2),
        },
        "per_sec_type": per_sec,
        "per_pattern": per_pattern,
        "per_coin_top20": per_coin_top20,
    }

    if os.environ.get("IB_DUMP_TRADES", "0") == "1":
        out["trades"] = accepted

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, default=str, indent=2)

    print()
    print("=" * 60)
    print("  IB HONEST REPLAY — SUMMARY")
    print("=" * 60)
    print(f"  Window:    {out['history_window']['start']} ->{out['history_window']['end']}  ({days}d / {years:.2f}y)")
    print(f"  Coins:     attempted={attempted}  with_data={len(sources)}  skipped={len(skipped)}")
    print(f"  Trades:    raw={len(all_trades)}  accepted={n}  win_rate={win_rate:.1f}%")
    print(f"  R:         gross={gross_R:+.1f}  fees={fees_R:.1f}  net={net_R:+.1f}  avgR={net_R/max(n,1):+.3f}")
    print(f"  Equity:    ${ACCOUNT:,} ->${final_eq:,.0f}  ({100*(final_eq/ACCOUNT-1):+.1f}%)")
    print(f"  CAGR:      {cagr:+.2f}%/yr   Max DD: {max_dd:.2f}% intra-bar  ({max_dd_realized:.2f}% realized)")
    print(f"  MM%:       avg={mm_avg:.1f}%  max={mm_max:.1f}%  (cap={MM_CAP*100:.0f}%)")
    print(f"  By sec:    {per_sec}")
    print(f"  Output:    {OUT_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()
