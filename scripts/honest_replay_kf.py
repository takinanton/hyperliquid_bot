"""KF honest replay backtest using LIVE .env from the live VPS.

ENV SOURCE (2026-05-10): live `kraken-bot:/root/hyperliquid_bot/.env` через ssh.
Snapshots под `data/backtest_params_*/envs/` ЗАПРЕЩЕНЫ как default — приводили
к 3-day drift (KF RISK halved, pyramid/dd_halt/exclusions добавлены, COOLDOWN/
LONG_ONLY/PREMIUM/MAX_NET_POSITIONS выпилены). См. scripts/_live_env.py.

Explicit `KF_ENV_FILE=PATH` оставлен для воспроизведения исторических прогонов.

TF is taken from bot.config.WORKING_TF / HIGHER_TF (currently 1h / 4h)
— see _data_loader.load_prod().
"""
from __future__ import annotations  # PEP 563 — defer type-hint eval for py3.9 compat
import warnings; warnings.filterwarnings('ignore')
import os, sys, json, glob, time, subprocess
from pathlib import Path

HL_ROOT = os.environ.get("HL_ROOT", "/Users/ak/Desktop/HL")
DATA_DIR = os.path.join(HL_ROOT, "data")

sys.path.insert(0, os.path.join(HL_ROOT, "scripts"))
# NOTE: removed `sys.path.insert(0, HL_ROOT/hyperliquid_bot)` 2026-05-12.
# activate_live_bot("kf") below puts /tmp/live_bots/kf at sys.path[0];
# adding hyperliquid_bot/ here would let stale Mac `bot/` shadow live bot/.
from _live_env import load_live_env, load_env_file  # noqa: E402
from _live_bot import activate_live_bot  # noqa: E402

ENV_FILE_OVERRIDE = os.environ.get("KF_ENV_FILE", "").strip()
if ENV_FILE_OVERRIDE:
    ENV_SOURCE = f"file:{ENV_FILE_OVERRIDE}"
    load_env_file(ENV_FILE_OVERRIDE)
else:
    ENV_SOURCE = "live:kraken-bot"
    load_live_env("kf")

# 2026-05-17: overlay honest-backtest constants on live env. Aborts if missing.
from _honest_overrides import apply_honest_overrides  # noqa: E402
_HONEST_APPLIED = apply_honest_overrides()

# Pull live bot/ from kraken-bot VPS — source of truth for prod semantics.
# Without this, the script would read stale Mac bot/ (e.g. dd_halt still
# registered after 2026-05-11 rip). See scripts/_live_bot.py.
activate_live_bot("kf")

# Opt-in TF override (default = prod). Lets us run 1h-only vs 4h-only sweeps
# without touching live config. Must be set BEFORE importing _data_loader.
import _data_loader as _dl  # noqa: E402
_TF_W_OVR = os.environ.get("KF_WORKING_TF", "").strip()
_TF_H_OVR = os.environ.get("KF_HIGHER_TF", "").strip()
if _TF_W_OVR:
    _dl.PROD_WORKING_TF = _TF_W_OVR
if _TF_H_OVR:
    _dl.PROD_HIGHER_TF = _TF_H_OVR

SMOKE = set((os.environ.get("SMOKE_COINS") or "").split(",")) - {""}

OUT_PATH = os.environ.get(
    "KF_OUT_PATH",
    os.path.join(DATA_DIR, "honest_replay", "kf.json"),
)

from _data_loader import load_prod, load_slippage, PROD_WORKING_TF, PROD_HIGHER_TF  # noqa: E402

import pandas as pd  # noqa: E402
from collections import defaultdict  # noqa: E402

from bot.config import (  # noqa: E402
    Settings, PATTERN_MIN_RR,
    FORCE_LONG_COINS, FORCE_SHORT_COINS,
    PYRAMID_EXCLUDED_PATTERNS,
)
from bot.patterns_v2 import detect_all_v2  # noqa: E402
# bot.vstop module removed in earlier cleanup — use vstop_structure.find_structure_exit
# wrapped to match the legacy signature used by this script.
from bot.vstop_structure import find_structure_exit  # noqa: E402
from bot.swings import find_swings  # noqa: E402

def find_struct_vstop_exit(df, entry_idx, direction, initial_stop,
                           buffer_pct=0.003, max_holding_bars=200):
    """Wrapper matching legacy signature; mirrors bot.trader._compute_struct_stop:
    swings via find_swings(k=3, min_atr_mult=1.0), then trail through find_structure_exit."""
    swings = find_swings(df, k=3, min_atr_mult=1.0)
    return find_structure_exit(
        df, swings, direction, entry_idx, initial_stop,
        buffer_pct=buffer_pct, swing_confirm_lag=3,
        max_holding_bars=max_holding_bars,
    )

from bot.risk import required_rr, is_btc_regime_blocked  # noqa: E402
from bot.trader import cold_combo_risk_mult  # noqa: E402
from bot.prod_gates import assert_backtest_parity, ParityError  # noqa: E402
from _equity_sim import compute_worst_trough, simulate_with_intrabar_dd  # noqa: E402
import numpy as np  # noqa: E402
# NOTE: bot.oscillators.is_premium_signal removed — premium tier deleted in
# commit b37b8a5 (2026-05-07). Settings.premium_risk_mult no longer exists.

# --- Engine constants ---
# Starting bank: override via ACCOUNT_USD env var to backtest with the live
# acct value (handy after a deposit). Default $50K = nominal compounding base.
ACCOUNT = float(os.environ.get("ACCOUNT_USD", "50000"))

# 2026-05-10: KF leverage is adaptive (project_kf_adaptive_leverage_2026_05_09).
# Per-trade safe_lev = min(KRAKEN_LEVERAGE_CAP, 1/((1+LIQ_SL_BUFFER)*stop_pct + KF_MM_RATE_APPROX)).
# Hardcoded LEV=10 was wrong: thin-SL coins effectively run at 30-100x in prod,
# so margin / MM rejection sim under-counted by 3-10×. We now compute per-trade.
LEV_CAP = float(os.environ.get("KRAKEN_LEVERAGE_CAP", "100"))
LIQ_SL_BUFFER = float(os.environ.get("LIQ_SL_BUFFER", "0.3"))
MM_RATE = float(os.environ.get("KF_MM_RATE_APPROX", "0.025"))


