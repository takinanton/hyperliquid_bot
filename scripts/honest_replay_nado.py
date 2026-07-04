"""Nado honest replay backtest using LIVE .env from the live VPS.

ENV SOURCE (2026-05-10): live `kraken-bot:/root/nado_bot/.env` через ssh.
Snapshots под `data/backtest_params_*/envs/` ЗАПРЕЩЕНЫ как default. См.
scripts/_live_env.py.

Mirrors KF harness; differences:
  - Strips `-PERP` suffix from Nado coin names to match HL/disk-cache files.
  - No HIP-3 (Vertex universe is plain perps), no funding gate.
  - Nado-specific fee 0.10% RT (taker 0.05% × 2 sides).
  - Default candle dir = `hl_1y` as PROXY (Nado data not yet cached on Mac).
"""
from __future__ import annotations  # PEP 563 — defer type-hint eval for py3.9 compat
import warnings; warnings.filterwarnings('ignore')
import os, sys, json, time, subprocess
from collections import defaultdict
from pathlib import Path

HL_ROOT = os.environ.get("HL_ROOT", "/Users/ak/Desktop/HL")
DATA_DIR = os.path.join(HL_ROOT, "data")

sys.path.insert(0, os.path.join(HL_ROOT, "scripts"))
# NOTE: removed `sys.path.insert(0, HL_ROOT/hyperliquid_bot)` 2026-05-12.
# activate_live_bot("nado") below puts /tmp/live_bots/nado at sys.path[0];
# adding hyperliquid_bot/ here would let stale Mac `bot/` shadow live bot/.
from _live_env import load_live_env, load_env_file  # noqa: E402
from _live_bot import activate_live_bot  # noqa: E402

ENV_FILE_OVERRIDE = os.environ.get("NADO_ENV_FILE", "").strip()
if ENV_FILE_OVERRIDE:
    ENV_SOURCE = f"file:{ENV_FILE_OVERRIDE}"
    load_env_file(ENV_FILE_OVERRIDE)
else:
    ENV_SOURCE = "live:kraken-bot:nado_bot"
    load_live_env("nado")

# 2026-05-17: overlay honest-backtest constants on live env (aborts if missing).
# Per memory feedback_live_env_vs_backtest_overrides.md.
from _honest_overrides import apply_honest_overrides  # noqa: E402
_HONEST_APPLIED = apply_honest_overrides()


# Pull live bot/ from nado-bot VPS (/root/nado_bot/bot/). Without this,
# this script reads stale Mac `hyperliquid_bot/bot/` which doesn't even
# contain nado-specific patches.
activate_live_bot("nado")

SMOKE = set((os.environ.get("SMOKE_COINS") or "").split(",")) - {""}

OUT_PATH = os.environ.get(
    "NADO_OUT_PATH",
    os.path.join(DATA_DIR, "honest_replay", "nado.json"),
)

from _data_loader import load_prod, load_slippage, PROD_WORKING_TF, PROD_HIGHER_TF  # noqa: E402


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

import pandas as pd  # noqa: E402

from bot.config import (  # noqa: E402
    Settings, PATTERN_MIN_RR,
    FORCE_LONG_COINS, FORCE_SHORT_COINS,
)
from bot.patterns_v2 import detect_all_v2  # noqa: E402
from bot.vstop_structure import find_structure_exit  # noqa: E402
from bot.swings import find_swings  # noqa: E402

def find_struct_vstop_exit(df, entry_idx, direction, initial_stop,
                           buffer_pct=0.003, max_holding_bars=200):
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

ACCOUNT = float(os.environ.get("ACCOUNT_USD", "50000"))
LEV = 10
FEE_RT = 0.0010  # Nado: 0.05% taker × 2 = 0.10% RT (matches KF)

settings = Settings.from_env()
MIN_RR = settings.min_rr
MIN_RR_CT = settings.min_rr_countertrend
EXIT_MODE = os.environ.get("EXIT_MODE", "vstop_struct")
STRUCT_BUFFER = settings.struct_buffer_pct

MM_CAP = float(os.environ.get("MAX_MM_PCT", os.environ.get("MAX_MARGIN_USED_PCT", "0.50")))

# -------------------------------------------------------------------------
# PROD-PARITY DECLARATION (see bot/prod_gates.py).
# -------------------------------------------------------------------------
APPLIED_GATES: set[str] = {
    "force_long_short_coins", "min_rr",
    "pattern_min_rr", "open_trade_per_coin",
    "vstop_trailing",
    "max_margin_used_pct", "slippage_and_fees", "whitelist_coins",
    "max_opens_per_day_cycle",
    "btc_regime_filter",
    "liquidity_drag_check",
    "cold_combo_cap",
    "cascade_filter",
    "liquidity_tier_mult",
    "liquidity_cap_notional",  # 2026-05-11: bot/trader.py:315 — notional ≤ (vol_4h/4) / MIN_1H_LIQ_RATIO
}

# 2026-05-08: LONG_ONLY purged.
SKIP_FLAG_SHORT = os.environ.get("SKIP_FLAG_SHORT", "false").lower() in ("true", "1", "yes")

EXEMPT_GATES: dict[str, str] = {
    "already_traded_dedup":     "single-coin lock approximates it",
    "adaptive_rr_by_leverage":  "no per-coin max_leverage carried in candles",
    "force_trend_override":     "operator override — not a backtest concern",
    "min_notional":             "Nado min_notional≈$16 — backtested coins clear it",
    "funding_blocked":          "Nado is non-perp Vertex — no funding rate to block on",
    "hip3_market_hours":        "N/A on Nado (no HIP-3)",
    "hip3_min_sl_distance":     "N/A on Nado (no HIP-3)",
    "liq_vs_sl_buffer":         "no liquidation simulation — vstop trails SL anyway",
    "pattern_boost_mult":       "empty in current .env — re-check before next deploy",
    "signal_drift_freshness":   "trivial in backtest — entry == bar close",
    "dd_halt":                  "no rolling-DD halt sim — backtest may include trades prod would block",
    "pyramid_addon_trigger":    "no concurrent-position pyramid sim; per-trade R is level-1 stats only",
}

try:
    assert_backtest_parity(APPLIED_GATES, exempt=EXEMPT_GATES.keys(), strict=True)
    print(f"[prod_gates] parity OK — applied={len(APPLIED_GATES)}  "
          f"exempt={len(EXEMPT_GATES)} (TODO/known)", flush=True)
except ParityError as e:
    print(f"\n{e}\n", flush=True)
    print("[prod_gates] update APPLIED_GATES/EXEMPT_GATES in honest_replay_nado.py", flush=True)
    raise SystemExit(2)

# Nado data: not yet cached on Mac. Use HL 1h dataset as proxy by default;
# override NADO_DATA_DIR + NADO_DATA_FILE_SUFFIX once native cache exists.
NADO_DATA_DIR = os.environ.get("NADO_DATA_DIR", "hl_1y")
NADO_DATA_FILE_SUFFIX = os.environ.get("NADO_DATA_FILE_SUFFIX", ".json")
USING_PROXY = NADO_DATA_DIR == "hl_1y"

SLIP_FILE = os.path.join(DATA_DIR, "hl_slippage.json")  # proxy file
SLIP_DEFAULT = 0.0005
SLIP = load_slippage(SLIP_FILE)

NADO_START_TS = os.environ.get("NADO_START_TS", "").strip()
NADO_END_TS = os.environ.get("NADO_END_TS", "").strip()

CASCADE_WINDOW_MIN = int(os.environ.get("CASCADE_WINDOW_MIN", "60"))
CASCADE_THRESHOLD = int(os.environ.get("CASCADE_THRESHOLD", "3"))

LIQUIDITY_TIER_HIGH_USD = float(os.environ.get("LIQUIDITY_TIER_HIGH_USD", "2000000"))
LIQUIDITY_TIER_MID_USD = float(os.environ.get("LIQUIDITY_TIER_MID_USD", "300000"))
LIQUIDITY_RISK_MULT_HIGH = float(os.environ.get("LIQUIDITY_RISK_MULT_HIGH", "2.0"))
LIQUIDITY_RISK_MULT_MID = float(os.environ.get("LIQUIDITY_RISK_MULT_MID", "1.5"))
# Adaptive sizing cap (bot/trader.py:310-315): notional <= hourly_vol_est / ratio. Nado has no HIP-3.
MIN_1H_LIQ_RATIO = float(os.environ.get("MIN_1H_LIQUIDITY_RATIO", "5"))


def _strip_perp(coin: str) -> str:
    """`BTC-PERP` → `BTC`; `kPEPE-PERP` → `kPEPE`. Used to match HL proxy data."""
    return coin[:-5] if coin.endswith("-PERP") else coin