def _adaptive_lev(stop_pct: float) -> float:
    """Mirror bot/exchange_kraken.py adaptive leverage: cap × buffer formula."""
    if stop_pct <= 0:
        return LEV_CAP
    needed = 1.0 / ((1.0 + LIQ_SL_BUFFER) * stop_pct + MM_RATE)
    return float(max(1.0, min(LEV_CAP, needed)))


FEE_RT = 0.0010  # 0.05% taker per side x2 = 0.10% RT (Kraken Futures)

# 2026-05-17 honest gate: MIN_SL_DIST_PCT enforced in backtest (live gate live-only before).
# Per memory `project_min_sl_dist_gate_2026_05_11.md` and root-cause incident: 22 trades
# with stop_pct < 0.5% leaked into prior baseline because backtest bypassed this gate.
MIN_SL_DIST_PCT = float(os.environ.get("MIN_SL_DIST_PCT", "0.005"))
print(f"[honest_min_sl] MIN_SL_DIST_PCT={MIN_SL_DIST_PCT*100:.2f}%  "
      f"(signals with stop_pct < {MIN_SL_DIST_PCT*100:.2f}% will be rejected)", flush=True)

# Opt-in: reject signals whose stop_pct > MAX_SL_PCT. Default 1.0 = no cap.
# Set e.g. MAX_SL_PCT=0.03 to filter trades with stop > 3% from entry.
MAX_SL_PCT = float(os.environ.get("MAX_SL_PCT", "1.0"))