_EMA_LENGTH = 200

def _build_btc_trend_series() -> "pd.Series":
    btc_path = os.path.join(DATA_DIR, NADO_DATA_DIR, f"BTC{NADO_DATA_FILE_SUFFIX}")
    if not os.path.exists(btc_path):
        print(f"[btc_regime] WARN no BTC candles at {btc_path} — filter inert", flush=True)
        return pd.Series(dtype=object)
    _, df_btc = load_prod(btc_path)
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


# Nado universe — env COINS is the source of truth; strip -PERP for path match.
RAW_COINS = os.environ.get("COINS", "").strip()
COIN_LIST: list[str] = []
if RAW_COINS:
    for raw in RAW_COINS.split(","):
        sym = raw.strip()
        if not sym:
            continue
        # Nado ticks are like "BTC-PERP"; strip suffix to match data files.
        COIN_LIST.append(_strip_perp(sym))
else:
    # Fallback: list disk
    try:
        disk = sorted(
            f.replace(NADO_DATA_FILE_SUFFIX, "")
            for f in os.listdir(os.path.join(DATA_DIR, NADO_DATA_DIR))
            if f.endswith(NADO_DATA_FILE_SUFFIX)
        )
        COIN_LIST = disk
        print(f"[universe] disk fallback → {len(COIN_LIST)} coins", flush=True)
    except Exception as exc:
        print(f"[universe] disk listing failed: {exc}", flush=True)

# de-dupe preserving order
seen = set()
COIN_LIST = [c for c in COIN_LIST if not (c in seen or seen.add(c))]
if SMOKE:
    COIN_LIST = [c for c in COIN_LIST if c in SMOKE]


def resolve_path(coin: str) -> str | None:
    primary = os.path.join(DATA_DIR, NADO_DATA_DIR, f"{coin}{NADO_DATA_FILE_SUFFIX}")
    return primary if os.path.exists(primary) else None


def gen(coin: str, path: str) -> tuple[list[dict], str]:
    df, df_h = load_prod(path)
    if df is None:
        return [], "skip:no_data"
    src = "1h_full"
    slip = SLIP.get(coin, SLIP_DEFAULT)

    trades: list[dict] = []
    open_until = None

    force_long = coin in FORCE_LONG_COINS
    force_short = coin in FORCE_SHORT_COINS

    for i in range(60, len(df) - 1):
        ts = df['time'].iloc[i]
        if open_until is not None and ts < open_until:
            continue
        df_higher = df_h[df_h['time'] <= ts]
        if df_higher.empty or len(df_higher) < 30:
            continue
        df_curr = df.iloc[: i + 1]
        try:
            sigs = detect_all_v2(df_curr, df_higher, coin=coin, timeframe=PROD_WORKING_TF)
        except Exception:
            continue
        if not sigs:
            continue
        sig = sigs[0]
        if SKIP_FLAG_SHORT and sig.pattern == 'flag_short':
            continue
        if force_long and sig.direction != 'long':
            continue
        if force_short and sig.direction != 'short':
            continue
        btc_blocked, _ = is_btc_regime_blocked(sig.pattern, coin, _btc_trend_at(ts))
        if btc_blocked:
            continue
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
            continue
        # Liquidity drag
        stop_pct_pre = abs(sig.entry - sig.stop_loss) / sig.entry if sig.entry > 0 else 0.0
        if stop_pct_pre > 0:
            drag_R = (FEE_RT + 2 * slip) / stop_pct_pre
            if (sig.rr - drag_R) < settings.min_rr:
                continue
        try:
            exit_idx, exit_px, exit_reason = find_struct_vstop_exit(
                df, i, sig.direction, sig.stop_loss,
                buffer_pct=STRUCT_BUFFER, max_holding_bars=200,
            )
        except Exception:
            continue
        risk = abs(sig.entry - sig.stop_loss)
        if risk <= 0:
            continue
        stop_pct = risk / sig.entry
        cold_mult, _bt_n = cold_combo_risk_mult(sig.pattern, coin)
        # Liquidity tier mult
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
        ip = False
        rmult = cold_mult * liquidity_risk_mult
        notional_uncapped = ACCOUNT * settings.risk_per_trade * rmult / stop_pct

        # Adaptive liquidity cap (mirror bot/trader.py:310-315).
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
        # Intra-trade worst trough — enables mark-to-market MaxDD (see _equity_sim.py).
        worst_R, worst_ts = compute_worst_trough(df, i, exit_idx, sig.direction, sig.entry, risk)
        trades.append({
            'pattern': sig.pattern, 'coin': coin, 'direction': sig.direction,
            'is_premium': bool(ip),
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
        })
        open_until = df['time'].iloc[exit_idx]
    return trades, src


def _gen_one(coin: str) -> tuple[str, list[dict], str]:
    p = resolve_path(coin)
    if p is None:
        return coin, [], "skip:no_cached_file"
    try:
        trs, src = gen(coin, p)
    except Exception as e:
        return coin, [], f"skip:engine_err:{type(e).__name__}"
    return coin, trs, src


def main():
    print("=" * 72, flush=True)
    print(f"NADO HONEST REPLAY  env_source={ENV_SOURCE}", flush=True)
    print(f"  exchange={settings.exchange}  exit_mode={EXIT_MODE}  buf={STRUCT_BUFFER}", flush=True)
    print(f"  WORKING_TF={PROD_WORKING_TF}  HIGHER_TF={PROD_HIGHER_TF}", flush=True)
    print(f"  RISK={settings.risk_per_trade}  MIN_RR={MIN_RR}  MIN_RR_CT={MIN_RR_CT}", flush=True)
    print(f"  PATTERN_MIN_RR={dict(PATTERN_MIN_RR)}", flush=True)
    print(f"  MAX_MM_PCT={MM_CAP}  fee_rt={FEE_RT*100:.3f}% (Nado taker × 2)", flush=True)
    print(f"  SKIP_FLAG_SHORT={SKIP_FLAG_SHORT}", flush=True)
    print(f"  NADO_DATA_DIR={NADO_DATA_DIR}  proxy={USING_PROXY}", flush=True)
    print(f"  COINS in .env: {len(COIN_LIST)}  smoke={sorted(SMOKE) if SMOKE else 'no'}", flush=True)
    print("=" * 72, flush=True)

    workers = int(os.environ.get("NADO_WORKERS", "1"))
    workers = max(1, min(workers, len(COIN_LIST))) if COIN_LIST else 1
    print(f"\nWorkers={workers}", flush=True)

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

    if NADO_START_TS or NADO_END_TS:
        before = len(all_trades)
        start_pd = pd.Timestamp(NADO_START_TS).tz_localize("UTC") if NADO_START_TS else None
        end_pd   = pd.Timestamp(NADO_END_TS  ).tz_localize("UTC") if NADO_END_TS   else None
        def _in_window(t):
            ts = pd.Timestamp(t['open_ts'])
            if ts.tzinfo is None: ts = ts.tz_localize("UTC")
            if start_pd is not None and ts < start_pd: return False
            if end_pd   is not None and ts > end_pd:   return False
            return True
        all_trades = [t for t in all_trades if _in_window(t)]
        print(f"  Time-window filter: {before} → {len(all_trades)} signals", flush=True)

    all_trades.sort(key=lambda x: x['open_ts'])
    accepted: list[dict] = []
    open_pos: list[tuple[str, float, bool]] = []
    closed_history: list[tuple] = []
    rejected_mm = 0
    rejected_cascade = 0
    cascade_window = pd.Timedelta(minutes=CASCADE_WINDOW_MIN) if CASCADE_WINDOW_MIN > 0 else None

    for t in all_trades:
        ts = t['open_ts']
        ts_pd = pd.Timestamp(ts)
        new_open: list[tuple[str, float, bool]] = []
        for c, m, was_loss in open_pos:
            if c > ts:
                new_open.append((c, m, was_loss))
            else:
                closed_history.append((pd.Timestamp(c), was_loss))
        open_pos = new_open

        if cascade_window is not None and CASCADE_THRESHOLD > 0:
            cutoff = ts_pd - cascade_window
            recent_losses = sum(
                1 for cts, was_loss in closed_history
                if was_loss and cutoff <= cts <= ts_pd
            )
            if recent_losses >= CASCADE_THRESHOLD:
                rejected_cascade += 1
                continue

        cur_m = sum(m for _, m, _ in open_pos)
        margin = t['notional'] / LEV
        if (cur_m + margin) / ACCOUNT > MM_CAP:
            rejected_mm += 1
            continue
        accepted.append(t)
        open_pos.append((t['close_ts'], margin, t['pnl_R_net'] <= 0))

    n = len(accepted)
    print(f"\n=== Caps applied ===", flush=True)
    print(f"  Total raw: {len(all_trades)}", flush=True)
    print(f"  Rejected by cascade ({CASCADE_THRESHOLD} losses in {CASCADE_WINDOW_MIN}m): {rejected_cascade}", flush=True)
    print(f"  Rejected by MM cap ({MM_CAP*100:.0f}%): {rejected_mm}", flush=True)
    print(f"  Accepted: {n}", flush=True)

    eq = ACCOUNT
    peak = ACCOUNT
    max_dd = 0.0
    gross_R = 0.0
    fees_R = 0.0
    net_R = 0.0
    wins = 0

    if n > 0:
        for t in accepted:
            t['_eq_at_accept'] = eq
            pnl = t['pnl_R_net'] * (eq * settings.risk_per_trade * t['risk_mult']) * t.get('cap_ratio', 1.0)
            eq += pnl
            peak = max(peak, eq)
            dd = 100 * (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
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

    # Mark-to-market MaxDD using intra-trade worst troughs.
    # Realized-only DD (above) systematically under-counts intra-trade valleys
    # because equity updates only on close. See scripts/_equity_sim.py +
    # feedback_backtest_intrabar_maxdd.md.
    _mtm_summary = simulate_with_intrabar_dd(accepted, ACCOUNT, settings)
    max_dd_realized = max_dd  # close-only (legacy)
    max_dd = _mtm_summary['max_dd_pct']  # intra-bar (true)

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

    out = {
        'exchange': 'nado',
        'data_proxy': USING_PROXY,  # True = ran on HL data; False = native Nado cache
        'data_dir': NADO_DATA_DIR,
        'env_source': ENV_SOURCE,
        'git_head': _resolve_git_head(),
        'history_window': {
            'start': str(ts_min) if ts_min is not None else None,
            'end': str(ts_max) if ts_max is not None else None,
            'days': int(days),
        },
        'config_resolved': {
            'risk_per_trade': settings.risk_per_trade,
            'min_rr': MIN_RR,
            'min_rr_countertrend': MIN_RR_CT,
            'pattern_min_rr': dict(PATTERN_MIN_RR),
            'max_margin_used_pct': MM_CAP,
            'exit_mode': EXIT_MODE,
            'struct_buffer_pct': STRUCT_BUFFER,
            'force_long_coins': sorted(FORCE_LONG_COINS),
            'force_short_coins': sorted(FORCE_SHORT_COINS),
            'fee_rt': FEE_RT,
            'slippage_default_per_side': SLIP_DEFAULT,
            'leverage_assumed': LEV,
            'account_starting_usd': ACCOUNT,
        },
        'coins_attempted': attempted,
        'coins_with_data': len(sources),
        'data_sources': sources,
        'coins_skipped': [{'coin': c, 'reason': r} for c, r in skipped],
        'caps_applied': {
            'raw_signals': len(all_trades),
            'rejected_mm_cap': rejected_mm,
            'rejected_cascade': rejected_cascade,
            'accepted': n,
        },
        'summary': {
            'trades': n,
            'gross_R': round(gross_R, 2),
            'fees_R': round(fees_R, 2),
            'net_R': round(net_R, 2),
            'cagr_pct': round(cagr, 2),
            'max_dd_pct': round(max_dd, 2),
            'max_dd_realized_pct': round(max_dd_realized, 2),
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

    print()
    print("=" * 60)
    print("  NADO HONEST REPLAY — SUMMARY"
          + ("  [PROXY: HL data]" if USING_PROXY else ""))
    print("=" * 60)
    print(f"  Window:    {out['history_window']['start']} → {out['history_window']['end']}  ({days}d / {years:.2f}y)")
    print(f"  Coins:     attempted={attempted}  with_data={len(sources)}  skipped={len(skipped)}")
    print(f"  Trades:    raw={len(all_trades)}  accepted={n}  win_rate={win_rate:.1f}%")
    print(f"  R:         gross={gross_R:+.1f}  fees={fees_R:.1f}  net={net_R:+.1f}")
    print(f"  Equity:    ${ACCOUNT:,} → ${final_eq:,.0f}  ({100*(final_eq/ACCOUNT-1):+.1f}%)")
    print(f"  CAGR:      {cagr:+.2f}%/yr   Max DD: {max_dd:.2f}% intra-bar  ({max_dd_realized:.2f}% realized)")
    print(f"  Output:    {OUT_PATH}")
    print("=" * 60)