def _resolve_git_head() -> str:
    """`git rev-parse HEAD` of hyperliquid_bot repo, or 'unknown'."""
    try:
        out = subprocess.run(
            ["git", "-C", os.path.join(HL_ROOT, "hyperliquid_bot"), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"

# --- Resolve .env values via Settings ---
settings = Settings.from_env()
MIN_RR = settings.min_rr
MIN_RR_CT = settings.min_rr_countertrend
# Settings.exit_mode was removed when vstop_atr was cleaned up — bot now hardwires
# structural trailing (find_structure_exit). Read from env for log/output only.
EXIT_MODE = os.environ.get("EXIT_MODE", "vstop_struct")
STRUCT_BUFFER = settings.struct_buffer_pct


def _tf_hours(tf: str) -> float:
    n = int(''.join(c for c in tf if c.isdigit()))
    u = ''.join(c for c in tf if c.isalpha()).lower()
    return n * {'m': 1 / 60, 'h': 1, 'd': 24}[u]


WORKING_HOURS = _tf_hours(PROD_WORKING_TF)

# MM cap: prod uses MAX_MM_PCT (main.py) but trader.py:396 still reads MAX_MARGIN_USED_PCT
# — read both, prefer the new name. See bot/prod_gates.py max_margin_used_pct notes.
MM_CAP = float(os.environ.get("MAX_MM_PCT", os.environ.get("MAX_MARGIN_USED_PCT", "0.90")))

# -------------------------------------------------------------------------
# PROD-PARITY DECLARATION (see bot/prod_gates.py).
# -------------------------------------------------------------------------
APPLIED_GATES: set[str] = {
    "force_long_short_coins", "min_rr",
    "pattern_min_rr", "open_trade_per_coin",
    "vstop_trailing",
    "max_margin_used_pct", "slippage_and_fees", "whitelist_coins",
    "btc_regime_filter",       # bot.risk.is_btc_regime_blocked on 4h EMA200
    "liquidity_drag_check",    # drag = (fee+2*slip)/stop_pct
    "cold_combo_cap",          # bot.trader.cold_combo_risk_mult
    "cascade_filter",          # simulate_portfolio cross-coin loss count
    "liquidity_tier_mult",     # bot/trader.py:270 — vol_4h/4 → 1h_est tier × risk
    "adaptive_rr_by_leverage", # 2026-05-10: KRAKEN_LEVERAGE_CAP + safe_lev formula
    "pyramid_addon_trigger",   # gen() inline pyramid sweep + PYR_TRIGGER_R / PYR_MAX_LEVELS
    "liquidity_cap_notional",  # cap_ratio in _try_emit_signal (1h_vol_est / MIN_1H_LIQ_RATIO)
    "max_opens_per_day_cycle", # trivial — defaults 999, no-op
    # concentration_max_net_positions removed 2026-05-08 (gate deleted from prod & registry).
}

# 2026-05-07 (post-1mo backtest review): SKIP_FLAG_SHORT отсутствует в
# bot/prod_gates.py registry — assert_backtest_parity() его не видит. Применяем
# явно в gen() ниже. Регистрация в prod_gates → followup.
# 2026-05-08: LONG_ONLY purged — directional restriction removed everywhere.
SKIP_FLAG_SHORT = os.environ.get("SKIP_FLAG_SHORT", "false").lower() in ("true", "1", "yes")
EXEMPT_GATES: dict[str, str] = {
    "already_traded_dedup":     "single-coin lock approximates it",
    "force_trend_override":     "operator override — not a backtest concern",
    "min_notional":             "all backtested coins clear KF min notional",
    "funding_blocked":          "TODO: hourly funding history per coin",
    "hip3_min_sl_distance":     "N/A on Kraken (no HIP-3)",
    "hip3_market_hours":        "N/A on Kraken (no HIP-3)",
    "liq_vs_sl_buffer":         "N/A on Kraken (project_liq_vs_sl_guard: KF excluded)",
    "pattern_boost_mult":       "empty in current .env — re-check before next deploy",
    "signal_drift_freshness":   "trivial in backtest — entry == bar close",
    "dd_halt":                  "project_dd_halt_removed_2026_05_11 — halt_pct=0 in prod",
}

try:
    assert_backtest_parity(APPLIED_GATES, exempt=EXEMPT_GATES.keys(), strict=True)
    print(f"[prod_gates] parity OK — applied={len(APPLIED_GATES)}  "
          f"exempt={len(EXEMPT_GATES)} (TODO/known)", flush=True)
except ParityError as e:
    print(f"\n{e}\n", flush=True)
    print("[prod_gates] update APPLIED_GATES/EXEMPT_GATES in honest_replay_kf.py", flush=True)
    raise SystemExit(2)

SLIP_FILE = os.path.join(DATA_DIR, "kf_slippage.json")
# 2026-05-17 honest override: replace 0.0005 default + apply 0.0024 floor to map.
# Per memory `feedback_fees_slippage_in_backtests.md` and root-cause incident:
# live .env had SLIPPAGE=0.002 but `SLIPPAGE_DEFAULT_PER_SIDE` not honored —
# default was hardcoded 0.0005, way below mandated 0.0024.
SLIP_DEFAULT = float(os.environ.get("SLIPPAGE_DEFAULT_PER_SIDE", "0.0024"))
SLIP_FLOOR = float(os.environ.get("SLIPPAGE_FLOOR_PER_SIDE", "0.0024"))
_SLIP_RAW = load_slippage(SLIP_FILE)
# Apply floor: if per-coin map has value < floor, use floor instead
SLIP = {coin: max(s, SLIP_FLOOR) for coin, s in _SLIP_RAW.items()}
n_floor_applied = sum(1 for c, s in _SLIP_RAW.items() if s < SLIP_FLOOR)
print(f"[honest_slip] default={SLIP_DEFAULT*100:.3f}%/side  floor={SLIP_FLOOR*100:.3f}%/side  "
      f"floor_applied_to={n_floor_applied}/{len(_SLIP_RAW)} coins", flush=True)

# 2026-05-17: funding cost on leveraged notional (KF perps ~7-10%/y avg).
# Charged per day on open position. Per memory `feedback_fees_slippage_in_backtests.md`.
FUNDING_COST_ANNUAL = float(os.environ.get("FUNDING_COST_ANNUAL_PCT", "0.08"))
FUNDING_COST_DAILY = FUNDING_COST_ANNUAL / 365.0
print(f"[honest_funding] annual={FUNDING_COST_ANNUAL*100:.1f}%/y  daily={FUNDING_COST_DAILY*100:.4f}%/day", flush=True)

# Data directory + filename pattern (env-overridable for non-default windows / TFs).
# Default: kf_1h_full/{coin}_USD_USD.json. For 4h-native data set
# KF_DATA_DIR=kf_4h_333d (file suffix stays the same).
KF_DATA_DIR = os.environ.get("KF_DATA_DIR", "kf_1h_full")
KF_DATA_FILE_SUFFIX = os.environ.get("KF_DATA_FILE_SUFFIX", "_USD_USD.json")

# Optional time window — restrict signals to [KF_START_TS, KF_END_TS]. Empty = full history.
# ISO format like "2026-04-04" or "2026-04-04T00:00:00".
KF_START_TS = os.environ.get("KF_START_TS", "").strip()
KF_END_TS   = os.environ.get("KF_END_TS",   "").strip()

# Cascade filter — env-driven, match bot/trader.py defaults.
CASCADE_WINDOW_MIN = int(os.environ.get("CASCADE_WINDOW_MIN", "60"))
CASCADE_THRESHOLD = int(os.environ.get("CASCADE_THRESHOLD", "3"))

# Liquidity tier mult — env-driven, match bot/trader.py:270-273 defaults.
LIQUIDITY_TIER_HIGH_USD = float(os.environ.get("LIQUIDITY_TIER_HIGH_USD", "2000000"))
LIQUIDITY_TIER_MID_USD = float(os.environ.get("LIQUIDITY_TIER_MID_USD", "300000"))
LIQUIDITY_RISK_MULT_HIGH = float(os.environ.get("LIQUIDITY_RISK_MULT_HIGH", "2.0"))
LIQUIDITY_RISK_MULT_MID = float(os.environ.get("LIQUIDITY_RISK_MULT_MID", "1.5"))
# Adaptive sizing cap (bot/trader.py:310-315): notional <= hourly_vol_est / ratio. KF has no HIP-3.
MIN_1H_LIQ_RATIO = float(os.environ.get("MIN_1H_LIQUIDITY_RATIO", "5"))

# -------------------------------------------------------------------------
# BTC 4h regime series (matches bot.risk.higher_tf_trend EMA200 logic).
# Pre-compute once so gen() can do O(log n) lookup per ts.
# -------------------------------------------------------------------------
_EMA_LENGTH = 200  # bot.config.EMA_LENGTH

def _build_btc_trend_series() -> "pd.Series":
    btc_path = os.path.join(DATA_DIR, KF_DATA_DIR, f"BTC{KF_DATA_FILE_SUFFIX}")
    if not os.path.exists(btc_path):
        print(f"[btc_regime] WARN no BTC candles at {btc_path} — filter inert", flush=True)
        return pd.Series(dtype=object)
    _, df_btc = load_prod(btc_path)  # higher TF series for regime
    if df_btc is None or df_btc.empty or len(df_btc) < _EMA_LENGTH:
        print("[btc_regime] WARN BTC candles short — filter inert", flush=True)
        return pd.Series(dtype=object)
    ema = df_btc["Close"].ewm(span=_EMA_LENGTH, adjust=False).mean()
    trend = pd.Series(
        np.where(df_btc["Close"] > ema, "long", "short"),
        index=df_btc["time"],
    )
    trend.iloc[:_EMA_LENGTH] = "unknown"
    print(f"[btc_regime] series built: {len(trend)} {PROD_HIGHER_TF} bars  "
          f"({trend.iloc[_EMA_LENGTH:].value_counts().to_dict()})", flush=True)
    return trend


_BTC_TRENDS: "pd.Series | None" = None


def _btc_trend_at(ts) -> str:
    global _BTC_TRENDS
    if _BTC_TRENDS is None:
        _BTC_TRENDS = _build_btc_trend_series()
    if _BTC_TRENDS.empty:
        return "unknown"
    pos = _BTC_TRENDS.index.searchsorted(ts, side="right") - 1
    if pos < 0:
        return "unknown"
    return str(_BTC_TRENDS.iloc[pos])

# --- Pair list ---
# 1. Explicit COINS env wins (operator override).
# 2. Otherwise call bot.config._coins_from_env() — same path prod uses, which
#    queries ccxt.krakenfutures().load_markets() for COIN_FILTER=none on Kraken.
# 3. Final fallback: list whatever data files we have on disk (for offline runs).
RAW_COINS = os.environ.get("COINS", "").strip()
COIN_LIST: list[str] = []
if RAW_COINS:
    for raw in RAW_COINS.split(","):
        sym = raw.strip().split("/")[0].upper()
        if sym:
            COIN_LIST.append(sym)
else:
    try:
        from bot.config import _coins_from_env
        COIN_LIST = [c.split("/")[0].upper() for c in _coins_from_env()]
        print(f"[universe] resolved via bot.config._coins_from_env() → {len(COIN_LIST)} coins", flush=True)
    except Exception as exc:
        print(f"[universe] _coins_from_env failed ({exc}); falling back to disk listing", flush=True)
        try:
            disk = sorted(
                f.replace(KF_DATA_FILE_SUFFIX, "")
                for f in os.listdir(os.path.join(DATA_DIR, KF_DATA_DIR))
                if f.endswith(KF_DATA_FILE_SUFFIX)
            )
            COIN_LIST = disk
            print(f"[universe] disk fallback → {len(COIN_LIST)} coins", flush=True)
        except Exception as exc2:
            print(f"[universe] disk listing failed: {exc2}", flush=True)
# de-dupe preserving order
seen = set()
COIN_LIST = [c for c in COIN_LIST if not (c in seen or seen.add(c))]
if SMOKE:
    COIN_LIST = [c for c in COIN_LIST if c in SMOKE]


# --- Per-coin data loading: directory + filename pattern from env (default kf_1h_full) ---
def resolve_path(coin: str) -> str | None:
    primary = os.path.join(DATA_DIR, KF_DATA_DIR, f"{coin}{KF_DATA_FILE_SUFFIX}")
    if os.path.exists(primary):
        return primary
    return None


def _try_emit_signal(df, df_h, coin, i, slip, force_long, force_short,
                     allow_patterns: set | None, sig_dir_filter: str | None):
    """Run detect_all_v2 + all pre-entry gates at bar i. Returns dict for the
    accepted trade, or None. Used for both base entries and pyramid addons.

    sig_dir_filter: 'long' / 'short' to restrict direction (pyramid mode);
        None for base entries.
    allow_patterns: set of patterns explicitly permitted (pyramid mode); None
        to allow all (subject to SKIP_FLAG_SHORT etc.).
    """
    ts = df['time'].iloc[i]
    df_higher = df_h[df_h['time'] <= ts]
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
    if SKIP_FLAG_SHORT and sig.pattern == 'flag_short':
        return None
    if force_long and sig.direction != 'long':
        return None
    if force_short and sig.direction != 'short':
        return None
    if sig_dir_filter and sig.direction != sig_dir_filter:
        return None
    if allow_patterns is not None and sig.pattern not in allow_patterns:
        return None
    btc_blocked, _ = is_btc_regime_blocked(sig.pattern, coin, _btc_trend_at(ts))
    if btc_blocked:
        return None
    higher_trend = (
        'long' if sig.higher_trend == 'up'
        else ('short' if sig.higher_trend == 'down' else 'unknown')
    )
    if force_long:
        higher_trend = 'long'
    elif force_short:
        higher_trend = 'short'
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
    if MAX_SL_PCT < 1.0 and stop_pct_pre > MAX_SL_PCT:
        return None
    # 2026-05-17 honest gate: enforce MIN_SL_DIST_PCT (live gate, was bypassed in replay).
    if stop_pct_pre < MIN_SL_DIST_PCT:
        return None
    if stop_pct_pre > 0:
        drag_R = (FEE_RT + 2 * slip) / stop_pct_pre
        if (sig.rr - drag_R) < settings.min_rr:
            return None
    try:
        exit_idx, exit_px, exit_reason = find_struct_vstop_exit(
            df, i, sig.direction, sig.stop_loss,
            buffer_pct=STRUCT_BUFFER, max_holding_bars=200,
        )
    except Exception:
        return None
    risk = abs(sig.entry - sig.stop_loss)
    if risk <= 0:
        return None
    stop_pct = risk / sig.entry
    cold_mult, _bt_n = cold_combo_risk_mult(sig.pattern, coin)
    liquidity_risk_mult = 1.0
    hourly_vol_est = 0.0
    try:
        vol_units = float(df["Volume"].iloc[i])
        close_px = float(df["Close"].iloc[i])
        vol_4h_usd = vol_units * close_px
        hourly_vol_est = vol_4h_usd / 4.0 if vol_4h_usd > 0 else 0.0
        if hourly_vol_est >= LIQUIDITY_TIER_HIGH_USD:
            liquidity_risk_mult = LIQUIDITY_RISK_MULT_HIGH
        elif hourly_vol_est >= LIQUIDITY_TIER_MID_USD:
            liquidity_risk_mult = LIQUIDITY_RISK_MULT_MID
    except Exception:
        pass
    rmult = cold_mult * liquidity_risk_mult
    notional_uncapped = ACCOUNT * settings.risk_per_trade * rmult / stop_pct

    # Adaptive liquidity cap (mirror bot/trader.py:310-315):
    # notional <= hourly_vol_est / MIN_1H_LIQ_RATIO. cap_ratio scales $-PnL/notional/margin
    # proportionally when binding; fee_R stays invariant (= fees/stop_pct).
    cap_ratio = 1.0
    notional = notional_uncapped
    if hourly_vol_est > 0 and MIN_1H_LIQ_RATIO > 0:
        liq_cap = hourly_vol_est / MIN_1H_LIQ_RATIO
        if 0 < liq_cap < notional_uncapped:
            notional = liq_cap
            cap_ratio = liq_cap / notional_uncapped

    pnl_dir = (exit_px - sig.entry) if sig.direction == 'long' else (sig.entry - exit_px)
    pnl_R_gross = pnl_dir / risk
    fee_R = (notional * (FEE_RT + 2 * slip)) / max(ACCOUNT * settings.risk_per_trade * rmult * cap_ratio, 1e-9)
    # Funding cost on leveraged notional (added 2026-05-17 honest fix).
    # Charged per day open. Long pays, short receives — but avg KF perp funding
    # historically positive ~7-10%/y → use as cost on long, income on short.
    try:
        bars_held = max(exit_idx - i, 1)
        # WORKING_TF default 4h → 6 bars/day; HIGHER_TF 1d. Approximate.
        days_held = bars_held * 4.0 / 24.0
    except Exception:
        days_held = 1.0
    funding_cost_pct = FUNDING_COST_DAILY * days_held
    funding_R = (notional * funding_cost_pct) / max(ACCOUNT * settings.risk_per_trade * rmult * cap_ratio, 1e-9)
    if sig.direction == 'short':
        funding_R = -funding_R  # short receives funding (cost negative)
    fee_R = fee_R + funding_R  # fold funding into fee_R for backward compat
    # Intra-trade worst trough — enables mark-to-market MaxDD (see _equity_sim.py).
    worst_R, worst_ts = compute_worst_trough(df, i, exit_idx, sig.direction, sig.entry, risk)
    return {
        'pattern': sig.pattern, 'coin': coin, 'direction': sig.direction,
        'is_premium': False,
        'open_ts': str(ts), 'close_ts': str(df['time'].iloc[exit_idx]),
        'open_idx': int(i), 'exit_idx': int(exit_idx),
        'entry': float(sig.entry),
        'stop_loss': float(sig.stop_loss),
        'initial_risk': float(risk),
        'pnl_R_gross': float(pnl_R_gross),
        'fee_R': float(fee_R),
        'pnl_R_net': float(pnl_R_gross - fee_R),
        'worst_R_unrealized': float(worst_R),
        'worst_ts': str(worst_ts) if worst_ts is not None else None,
        'risk_mult': float(rmult),
        'stop_pct': float(stop_pct),
        'notional': float(notional),
        'cap_ratio': float(cap_ratio),
        'rr': float(sig.rr),
        'higher_trend': higher_trend,
        'exit_reason': exit_reason,
    }


def gen(coin: str, path: str) -> tuple[list[dict], str]:
    """Per-coin scan: base entries (single-coin lock) + inline pyramid addons.

    Performance: detect_all_v2 is invoked only on bars where the coin is "free"
    (main loop) plus bars inside an open position window (pyramid sweep). This
    is ~3-4× faster than scanning every bar and matches prod semantics:
    bot.main scans coin once per cycle, opens base, then on subsequent cycles
    while position is open it can add pyramid levels when unrealized_R from
    last entry >= PYRAMID_TRIGGER_R.
    """
    df, df_h = load_prod(path)
    if df is None:
        return [], "skip:no_data"
    src = "1h_full"
    slip = SLIP.get(coin, SLIP_DEFAULT)
    trades: list[dict] = []

    PYR_ENABLED = settings.pyramid_enabled
    PYR_TRIGGER_R = settings.pyramid_trigger_r
    PYR_MAX_LEVELS = settings.max_pyramid_levels

    force_long = coin in FORCE_LONG_COINS
    force_short = coin in FORCE_SHORT_COINS

    open_until_idx = -1  # main loop blocks while a chain is open
    i = 60
    n = len(df) - 1
    while i < n:
        if i <= open_until_idx:
            i += 1
            continue
        t = _try_emit_signal(df, df_h, coin, i, slip, force_long, force_short,
                             allow_patterns=None, sig_dir_filter=None)
        if t is None:
            i += 1
            continue
        # Base entry accepted.
        t['_pyr_level'] = 1
        t['_is_addon'] = False
        trades.append(t)
        chain_root_open_ts = t['open_ts']
        chain_dir = t['direction']
        last_entry = t['entry']
        last_initial_risk = t['initial_risk']
        chain_max_exit_idx = t['exit_idx']

        # Pyramid sweep over [base.open_idx+1, base.exit_idx-1].
        if PYR_ENABLED and PYR_MAX_LEVELS > 1:
            level = 1
            allowed_pyr_patterns = None  # any non-excluded
            j = t['exit_idx']  # use as `start scanning here` → +1
            scan_lo = t['open_idx'] + 1
            scan_hi = t['exit_idx']  # exclusive
            j = scan_lo
            while j < scan_hi and level < PYR_MAX_LEVELS:
                addon = _try_emit_signal(
                    df, df_h, coin, j, slip, force_long, force_short,
                    allow_patterns=None, sig_dir_filter=chain_dir,
                )
                if addon is None:
                    j += 1
                    continue
                # Pyramid pattern exclusion.
                if addon['pattern'] in PYRAMID_EXCLUDED_PATTERNS:
                    j += 1
                    continue
                # Unrealized R from LAST entry at this signal's mark (= addon entry).
                mark = addon['entry']
                if chain_dir == 'long':
                    unreal_R = (mark - last_entry) / last_initial_risk
                else:
                    unreal_R = (last_entry - mark) / last_initial_risk
                if unreal_R < PYR_TRIGGER_R:
                    j += 1
                    continue
                level += 1
                addon['_pyr_level'] = level
                addon['_is_addon'] = True
                addon['_chain_root_open_ts'] = chain_root_open_ts
                trades.append(addon)
                last_entry = addon['entry']
                last_initial_risk = addon['initial_risk']
                if addon['exit_idx'] > chain_max_exit_idx:
                    chain_max_exit_idx = addon['exit_idx']
                # Continue from addon's open_idx+1 — но не выходить за base exit_idx.
                j = addon['open_idx'] + 1

        open_until_idx = chain_max_exit_idx
        i = chain_max_exit_idx + 1
    return trades, src


# --- Pool worker (must be top-level for Windows spawn) ---
def _gen_one(coin: str) -> tuple[str, list[dict], str]:
    """Returns (coin, trades, src_or_skip)."""
    p = resolve_path(coin)
    if p is None:
        return coin, [], "skip:no_cached_file"
    try:
        trs, src = gen(coin, p)
    except Exception as e:
        return coin, [], f"skip:engine_err:{type(e).__name__}"
    return coin, trs, src


def main():
    # --- Sanity print ---
    print("=" * 72, flush=True)
    print(f"KF HONEST REPLAY  env_source={ENV_SOURCE}", flush=True)
    print(f"  exchange={settings.exchange}  exit_mode={EXIT_MODE}  buf={STRUCT_BUFFER}", flush=True)
    print(f"  WORKING_TF={PROD_WORKING_TF}  HIGHER_TF={PROD_HIGHER_TF}", flush=True)
    print(f"  RISK={settings.risk_per_trade}  MIN_RR={MIN_RR}  MIN_RR_CT={MIN_RR_CT}", flush=True)
    print(f"  LOW_LEV<={os.environ.get('LOW_LEV_THRESHOLD')} → {os.environ.get('LOW_LEV_MIN_RR')}  "
          f"MID_LEV<={os.environ.get('MID_LEV_THRESHOLD')} → {os.environ.get('MID_LEV_MIN_RR')}", flush=True)
    print(f"  PATTERN_MIN_RR={dict(PATTERN_MIN_RR)}", flush=True)
    print(f"  (concentration cap removed 2026-05-08)  MAX_MM_PCT={MM_CAP}", flush=True)
    print(f"  FORCE_LONG_COINS={FORCE_LONG_COINS}  FORCE_SHORT_COINS={FORCE_SHORT_COINS}", flush=True)
    print(f"  SKIP_FLAG_SHORT={SKIP_FLAG_SHORT}", flush=True)
    print(f"  KF_DATA_DIR={KF_DATA_DIR}  KF_START_TS={KF_START_TS or '∅'}  KF_END_TS={KF_END_TS or '∅'}", flush=True)
    print(f"  MAX_SL_PCT={MAX_SL_PCT}  (1.0 = no cap)", flush=True)
    print(f"  COINS in .env: {len(COIN_LIST)}  smoke={sorted(SMOKE) if SMOKE else 'no'}", flush=True)
    print(f"  Slippage map loaded: {len(SLIP)} coins, default={SLIP_DEFAULT*100:.3f}% per side", flush=True)
    print("=" * 72, flush=True)

    workers = int(os.environ.get("KF_WORKERS", "1"))
    workers = max(1, min(workers, len(COIN_LIST))) if COIN_LIST else 1
    print(f"\nWorkers={workers}  (Pool spawn: each builds own BTC trend series)", flush=True)

    # --- Run sweep ---
    t0 = time.time()
    all_trades: list[dict] = []
    sources: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    attempted = 0

    if workers == 1:
        results_iter = (_gen_one(c) for c in COIN_LIST)
    else:
        from multiprocessing import Pool
        pool = Pool(workers)
        results_iter = pool.imap_unordered(_gen_one, COIN_LIST)

    for coin, trs, src in results_iter:
        attempted += 1
        if src.startswith("skip"):
            skipped.append((coin, src.split(":", 1)[1]))
        else:
            sources[coin] = src
            all_trades.extend(trs)
        if attempted % 5 == 0 or attempted == len(COIN_LIST):
            print(f"  {attempted}/{len(COIN_LIST)}  coin={coin}  src={src}  "
                  f"trades_so_far={len(all_trades)}  elapsed={time.time()-t0:.0f}s",
                  flush=True)

    if workers > 1:
        pool.close()
        pool.join()

    print(f"\nGenerated {len(all_trades)} raw signals across {len(sources)} coins "
          f"in {time.time()-t0:.1f}s", flush=True)
    return all_trades, sources, skipped, attempted


if __name__ == "__main__":
    all_trades, sources, skipped, attempted = main()

    # --- Optional time-window filter (KF_START_TS / KF_END_TS) ---
    # NB: only filters which signals get ACCEPTED into the portfolio sim. BTC
    # regime EMA200 still uses the full candle history for warmup, so the
    # filter doesn't disturb pre-conditions inside scan/gen.
    if KF_START_TS or KF_END_TS:
        before = len(all_trades)
        # All open_ts are tz-aware UTC; force start/end to UTC to avoid tz-naive comparison.
        start_pd = pd.Timestamp(KF_START_TS).tz_localize("UTC") if KF_START_TS else None
        end_pd   = pd.Timestamp(KF_END_TS  ).tz_localize("UTC") if KF_END_TS   else None
        def _in_window(t):
            ts = pd.Timestamp(t['open_ts'])
            if ts.tzinfo is None: ts = ts.tz_localize("UTC")
            if start_pd is not None and ts < start_pd: return False
            if end_pd   is not None and ts > end_pd:   return False
            return True
        all_trades = [t for t in all_trades if _in_window(t)]
        print(f"  Time-window filter [{KF_START_TS or '−∞'} … {KF_END_TS or '+∞'}]: "
              f"{before} → {len(all_trades)} signals", flush=True)

    # --- Apply caps chronologically (with pyramid handled in gen() + inline equity) ---
    # Prod-fidelity mirrors bot/trader.py + bot/main.py (post-2026-05-11):
    #   • single-coin lock OR pyramid_addon_trigger (PYRAMID_ENABLED=true) — done in gen()
    #   • cascade: N losses within window
    #   • MM cap: total margin / ACCOUNT < MM_CAP
    # dd_halt RIPPED from prod 2026-05-11 — NOT applied here.
    all_trades.sort(key=lambda x: x['open_ts'])
    accepted: list[dict] = []
    rejected_mm = 0
    rejected_cascade = 0
    cascade_window = pd.Timedelta(minutes=CASCADE_WINDOW_MIN) if CASCADE_WINDOW_MIN > 0 else None

    PYR_ENABLED = settings.pyramid_enabled
    PYR_TRIGGER_R = settings.pyramid_trigger_r
    PYR_MAX_LEVELS = settings.max_pyramid_levels

    # Open positions: each entry = dict with close_ts (str), margin, was_loss,
    # coin, direction, level, entry, initial_risk, eq_at_accept, pnl_R_net,
    # risk_mult. We keep both a flat list (for MM cap) and per-(coin,dir) view.
    open_pos: list[dict] = []
    open_by_cd: dict[tuple[str, str], list[dict]] = {}
    closed_history: list[tuple] = []  # (close_ts: pd.Timestamp, was_loss: bool)

    eq = ACCOUNT
    peak = ACCOUNT
    max_dd = 0.0

    def _close_expired(now_ts_pd: pd.Timestamp) -> None:
        """Move positions whose close_ts <= now into closed_history; update equity (compounding)."""
        global eq, peak, max_dd, open_pos
        new_open = []
        for op in open_pos:
            if pd.Timestamp(op['close_ts']) > now_ts_pd:
                new_open.append(op)
            else:
                closed_history.append((pd.Timestamp(op['close_ts']), op['was_loss']))
                pnl = op['pnl_R_net'] * (op['eq_at_accept'] * settings.risk_per_trade * op['risk_mult']) * op.get('cap_ratio', 1.0)
                eq += pnl
                peak = max(peak, eq)
                dd = 100 * (peak - eq) / peak if peak > 0 else 0.0
                if dd > max_dd:
                    max_dd = dd
                # Drop from per-(coin,dir) view
                cd = (op['coin'], op['direction'])
                if cd in open_by_cd:
                    open_by_cd[cd] = [x for x in open_by_cd[cd] if x is not op]
                    if not open_by_cd[cd]:
                        del open_by_cd[cd]
        open_pos = new_open

    # Track which chains were accepted/rejected so addons inherit base outcome.
    accepted_chain_roots: set[str] = set()
    rejected_chain_roots: set[str] = set()
    rejected_addon_chain = 0

    for t in all_trades:
        ts_pd = pd.Timestamp(t['open_ts'])

        # 1. Realize closes that happened before this signal — updates eq/peak/max_dd.
        _close_expired(ts_pd)

        is_addon = bool(t.get('_is_addon'))
        chain_root = t.get('_chain_root_open_ts') if is_addon else t['open_ts']

        # 2. Chain-rejection propagation: if base wasn't accepted, addon can't fire either.
        if is_addon:
            if chain_root in rejected_chain_roots or chain_root not in accepted_chain_roots:
                rejected_addon_chain += 1
                continue

        # 3. CASCADE FILTER (N losses in window → block).
        if cascade_window is not None and CASCADE_THRESHOLD > 0:
            cutoff = ts_pd - cascade_window
            recent_losses = sum(
                1 for cts, was_loss in closed_history
                if was_loss and cutoff <= cts <= ts_pd
            )
            if recent_losses >= CASCADE_THRESHOLD:
                rejected_cascade += 1
                if not is_addon:
                    rejected_chain_roots.add(chain_root)
                continue

        # 4. MM cap (total margin across open positions).
        # Per-trade adaptive lev: thin-SL coins run higher effective leverage,
        # so margin draw is much smaller than the old LEV=10 estimate.
        cur_m = sum(op['margin'] for op in open_pos)
        eff_lev = _adaptive_lev(t['stop_pct'])
        margin = t['notional'] / eff_lev
        if (cur_m + margin) / ACCOUNT > MM_CAP:
            rejected_mm += 1
            if not is_addon:
                rejected_chain_roots.add(chain_root)
            continue

        # 6. Accept.
        op = {
            'close_ts': t['close_ts'],
            'margin': margin,
            'was_loss': t['pnl_R_net'] <= 0,
            'coin': t['coin'],
            'direction': t['direction'],
            'level': int(t.get('_pyr_level', 1)),
            'entry': float(t['entry']),
            'initial_risk': float(t['initial_risk']),
            'eq_at_accept': eq,
            'pnl_R_net': t['pnl_R_net'],
            'risk_mult': t['risk_mult'],
            'cap_ratio': float(t.get('cap_ratio', 1.0)),
        }
        open_pos.append(op)
        cd = (t['coin'], t['direction'])
        open_by_cd.setdefault(cd, []).append(op)
        t['_eq_at_accept'] = eq
        if not is_addon:
            accepted_chain_roots.add(chain_root)
        accepted.append(t)

    # 7. Realize remaining open positions at end-of-history.
    end_ts = pd.Timestamp(max((op['close_ts'] for op in open_pos), default=str(pd.Timestamp.utcnow())))
    _close_expired(end_ts + pd.Timedelta(seconds=1))

    # 8. Mark-to-market MaxDD using intra-trade worst troughs.
    # Above accept-loop tracks `max_dd` on REALIZED equity (close-to-close).
    # Live bot MM%/equity sees unrealized PnL of every open position every tick,
    # so realized-only DD systematically under-counts intra-trade valleys.
    # See scripts/_equity_sim.py + feedback_backtest_intrabar_maxdd.md.
    _mtm_summary = simulate_with_intrabar_dd(accepted, ACCOUNT, settings)
    max_dd_realized = max_dd  # close-only (legacy view)
    max_dd = _mtm_summary['max_dd_pct']  # intra-bar (true)

    n = len(accepted)
    addons_n = sum(1 for x in accepted if x.get('_is_addon'))
    base_n = n - addons_n
    print(f"\n=== Caps applied ===", flush=True)
    print(f"  Total raw signals: {len(all_trades)}  (base={sum(1 for t in all_trades if not t.get('_is_addon'))}, addons={sum(1 for t in all_trades if t.get('_is_addon'))})", flush=True)
    print(f"  Rejected by cascade ({CASCADE_THRESHOLD} losses in {CASCADE_WINDOW_MIN}m): {rejected_cascade}", flush=True)
    print(f"  Rejected addons (chain-base rejected): {rejected_addon_chain}", flush=True)
    print(f"  Rejected by MM cap ({MM_CAP*100:.0f}%): {rejected_mm}", flush=True)
    print(f"  Accepted: {n}  (base={base_n}, pyramid_addons={addons_n})", flush=True)

    # --- Aggregate stats over accepted ---
    gross_R = 0.0
    fees_R = 0.0
    net_R = 0.0
    wins = 0

    if n > 0:
        for t in accepted:
            gross_R += t['pnl_R_gross']
            fees_R += t['fee_R']
            net_R += t['pnl_R_net']
            if t['pnl_R_net'] > 0:
                wins += 1
        final_eq = eq
        win_rate = 100.0 * wins / n
        ts_min = pd.to_datetime(min(t['open_ts'] for t in accepted))
        ts_max = pd.to_datetime(max(t['close_ts'] for t in accepted))
        days = (ts_max - ts_min).days
        years = max(days / 365.25, 0.1)
        cagr = ((final_eq / ACCOUNT) ** (1 / years) - 1) * 100
    else:
        final_eq = ACCOUNT
        win_rate = 0.0
        ts_min = None
        ts_max = None
        days = 0
        years = 0
        cagr = 0.0

    # --- Per-pattern + per-coin top10 ---
    by_pat: dict[str, list[dict]] = defaultdict(list)
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for t in accepted:
        by_pat[t['pattern']].append(t)
        by_coin[t['coin']].append(t)


    def stats(trs: list[dict]) -> dict:
        if not trs:
            return {'n': 0}
        nn = len(trs)
        sumR = sum(t['pnl_R_net'] for t in trs)
        w = [t['pnl_R_net'] for t in trs if t['pnl_R_net'] > 0]
        losses = [t['pnl_R_net'] for t in trs if t['pnl_R_net'] <= 0]
        return {
            'n': nn,
            'win_rate': round(100 * len(w) / nn, 2),
            'avg_R': round(sumR / nn, 4),
            'sum_R': round(sumR, 2),
            'avg_win': round(sum(w) / max(len(w), 1), 3),
            'avg_loss': round(sum(losses) / max(len(losses), 1), 3),
        }


    per_pattern = []
    for pat, trs in sorted(by_pat.items()):
        s = stats(trs)
        s['pattern'] = pat
        per_pattern.append(s)

    per_coin_all = []
    for coin, trs in by_coin.items():
        s = stats(trs)
        s['coin'] = coin
        per_coin_all.append(s)
    per_coin_all.sort(key=lambda r: -r.get('sum_R', 0))
    per_coin_top10 = per_coin_all[:10]

    # --- Build output JSON ---
    out = {
        'exchange': 'kraken',
        'git_head': _resolve_git_head(),
        'env_source': ENV_SOURCE,
        'history_window': {
            'start': str(ts_min) if ts_min is not None else None,
            'end': str(ts_max) if ts_max is not None else None,
            'days': int(days),
        },
        'config_resolved': {
            'risk_per_trade': settings.risk_per_trade,
            'min_rr': MIN_RR,
            'min_rr_countertrend': MIN_RR_CT,
            'low_lev_threshold': int(os.environ.get('LOW_LEV_THRESHOLD', '3')),
            'low_lev_min_rr': float(os.environ.get('LOW_LEV_MIN_RR', '1.5')),
            'mid_lev_threshold': int(os.environ.get('MID_LEV_THRESHOLD', '5')),
            'mid_lev_min_rr': float(os.environ.get('MID_LEV_MIN_RR', '1.1')),
            'pattern_min_rr': dict(PATTERN_MIN_RR),
            'max_margin_used_pct': MM_CAP,
            'exit_mode': EXIT_MODE,
            'struct_buffer_pct': STRUCT_BUFFER,
            'force_long_coins': sorted(FORCE_LONG_COINS),
            'force_short_coins': sorted(FORCE_SHORT_COINS),
            'fee_rt': FEE_RT,
            'slippage_default_per_side': SLIP_DEFAULT,
            'leverage_cap': LEV_CAP,
            'liq_sl_buffer': LIQ_SL_BUFFER,
            'mm_rate_approx': MM_RATE,
            'leverage_mode': 'adaptive_per_trade (safe_lev = min(cap, 1/((1+buf)*sl + mmrate)))',
            'account_starting_usd': ACCOUNT,
            'max_sl_pct': MAX_SL_PCT,
            'working_tf': _dl.PROD_WORKING_TF,
            'higher_tf': _dl.PROD_HIGHER_TF,
            # 2026-05-17 honest overrides applied
            'min_sl_dist_pct': MIN_SL_DIST_PCT,
            'slippage_floor_per_side': SLIP_FLOOR,
            'funding_cost_annual_pct': FUNDING_COST_ANNUAL,
            'honest_overrides_applied': _HONEST_APPLIED,
        },
        'coins_attempted': attempted,
        'coins_with_data': len(sources),
        'data_sources': sources,
        'coins_skipped': [{'coin': c, 'reason': r} for c, r in skipped],
        'caps_applied': {
            'raw_signals': len(all_trades),
            'raw_base': sum(1 for t in all_trades if not t.get('_is_addon')),
            'raw_addons': sum(1 for t in all_trades if t.get('_is_addon')),
            'rejected_mm_cap': rejected_mm,
            'rejected_cascade': rejected_cascade,
            'rejected_addon_chain': rejected_addon_chain,
            'pyramid_addons': sum(1 for x in accepted if x.get('_is_addon')),
            'accepted': n,
        },
        'pyramid': {
            'enabled': PYR_ENABLED,
            'trigger_R': PYR_TRIGGER_R,
            'max_levels': PYR_MAX_LEVELS,
            'excluded_patterns': sorted(PYRAMID_EXCLUDED_PATTERNS),
        },
        'summary': {
            'trades': n,
            'gross_R': round(gross_R, 2),
            'fees_R': round(fees_R, 2),
            'net_R': round(net_R, 2),
            'cagr_pct': round(cagr, 2),
            'max_dd_pct': round(max_dd, 2),               # intra-bar (mark-to-market)
            'max_dd_realized_pct': round(max_dd_realized, 2),  # close-only (legacy)
            'maxdd_method': _mtm_summary['method'],
            'win_rate': round(win_rate, 2),
            'final_equity_usd': round(final_eq, 2),
        },
        'per_pattern': per_pattern,
        'per_coin_top10': per_coin_top10,
        'trades': accepted,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w') as f:
        json.dump(out, f, default=str, indent=2)

    # --- 10-line console summary ---
    print()
    print("=" * 60)
    print("  KF HONEST REPLAY — SUMMARY (real prod .env)")
    print("=" * 60)
    print(f"  Window:    {out['history_window']['start']} → {out['history_window']['end']}  ({days}d / {years:.2f}y)")
    print(f"  Coins:     attempted={attempted}  with_data={len(sources)}  skipped={len(skipped)}")
    print(f"  Trades:    raw={len(all_trades)}  accepted={n}  win_rate={win_rate:.1f}%")
    print(f"  R:         gross={gross_R:+.1f}  fees={fees_R:.1f}  net={net_R:+.1f}")
    print(f"  Equity:    ${ACCOUNT:,} → ${final_eq:,.0f}  ({100*(final_eq/ACCOUNT-1):+.1f}%)")
    print(f"  CAGR:      {cagr:+.2f}%/yr   Max DD: {max_dd:.2f}% intra-bar  ({max_dd_realized:.2f}% realized)")
    print(f"  Output:    {OUT_PATH}")
    print("=" * 60)
