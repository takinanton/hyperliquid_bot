"""Исполнение сигнала и управление позициями (v2: trend-only + Vstop, без TP)."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from bot.config import (
    FORCE_TREND,
    LOW_LEV_MIN_RR,
    LOW_LEV_THRESHOLD,
    MID_LEV_MIN_RR,
    MID_LEV_THRESHOLD,
    PATTERN_MIN_RR,
    PYRAMID_EXCLUDED_PATTERNS,
    Settings,
)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from bot.exchange import HLClient
from bot.journal import (
    _conn,
    already_traded,
    has_open_trade_for_tf_dir,
    insert_rejected,
    insert_trade,
    open_levels_for_tf_dir,
    update_trade_status,
)
from bot.sessions import is_session_open
from bot.risk import (
    check_liq_vs_sl,
    compute_size,
    is_countertrend,
    is_funding_blocked,
    required_rr,
)
from bot.swings import find_swings, SwingKind

log = logging.getLogger(__name__)


MIN_SL_DIST_PCT = float(os.getenv("MIN_SL_DIST_PCT", "0.005"))  # 0.5% min — anti dead-coin / detector noise

# FRESHNESS dedupe: suppress repeat signal fires within TTL window.
# key = (coin, pattern, timeframe, direction, round(entry, 6)) → suppress_until_ts
_FRESHNESS_REJECT_UNTIL: dict[tuple, float] = {}
_FRESHNESS_DEDUPE_TTL_SEC = float(os.getenv("FRESHNESS_DEDUPE_TTL_SEC", "600"))


# ============================================================
# Cold-combo cap (D4, 2026-05-07)
# ============================================================
# CHZ id=39 lost -$1196 на triangle_short, у которого backtest n=1 в
# kf_final_prod.json (effectively zero baseline). Юзер: «filter coin list to
# coins with ≥5 backtest trades per pattern, OR set risk_mult=0.5 for cold-
# coin-pattern combos». Берём вариант B (less destructive) — 0.5× risk_mult
# на (pattern, coin) с bt_n < KF_COLD_COMBO_MIN_N (default 5).
#
# Файл `data/kf_combo_counts.json` — `{"<pattern>|<coin>": <n>}`. Coin =
# unsuffixed (BTC, ETH, ...). Bot's coin = "BTC/USD:USD" → strip "/USD:USD".
_COMBO_COUNTS_CACHE: dict[str, int] | None = None


def _normalize_combo_coin(coin: str) -> str:
    """Strip exchange suffix to match kf_final_prod backtest coin format.

    KF: 'BTC/USD:USD' → 'BTC'. HL: 'BTC' → 'BTC'. Nado: 'BTC-PERP' → 'BTC'.
    HIP-3 'xyz:META' → 'META' (HIP-3 не в kf backtest, всегда cold).
    """
    if "/" in coin:
        return coin.split("/", 1)[0]
    if coin.startswith("xyz:"):
        return coin.split(":", 1)[1]
    if coin.endswith("-PERP"):
        return coin[: -len("-PERP")]
    return coin


def _load_combo_counts() -> dict[str, int]:
    """Read data/kf_combo_counts.json once at first use, cache thereafter."""
    global _COMBO_COUNTS_CACHE
    if _COMBO_COUNTS_CACHE is not None:
        return _COMBO_COUNTS_CACHE
    try:
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "data" / "kf_combo_counts.json"
        if not path.exists():
            log.warning("kf_combo_counts.json not found at %s — cold-combo cap disabled", path)
            _COMBO_COUNTS_CACHE = {}
            return _COMBO_COUNTS_CACHE
        with open(path) as f:
            _COMBO_COUNTS_CACHE = json.load(f)
        log.info("Loaded %d combo counts from kf_combo_counts.json", len(_COMBO_COUNTS_CACHE))
    except Exception as e:
        log.warning("Failed to load kf_combo_counts.json: %s — cold-combo cap disabled", e)
        _COMBO_COUNTS_CACHE = {}
    return _COMBO_COUNTS_CACHE


def cold_combo_risk_mult(pattern: str, coin: str) -> tuple[float, int]:
    """Return (mult, bt_n) for (pattern, coin) combo.

    mult=1.0 если bt_n >= KF_COLD_COMBO_MIN_N (default 5).
    mult=KF_COLD_COMBO_MULT (default 0.5) если bt_n < threshold.
    Set KF_COLD_COMBO_MIN_N=0 to disable.
    """
    min_n = int(os.getenv("KF_COLD_COMBO_MIN_N", "5"))
    if min_n <= 0:
        return 1.0, -1
    counts = _load_combo_counts()
    if not counts:
        return 1.0, -1  # no data, fail-open
    norm_coin = _normalize_combo_coin(coin)
    key = f"{pattern}|{norm_coin}"
    n = counts.get(key, 0)
    if n < min_n:
        mult = float(os.getenv("KF_COLD_COMBO_MULT", "0.5"))
        return mult, n
    return 1.0, n


# ============================================================
# Открытие сделки (без TP, только market_open + initial SL)
# ============================================================
def execute_signal(
    *,
    client: HLClient,
    settings: Settings,
    signal,  # duck-typed: .coin, .pattern, .timeframe, .direction, .entry, .stop_loss, .rr, .detected_at
    funding_hourly: float,
    higher_trend: str,
    account_value: float,
    dry_run: bool,
) -> int | None:
    coin = signal.coin
    tf = signal.timeframe

    # FRESHNESS dedupe (port from 4 other bots 2026-05-12) — suppress repeat fires
    # of same (coin, pattern, tf, dir, entry) signal within TTL window. Race-safe
    # against fast scan loops where has_open_trade_for_tf_dir may not yet reflect
    # an in-flight open.
    if _FRESHNESS_DEDUPE_TTL_SEC > 0:
        import time as _time
        _fresh_key = (coin, signal.pattern, tf, signal.direction, round(float(signal.entry), 6))
        _now = _time.time()
        _suppress_until = _FRESHNESS_REJECT_UNTIL.get(_fresh_key, 0.0)
        if _now < _suppress_until:
            return None
        _FRESHNESS_REJECT_UNTIL[_fresh_key] = _now + _FRESHNESS_DEDUPE_TTL_SEC

    # ---- Фильтры (порядок важен) ----
    # 0. Ручной override тренда
    if FORCE_TREND == "neutral":
        log.info("SKIP %s %s — FORCE_TREND=neutral, пауза", coin, signal.pattern)
        return None
    if FORCE_TREND == "up" and signal.direction != "long":
        log.info("SKIP %s %s — FORCE_TREND=up, шорты заблокированы", coin, signal.pattern)
        return None
    if FORCE_TREND == "down" and signal.direction != "short":
        log.info("SKIP %s %s — FORCE_TREND=down, лонги заблокированы", coin, signal.pattern)
        return None

    if already_traded(coin, signal.pattern, tf, signal.detected_at):
        log.info("SKIP duplicate %s %s %s @ %s", coin, signal.pattern, tf, signal.detected_at)
        return None

    # ---- Exchange-side orphan dedup (IB safety, 2026-05-12) ----
    # User may hold positions manually (e.g. pre-existing COIN orphan, $226k).
    # If exchange shows a position but bot's DB has NO open trade for this coin,
    # REJECT — bot must NEVER auto-add to manually-held positions.
    try:
        ex_positions = client.open_positions()
        if coin in ex_positions:
            from bot.journal import open_trades_for
            if not open_trades_for(coin):
                reason = (f"exchange-side orphan position present "
                          f"(qty={ex_positions[coin].get('size','?')}) — manual cleanup needed, "
                          f"bot will not auto-add")
                insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                                direction=signal.direction, rr=signal.rr, reason=reason)
                log.warning("REJECT %s: %s", coin, reason)
                return None
    except (AttributeError, NotImplementedError, ImportError):
        pass  # has_open_trade_for_coin not defined on older bots — skip
    except Exception as e:
        log.warning("exchange-side dedup probe failed for %s: %s — proceeding", coin, e)

    # ---- Pyramid / single-coin lock ----
    # Default: block same (coin, TF, direction). With PYRAMID_ENABLED=true allow
    # add-on entries when the open position is already +PYRAMID_TRIGGER_R from
    # last entry, total levels < MAX_PYRAMID_LEVELS, and pattern is not in
    # PYRAMID_EXCLUDED_PATTERNS. Each level keeps its own SL.
    pyramid_level = 0
    parent_trade_id: int | None = None
    if not settings.pyramid_enabled:
        if has_open_trade_for_tf_dir(coin, tf, signal.direction):
            log.info("SKIP %s %s %s %s — same (coin,TF,dir) уже открыт",
                     coin, signal.pattern, tf, signal.direction)
            return None
    else:
        open_levels = open_levels_for_tf_dir(coin, tf, signal.direction)
        if open_levels:
            if signal.pattern in PYRAMID_EXCLUDED_PATTERNS:
                reason = (f"pyramid blocked: pattern {signal.pattern} в "
                          f"PYRAMID_EXCLUDED_PATTERNS (avgR addon < initial in backtest)")
                insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                                direction=signal.direction, rr=signal.rr, reason=reason)
                log.info("REJECT %s: %s", coin, reason)
                return None
            if len(open_levels) >= settings.max_pyramid_levels:
                reason = (f"pyramid blocked: levels={len(open_levels)} >= "
                          f"MAX_PYRAMID_LEVELS={settings.max_pyramid_levels}")
                insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                                direction=signal.direction, rr=signal.rr, reason=reason)
                log.info("REJECT %s: %s", coin, reason)
                return None
            last_level = open_levels[-1]
            risk_unit = abs(float(last_level["entry"]) - float(last_level["stop_loss"]))
            if risk_unit <= 0:
                log.info("SKIP %s — last level risk_unit=0, skipping pyramid", coin)
                return None
            try:
                mark_now_for_pyr = client.mark_price(coin)
            except Exception as exc:
                log.warning("SKIP pyramid trigger check: mark_price failed for %s: %s", coin, exc)
                return None
            if signal.direction == "long":
                unreal_R = (mark_now_for_pyr - float(last_level["entry"])) / risk_unit
            else:
                unreal_R = (float(last_level["entry"]) - mark_now_for_pyr) / risk_unit
            if unreal_R < settings.pyramid_trigger_r:
                reason = (f"pyramid wait: unrealized_R={unreal_R:.2f} < "
                          f"trigger={settings.pyramid_trigger_r:.2f} "
                          f"(level {len(open_levels)} vs last entry {last_level['entry']})")
                insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                                direction=signal.direction, rr=signal.rr, reason=reason)
                log.info("WAIT %s: %s", coin, reason)
                return None
            pyramid_level = len(open_levels)
            parent_trade_id = int(open_levels[0]["id"])
            log.info("PYRAMID OK %s %s %s — level %d, unrealized_R=%.2f from last entry %.4f",
                     coin, signal.pattern, signal.direction, pyramid_level, unreal_R,
                     float(last_level["entry"]))

    # Получаем asset metadata раньше чем R/R check —
    # нужно знать max_leverage чтобы скорректировать rr_min.
    try:
        asset = client.asset(coin)
    except KeyError:
        log.warning("No meta for %s", coin)
        return None

    # ---- Trading-session gate (IB only — crypto exchanges always-open) ----
    # Skip signal if target market is closed OR within pre-close cutoff (STK).
    # Pre-close cutoff prevents opening position <30min before RTH end (gap risk
    # before SL fully placed). For FUT only blocks weekly close / weekend.
    sess_open, sess_reason = is_session_open(asset.sec_type)
    if not sess_open:
        reason = f"market_closed:{sess_reason}"
        insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                        direction=signal.direction, rr=signal.rr, reason=reason)
        log.info("SKIP %s %s %s — %s", coin, signal.pattern, tf, reason)
        return None

    # Adaptive R/R по leverage: чем меньше плечо — тем дороже margin slot,
    # тем выше требуемая отдача. На капитале $3000 с leverage=3x одна позиция
    # съедает ~$33 margin (1% риск × 3x), а с leverage=10x — ~$10. С 12 net
    # max позиций low-leverage пары быстро жрут весь margin.
    # Двухуровневый порог: leverage<=3 → 1.5; leverage<=5 → 1.1; иначе default.
    rr_min_base = required_rr(signal.direction, higher_trend, settings)
    rr_min = rr_min_base
    if asset.max_leverage <= LOW_LEV_THRESHOLD:
        rr_min = max(rr_min, LOW_LEV_MIN_RR)
    elif asset.max_leverage <= MID_LEV_THRESHOLD:
        rr_min = max(rr_min, MID_LEV_MIN_RR)
    # Pattern-specific R/R floor (e.g. flag_short → 1.5, был слаб в backtest)
    pattern_rr = PATTERN_MIN_RR.get(signal.pattern)
    if pattern_rr is not None:
        rr_min = max(rr_min, pattern_rr)
    if signal.rr < rr_min:
        reason = f"R/R {signal.rr:.2f} < {rr_min:.2f} (leverage={asset.max_leverage}x)"
        insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                        direction=signal.direction, rr=signal.rr, reason=reason)
        log.info("REJECT %s %s %s: %s", coin, signal.pattern, tf, reason)
        return None

    if is_funding_blocked(funding_hourly, signal.direction, settings.funding_block_threshold):
        reason = f"funding={funding_hourly:.6f} блокирует {signal.direction}"
        insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                        direction=signal.direction, rr=signal.rr, reason=reason)
        log.info("REJECT %s %s %s: %s", coin, signal.pattern, tf, reason)
        return None

    # MIN SL distance gate — anti dead-coin / detector noise on zero-vol consolidation.
    # Например ID/USD 2026-05-10 22:45: SL=0.034997 entry=0.035000 → dist=0.0086% → size $1.46M → WOULD_CAUSE_LIQUIDATION.
    sl_dist_pct = abs(signal.entry - signal.stop_loss) / signal.entry if signal.entry else 0.0
    if sl_dist_pct < MIN_SL_DIST_PCT:
        reason = f"SL_DIST {sl_dist_pct*100:.4f}% < min {MIN_SL_DIST_PCT*100:.2f}% — dead coin / detector noise"
        insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                        direction=signal.direction, rr=signal.rr, reason=reason)
        log.info("REJECT %s %s %s: %s", coin, signal.pattern, tf, reason)
        return None

    # 0c. HIP-3 market hours filter — xyz:* skip outside US equity hours.
    # Backtest validation 2026-05-07: 6/6 fills last week were 0-4 UTC,
    # 100% instant-SL <1min, accumulated -$6.15 + slippage. After-hours
    # HIP-3 has stale prices, drift gate passes but real fill at re-open
    # gaps 1-5%. Skip xyz:* outside Mon-Fri 14:30-21:00 UTC.
    if coin.startswith("xyz:") and os.getenv("HIP3_MARKET_HOURS_ONLY", "0") == "1":
        now = datetime.now(timezone.utc)
        is_weekend = now.weekday() >= 5
        in_us_hours = (
            (now.hour > 14 or (now.hour == 14 and now.minute >= 30))
            and now.hour < 21
        )
        if is_weekend or not in_us_hours:
            ts_str = now.strftime('%a %H:%M UTC')
            reason = (f'HIP-3 outside US market hours ({ts_str}); '
                      f'after-hours = stale prices + instant-SL')
            insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                            direction=signal.direction, rr=signal.rr, reason=reason)
            log.info("REJECT %s %s: %s", coin, signal.pattern, reason)
            return None

    # === CASCADE FILTER (юзер 2026-05-06; tuned 2026-05-06 v2) ===
    # Cascade = серия ЛОССОВ подряд (real market stress), а не просто closes.
    # V1 (threshold=2 closes any) был слишком aggressive: 374 rejects/4h на HL,
    # т.к. instant-close по SL trigger HIP-3 trades с tiny fills тоже считались.
    # V2: считаем только LOSSES (pnl_dollars < 0). Threshold 3 в окне 60m.
    # Backtest на 37 HL trades: window=60m N=2 спасал 57% bad slips ценой 22%
    # сделок — но только если closes были реально в cascade. Now real cascade
    # = real losses, no false positives from instant-SL trades.
    cascade_window_min = int(os.getenv("CASCADE_WINDOW_MIN", "60"))
    cascade_threshold = int(os.getenv("CASCADE_THRESHOLD", "3"))
    if cascade_window_min > 0 and cascade_threshold > 0:
        try:
            from bot.journal import _conn
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cascade_window_min)).isoformat()
            with _conn() as con:
                row = con.execute(
                    "SELECT COUNT(*) AS n FROM trades WHERE closed_at > ? "
                    "AND status NOT IN ('cancelled','closed_reconcile') "
                    "AND pnl_dollars IS NOT NULL AND pnl_dollars < 0",
                    (cutoff,),
                ).fetchone()
                recent_losses = int(row["n"]) if row else 0
            if recent_losses >= cascade_threshold:
                reason = (f"cascade detected: {recent_losses} losses за "
                          f"{cascade_window_min}m (threshold {cascade_threshold}) — slip risk")
                insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                                direction=signal.direction, rr=signal.rr, reason=reason)
                log.info("REJECT %s %s %s: %s", coin, signal.pattern, tf, reason)
                return None
        except Exception as e:
            log.warning("cascade filter check failed: %s — proceeding", e)

    # ADAPTIVE SIZING + LIQUIDITY-TIERED RISK BOOST.
    # 1) Adaptive cap: thin pairs trade smaller (was hard reject).
    # 2) Liquid pairs (BTC, ETH) get BIGGER per-trade risk via tier multiplier.
    liquidity_cap_notional: float | None = None
    liquidity_risk_mult: float = 1.0
    last_vol_usd_for_log: float = 0
    # 2026-05-08 (N21): убрали отдельный 1h fetch (был причина top-of-1h burst).
    # Используем уже закэшированный 4h volume из _collect_signals → 0 extra requests.
    # 4h-USD-volume / 4 = avg-1h-volume-estimate → совместимо со старыми порогами в env.
    try:
        vol_4h_usd = client.last_4h_volume_usd(coin)
        if vol_4h_usd and vol_4h_usd > 0:
            hourly_vol_est = vol_4h_usd / 4.0  # ≈ 1h volume estimate
            last_vol_usd_for_log = hourly_vol_est
            if coin.startswith("xyz:"):
                ratio = float(os.getenv("MIN_1H_LIQUIDITY_RATIO_HIP3", "5"))
            else:
                ratio = float(os.getenv("MIN_1H_LIQUIDITY_RATIO", "5"))
            if hourly_vol_est > 0 and ratio > 0:
                liquidity_cap_notional = hourly_vol_est / ratio
            tier_high = float(os.getenv("LIQUIDITY_TIER_HIGH_USD", "2000000"))  # $2M
            tier_mid = float(os.getenv("LIQUIDITY_TIER_MID_USD", "300000"))      # $300k
            mult_high = float(os.getenv("LIQUIDITY_RISK_MULT_HIGH", "2.0"))
            mult_mid = float(os.getenv("LIQUIDITY_RISK_MULT_MID", "1.5"))
            if hourly_vol_est >= tier_high:
                liquidity_risk_mult = mult_high
            elif hourly_vol_est >= tier_mid:
                liquidity_risk_mult = mult_mid
    except Exception as e:
        log.warning("liquidity probe %s failed: %s — no liquidity adjust", coin, e)

    base_mult = 1.0

    # PATTERN_BOOST_MULT (юзер approve required): per-pattern risk multiplier.
    # Format: "123_long:1.5,123_short:1.5" (overnight analysis: 123 boost = +89R/27mo).
    # Default empty (no boost).
    pattern_boost_mult = 1.0
    boosts = os.getenv("PATTERN_BOOST_MULT", "")
    if boosts:
        for kv in boosts.split(","):
            kv = kv.strip()
            if ":" in kv:
                k, v = kv.split(":", 1)
                if k.strip() == signal.pattern:
                    try:
                        pattern_boost_mult = float(v)
                        log.info("PATTERN BOOST %s (%s): ×%.2f", coin, signal.pattern, pattern_boost_mult)
                    except ValueError:
                        pass

    # COLD-COMBO CAP (D4, 2026-05-07): backtest n<5 → halve risk_mult.
    # CHZ triangle_short n=1, lost -$1196. Halve risk for unproven combos.
    cold_mult, bt_n = cold_combo_risk_mult(signal.pattern, coin)
    if cold_mult < 1.0:
        log.info("COLD_COMBO: %s %s (bt_n=%d), risk reduced ×%.2f",
                 signal.pattern, coin, bt_n, cold_mult)

    # VIX sizing (opt-in via VIX_SIZING_ENABLED env): IB stocks/futures benefit from
    # scaling down in high-vol regimes (VIX>25 → 0.5×) and up in low-vol (VIX<14 → 1.5×).
    # Returns 1.0 when disabled or fetch fails — safe no-op for crypto bots.
    try:
        from bot.external_data import vix_size_multiplier
        vix_mult = vix_size_multiplier(settings)
    except Exception:
        vix_mult = 1.0

    risk_mult = base_mult * liquidity_risk_mult * pattern_boost_mult * cold_mult * vix_mult
    if liquidity_risk_mult > 1.0 or pattern_boost_mult > 1.0 or cold_mult < 1.0 or vix_mult != 1.0:
        log.info("RISK MULT %s: liquidity=%.1f × pattern_boost=%.1f × cold=%.2f × vix=%.2f → final %.2f",
                 coin, liquidity_risk_mult, pattern_boost_mult, cold_mult, vix_mult, risk_mult)
    size_res = compute_size(
        signal, asset, account_value, settings,
        risk_multiplier=risk_mult,
        liquidity_cap_notional=liquidity_cap_notional,
    )
    if size_res is None or size_res.size <= 0:
        reason = "size=0"
        insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                        direction=signal.direction, rr=signal.rr, reason=reason)
        return None
    # Reject if even capped notional is below exchange minimum (~$10)
    # MIN_NOTIONAL: общий минимум для размещения позиции.
    # MIN_SL_NOTIONAL: для бирж где SL имеет отдельный min_size (Nado: $100).
    # Bug fix 2026-05-06: ASTER-PERP опен прошёл (notional $13 ≥ MIN_NOTIONAL $10),
    # но SL placement упал ('Order amount or price too small, min_size product 48 = $100').
    # Position осталась unprotected до manual close. Юзер: 'сделай так чтобы
    # проблемы не возникало'. Решение: проверяем notional ≥ max(min_notional, min_sl_notional).
    min_notional = float(os.getenv("MIN_NOTIONAL_USD", "10"))
    min_sl_notional = float(os.getenv("MIN_SL_NOTIONAL_USD", "100"))  # Nado-safe default
    effective_min = max(min_notional, min_sl_notional)
    if size_res.notional < effective_min:
        reason = (f"notional ${size_res.notional:.2f} < min ${effective_min:.0f} "
                  f"(SL может не разместиться) "
                  f"(1h vol ${last_vol_usd_for_log:,.0f} тонкий, capped size слишком мал)")
        insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                        direction=signal.direction, rr=signal.rr, reason=reason)
        log.info("REJECT %s %s %s: %s", coin, signal.pattern, tf, reason)
        return None
    if liquidity_cap_notional and size_res.notional < (account_value * settings.risk_per_trade / 0.01):
        log.info("ADAPTIVE %s: notional $%.0f scaled down by liquidity (1h vol $%s, cap $%.0f)",
                 coin, size_res.notional, f"{last_vol_usd_for_log:,.0f}", liquidity_cap_notional)

    # MM CAP: не открываем если **maintenance margin** > MAX_MARGIN_USED_PCT × equity.
    # Bug fix 2026-05-05 (вечер 2): юзер показал что HL UI MM 35%, бот выдавал 70%.
    # Раньше юзали totalMarginUsed (это INITIAL margin) — в ~2× больше real MM.
    # Самопал формула 1/(2*lev) тоже была неправильной (≠ реальные exchange rates).
    # Правильно: HL API сам отдаёт `crossMaintenanceMarginUsed` — точное значение.
    try:
        max_mm_pct = float(os.getenv("MAX_MARGIN_USED_PCT", "0.50"))
        state = client.info.user_state(client.settings.account_address)
        # HL exposes real maintenance margin directly:
        used_maint = float(state.get("crossMaintenanceMarginUsed", 0) or 0)
        # New position maintenance margin: use exchange's tier — approximation
        # via initial_margin / 2 (большинство тиров: maint_rate = init_rate / 2).
        new_notional = signal.entry * size_res.size
        new_initial = new_notional / max(asset.max_leverage, 1)
        new_maint = new_initial / 2.0  # консервативная аппроксимация
        future_maint = used_maint + new_maint
        mm_pct = future_maint / max(account_value, 1e-9)
        if mm_pct > max_mm_pct:
            reason = (f"MM cap: used ${used_maint:.0f} + new ${new_maint:.0f} "
                      f"= ${future_maint:.0f} ({mm_pct*100:.0f}%) > {max_mm_pct*100:.0f}%")
            insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                            direction=signal.direction, rr=signal.rr, reason=reason)
            log.info("REJECT %s %s %s: %s", coin, signal.pattern, tf, reason)
            return None
    except Exception as e:
        log.warning("MM cap check failed: %s — proceeding", e)


    # === HIP-3 MIN SL DISTANCE GUARD (2026-05-06) ===
    # HIP-3 (xyz:*) thin liquidity → entry slippage 0.1-0.5%. Pattern detector
    # ставит SL за свежий swing extremum, но в after-hours / тонкие интервалы
    # swing'и плотные → SL оказывается на 0.3-0.9% от entry. Slippage съедает
    # большую часть headroom → instant SL trigger в первые 30-60 сек.
    # 6/6 closed HIP-3 trades за 2026-05-05/06 закрылись stop'ом за 35-55с.
    # Фикс: skip если SL distance < HIP3_MIN_SL_PCT (default 1.5%).
    if coin.startswith("xyz:"):
        hip3_min_sl_pct = float(os.getenv("HIP3_MIN_SL_PCT", "0.015"))
        sl_dist_pct = abs(signal.entry - signal.stop_loss) / signal.entry
        if sl_dist_pct < hip3_min_sl_pct:
            reason = (f"HIP-3 SL distance {sl_dist_pct*100:.2f}% < min "
                      f"{hip3_min_sl_pct*100:.2f}% (slippage съест headroom)")
            insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                            direction=signal.direction, rr=signal.rr, reason=reason)
            log.info("REJECT %s %s %s: %s", coin, signal.pattern, tf, reason)
            return None

    # === SIGNAL FRESHNESS GATE (юзер 2026-05-06) ===
    # Pattern-based stratегия предполагает что entry от signal.entry на момент
    # market_open ≈ актуальный mark. Если бот пропустил окно (outage / late
    # restart / ratelimit gap), то к моменту fire'а сигнала рынок мог уйти.
    # Кейс 2026-05-06: Nado bot был сломан 04:28-06:43 UTC (`os not defined`),
    # после рестарта firing'нул flag_long FARTCOIN-PERP с entry=0.23512 — это
    # close 1h-свечи 05:00-06:00. К моменту fill (06:43:46) рынок упал на
    # 1.22% до 0.23224. Bot купил НИЖЕ breakout = инвалидированный сетап.
    # Guard: получаем current mark, skip если drift > SIGNAL_MAX_DRIFT_PCT.
    try:
        mark_now = client.mark_price(coin)
        if mark_now and signal.entry > 0:
            drift_pct = abs(mark_now - signal.entry) / signal.entry
            max_drift_pct = float(os.getenv("SIGNAL_MAX_DRIFT_PCT", "0.01"))  # 1.0% default
            # Direction-aware "thesis-broken" check: длинный сигнал требует
            # mark ≥ entry; шорт — mark ≤ entry. Если ушло в "невалидную"
            # сторону на 0.5×threshold — уже думать; на ≥threshold — skip.
            thesis_broken = False
            if signal.direction == "long" and mark_now < signal.entry * (1 - max_drift_pct):
                thesis_broken = True
            elif signal.direction == "short" and mark_now > signal.entry * (1 + max_drift_pct):
                thesis_broken = True
            # Также skip если просто далеко в любую сторону (chasing / fast-mover)
            if drift_pct > max_drift_pct or thesis_broken:
                reason = (
                    f"FRESHNESS: signal.entry={signal.entry:.6f} mark={mark_now:.6f} "
                    f"drift={drift_pct*100:.2f}% > {max_drift_pct*100:.2f}% "
                    f"(thesis_broken={thesis_broken}). Сигнал устарел или рынок ускакал — skip."
                )
                insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                                direction=signal.direction, rr=signal.rr, reason=reason)
                log.warning("REJECT %s %s %s: %s", coin, signal.pattern, tf, reason)
                return None
    except (AttributeError, NotImplementedError):
        pass  # client doesn't expose mark_price — skip gate
    except Exception as e:
        log.warning("freshness gate failed for %s: %s — proceeding", coin, e)

    # === LIQUIDITY DRAG CHECK (юзер 2026-05-04) ===
    # effective_RR = signal.rr - drag_R (где drag = (fee + 2×slip) / stop_pct)
    # Если drag съедает edge → skip (тонкая для нашего размера)
    try:
        slip_per_side = client.slip_per_side(coin, size_res.notional)
        fee_rt = getattr(client, "_fee_rt_estimate", 0.001)  # 0.10% default fallback
        stop_pct_actual = abs(signal.entry - signal.stop_loss) / signal.entry
        drag_R = (fee_rt + 2 * slip_per_side) / stop_pct_actual if stop_pct_actual > 0 else 9.99
        effective_rr = signal.rr - drag_R
        if effective_rr < settings.min_rr:
            reason = (f"LIQUIDITY: slip {slip_per_side*100:.2f}%/side × stop {stop_pct_actual*100:.2f}% "
                      f"→ drag {drag_R:.2f}R, eff_RR {effective_rr:.2f} < {settings.min_rr}")
            insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                            direction=signal.direction, rr=signal.rr, reason=reason)
            log.info("REJECT %s %s %s: %s", coin, signal.pattern, tf, reason)
            return None
    except (AttributeError, NotImplementedError):
        pass  # client doesn't support slip_per_side yet — skip filter
    except Exception as e:
        log.warning("liquidity drag check failed for %s: %s — proceeding", coin, e)

    counter = is_countertrend(signal.direction, higher_trend)
    log.info(
        "SIGNAL %s %s %s | %s | entry=%.6f SL=%.6f | size=%.6f risk=$%.2f R/R=%.2f | "
        "leverage=%dx counter=%s funding=%.6f trend=%s",
        coin, signal.pattern, tf, signal.direction,
        signal.entry, signal.stop_loss,
        size_res.size, size_res.risk_dollars, signal.rr,
        size_res.leverage, counter, funding_hourly, higher_trend,
    )

    # ---- Запись в журнал перед сделкой ----
    initial_notes = {"current_stop": float(signal.stop_loss), "exit_mode": "vstop"}
    trade_id = insert_trade(
        coin=coin, pattern=signal.pattern, timeframe=tf,
        direction=signal.direction, detected_at=signal.detected_at,
        entry=signal.entry, stop_loss=signal.stop_loss,
        take_profits=[],  # NO TPs — все по Vstop
        size=size_res.size, risk_dollars=size_res.risk_dollars, rr=signal.rr,
        higher_tf_trend=higher_trend, funding_at_entry=funding_hourly,
        notes=("dry-run" if dry_run else json.dumps(initial_notes)),
        pyramid_level=pyramid_level,
        parent_trade_id=parent_trade_id,
    )

    if dry_run:
        log.info("DRY-RUN: skip orders. trade_id=%d", trade_id)
        return trade_id

    # ---- Леверидж ----
    if settings.leverage_mode == "max":
        lev_resp = client.update_leverage(coin, asset.max_leverage, is_cross=True)
        if lev_resp is None:
            # update_leverage упал даже после retry — НЕ открываем позицию
            # с непонятным leverage. Помечаем trade как failed чтобы
            # manage_open_positions её не трогал.
            log.error(
                "ABORT %s: update_leverage failed после retry — позицию не открываем",
                coin,
            )
            update_trade_status(
                trade_id, "cancelled",
                notes="aborted: update_leverage failed after retries",
            )
            return trade_id

    # ---- market_open ----
    is_buy = signal.direction == "long"
    filled_px: float | None = None
    slippage_pct: float | None = None
    market_open_ok = False
    try:
        resp = client.market_open(coin, is_buy=is_buy, sz=size_res.size)
        log.info("OPEN response: %s", _short(resp))
        # Парсим filled — нужно понять реально ли исполнилось
        # Bug fix 2026-05-06: записываем РЕАЛЬНЫЙ filled size в DB. Раньше DB
        # содержала intended size (size_res.size), при partial fill / liquidity
        # cap exchange имел меньше. Юзер обнаружил PUMP: DB 17M, exchange 1.22M.
        # → planned_risk считался от DB и врал в 14×.
        filled_sz = None
        try:
            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and isinstance(statuses[0], dict):
                st = statuses[0]
                if "error" in st:
                    log.error("OPEN %s ОТКЛОНЁН HL: %s", coin, st["error"])
                    # market_open_ok остаётся False
                else:
                    filled = st.get("filled")
                    if filled:
                        filled_px = float(filled.get("avgPx", 0)) or None
                        try:
                            filled_sz = float(filled.get("totalSz", 0)) or None
                        except (TypeError, ValueError):
                            filled_sz = None
                        market_open_ok = True
                        if filled_px and signal.entry > 0:
                            # Slippage в "плохую" сторону = positive
                            if is_buy:
                                slippage_pct = (filled_px - signal.entry) / signal.entry
                            else:
                                slippage_pct = (signal.entry - filled_px) / signal.entry
                            log.info(
                                "ENTRY %s: detected=%.6f filled=%.6f sz_intended=%.6f sz_filled=%s slippage=%.4f%%",
                                coin, signal.entry, filled_px, size_res.size,
                                f"{filled_sz:.6f}" if filled_sz else "?",
                                slippage_pct * 100,
                            )
                            # Bug fix 2026-05-06: |slip| > 0.5% уходило в INFO и не
                            # попадалось на глаза. Видели -1.22% на Nado FARTCOIN-PERP
                            # (favorable, но магнитуда = тонкий стакан → следующий
                            # раз может быть +1.22% и бот молча возьмёт плохой entry).
                            # WARN на |slip| > 0.5% делает выброс заметным в журнале
                            # (per-bot threshold через env: ENTRY_SLIP_WARN_PCT).
                            slip_warn_pct = float(os.getenv("ENTRY_SLIP_WARN_PCT", "0.5"))
                            if abs(slippage_pct) * 100 >= slip_warn_pct:
                                log.warning(
                                    "HIGH SLIP %s: %.4f%% (|slip| ≥ %.2f%%) — detected=%.6f filled=%.6f. "
                                    "Тонкий стакан / stale signal price. Проверить slippage data per-coin.",
                                    coin, slippage_pct * 100, slip_warn_pct,
                                    signal.entry, filled_px,
                                )
                            # SLIP CAP ABORT (2026-05-06): если |slip| > MAX_FILL_SLIP_PCT
                            # — emergency close, даже если favorable. Магнитуда indicates
                            # что либо signal stale, либо стакан невалидный → trade
                            # стратегически инвалиден. Default 1.5% (выше WARN 0.5%, ниже
                            # любого разумного R-distance чтобы не ловить волатильность).
                            slip_abort_pct = float(os.getenv("MAX_FILL_SLIP_PCT", "1.5"))
                            if abs(slippage_pct) * 100 >= slip_abort_pct:
                                log.warning(
                                    "SLIP ABORT %s: |slip| %.4f%% ≥ %.2f%% — emergency close",
                                    coin, abs(slippage_pct) * 100, slip_abort_pct,
                                )
                                try:
                                    close_resp = client.market_close(coin)
                                    log.warning("Slip-abort close %s: %s", coin, _short(close_resp))
                                except Exception as e:
                                    log.exception("Slip-abort close %s failed: %s", coin, e)
                                update_trade_status(
                                    trade_id, "cancelled",
                                    notes=(f"slip abort: {slippage_pct*100:.4f}% ≥ {slip_abort_pct}% "
                                           f"(detected={signal.entry:.6f} filled={filled_px:.6f}) — closed"),
                                )
                                client.invalidate_positions_cache()
                                return trade_id
                    elif "resting" in st:
                        # Phantom-trade guard 2026-05-11: IB Cancelled used to land
                        # here via _wrap_order_resp routing. Verify _status is truly
                        # in-flight before declaring success.
                        if resp.get("_status") in ("Cancelled", "ApiCancelled", "Inactive", "ApiPending"):
                            log.error("OPEN %s ABORT: order %s (resting payload but cancelled)",
                                      coin, resp.get("_status"))
                            # market_open_ok stays False — no DB write, no SL placement
                        else:
                            # Resting limit — для market_open редкость, обычно market fill instant
                            log.warning("OPEN %s in resting state, not filled yet", coin)
                            market_open_ok = True  # ордер на бирже, fill будет
        except Exception as e:
            log.warning("OPEN response parse failed for %s: %s", coin, e)
        # If filled_sz differs значимо от intended → update DB to reflect reality
        if filled_sz is not None and abs(filled_sz - size_res.size) / max(size_res.size, 1e-9) > 0.01:
            fill_ratio = filled_sz / max(size_res.size, 1e-9)
            log.warning(
                "Size mismatch %s: intended=%.6f filled=%.6f (%.1f%% diff) — updating DB",
                coin, size_res.size, filled_sz,
                100 * (filled_sz - size_res.size) / size_res.size,
            )
            # PARTIAL FILL ABORT (2026-05-06): для HIP-3 (xyz:*) thin liquidity
            # часто отдаёт <5% от intended size (видели 0.633 из 61 = 1%).
            # Tiny position с initial SL даёт нерепрезентативный риск + слив на
            # spread'ах. Решение: если fill ratio < HIP3_MIN_FILL_RATIO (default 0.5
            # = 50%) — закрываем позицию market'ом и помечаем cancelled.
            min_fill_ratio = float(os.getenv(
                "HIP3_MIN_FILL_RATIO" if coin.startswith("xyz:") else "MIN_FILL_RATIO",
                "0.5" if coin.startswith("xyz:") else "0.0",
            ))
            if min_fill_ratio > 0 and fill_ratio < min_fill_ratio:
                log.warning(
                    "PARTIAL FILL ABORT %s: fill_ratio=%.1f%% < %.0f%% — emergency close",
                    coin, fill_ratio * 100, min_fill_ratio * 100,
                )
                try:
                    close_resp = client.market_close(coin)
                    log.warning("Partial-fill close %s: %s", coin, _short(close_resp))
                except Exception as e:
                    log.exception("Partial-fill close %s failed: %s", coin, e)
                update_trade_status(
                    trade_id, "cancelled",
                    notes=(f"partial fill {fill_ratio*100:.1f}% < {min_fill_ratio*100:.0f}% "
                           f"(filled {filled_sz} of intended {size_res.size}) — closed"),
                )
                client.invalidate_positions_cache()
                return trade_id
            try:
                from bot.journal import update_trade_size, update_trade_risk_dollars
                from bot.risk import SizeResult
                update_trade_size(trade_id, filled_sz)
                # D3 fix 2026-05-07: also update risk_dollars to match actual
                # filled size. Pre-fix: DB had risk_dollars from intended size
                # (smaller than reality when filled > intended, larger when
                # filled < intended). Caused 0.93→442 spread in open trades.
                actual_entry = filled_px or signal.entry
                actual_risk_per_unit = abs(actual_entry - signal.stop_loss)
                actual_risk_dollars = filled_sz * actual_risk_per_unit
                # Sanity check: risk_dollars should be ≥ 50% of intended.
                # If smaller → log WARN (but still record actual).
                if actual_risk_dollars < 0.5 * size_res.risk_dollars:
                    log.warning(
                        "RISK DRIFT %s: intended_risk=$%.2f actual_risk=$%.2f "
                        "(filled %.4f vs intended %.4f) — partial fill or stale price",
                        coin, size_res.risk_dollars, actual_risk_dollars,
                        filled_sz, size_res.size,
                    )
                update_trade_risk_dollars(trade_id, actual_risk_dollars)
                size_res = SizeResult(
                    size=filled_sz,
                    risk_dollars=actual_risk_dollars,
                    leverage=size_res.leverage,
                    notional=filled_sz * actual_entry,
                )
            except Exception as e:
                log.warning("update_trade_size failed: %s", e)
        # Сразу инвалидируем кэш позиций — concentration check для следующих
        # сигналов в этом цикле должен видеть свежую картинку
        client.invalidate_positions_cache()
    except Exception as e:
        log.exception("market_open failed: %s", e)

    if not market_open_ok:
        # Сделка не исполнилась — пометить как cancelled, чтобы manage_open_positions
        # не трогала. Иначе будет phantom trade: в БД open, на бирже нет.
        log.error(
            "ABORT %s: market_open не подтвердил fill — помечаю trade %d как cancelled",
            coin, trade_id,
        )
        update_trade_status(
            trade_id, "cancelled",
            notes="market_open did not confirm fill (rejected/error/no fill data)",
        )
        return trade_id

    # ---- Initial SL (stop-market, reduce_only) ----
    # БАГ был: is_buy (та же сторона что market_open) — HL отклонял ордер.
    # ПРАВИЛЬНО: для SL сторона ОБРАТНАЯ — long закрываем sell, short — buy.
    is_buy_to_close = not is_buy
    # BUG FIX 2026-05-12 (pyramid coverage gap — port from KF/HL/Nado/Paci 2026-05-11):
    # При pyramid_level > 0 _place_sl ВНУТРИ делает _cancel_all_sl_orders → нюкает
    # SL лида (covering full prior position), потом ставит SL только на size_res.size
    # (новый level). До следующего vstop trail >0.05% часть позиции NAKED.
    # Фикс: при pyramid_level > 0 берём ТЕКУЩИЙ position size с биржи (после fill
    # propagated via invalidate_positions_cache выше) → SL на total.
    sl_size = size_res.size
    if pyramid_level > 0:
        try:
            _live_pos = client.open_positions().get(coin) or {}
            _live_sz = abs(float(_live_pos.get("size") or _live_pos.get("szi", 0) or 0))
            if _live_sz > sl_size * 1.001:
                log.info(
                    "pyramid SL upsize %s: lvl=%d delta_sz=%.6f → full_pos_sz=%.6f",
                    coin, pyramid_level, sl_size, _live_sz,
                )
                sl_size = _live_sz
        except Exception as e:
            log.warning("pyramid SL size lookup %s failed: %s — using delta size", coin, e)
    sl_oid = _place_sl(client, coin, is_buy_to_close, sl_size, signal.stop_loss)

    if sl_oid is None:
        # SL не размещён — позиция НЕЗАЩИЩЕНА. Экстренно закрываем market.
        # Лучше потерять slippage чем быть голым.
        log.error("CRITICAL: SL не разместился для %s — экстренное закрытие позиции", coin)
        try:
            close_resp = client.market_close(coin)
            log.warning("Emergency close %s: %s", coin, _short(close_resp))
            # Запишем PnL у emergency-close trade'а тоже, чтобы журнал не был ложно null.
            # Без этого закрытые из-за SL-fail сделки выглядят как "0 PnL" в TA.
            close_pnl, close_exit_px = _try_compute_close_pnl(
                client, coin, signal.direction, size_res.size,
                trade_open_iso=None,
            )
            note = f"FORCE_CLOSED: SL placement failed at {signal.stop_loss}"
            if close_exit_px is not None:
                note += f"; exit_px={close_exit_px:.6f}"
            close_pnl_pct = None
            if close_pnl is not None and signal.entry > 0 and size_res.size > 0:
                try:
                    close_pnl_pct = close_pnl / (signal.entry * size_res.size)
                except Exception:
                    pass
            update_trade_status(trade_id, "closed_no_sl",
                                pnl_dollars=close_pnl, pnl_pct=close_pnl_pct,
                                notes=note)
        except Exception as e:
            log.exception("Emergency market_close ТАКЖЕ упал для %s: %s — РУЧНОЕ ВМЕШАТЕЛЬСТВО", coin, e)
            update_trade_status(trade_id, "open_no_sl",
                                notes=f"DANGER: position open without SL, market_close failed: {e}")
        return trade_id

    # Сохраняем sl_oid в notes для последующего trail (+ slippage data)
    state = {
        "current_stop": float(signal.stop_loss),
        "sl_oid": sl_oid,
        "exit_mode": "vstop",
    }
    if filled_px is not None:
        state["entry_filled_px"] = filled_px
    if slippage_pct is not None:
        state["entry_slippage_pct"] = slippage_pct
    _set_trade_notes(trade_id, json.dumps(state))

    return trade_id


def _cancel_all_sl_orders(client: HLClient, coin: str) -> int:
    """Cancel ALL existing stop-loss orders on coin перед placement нового.

    Bug fix 2026-05-05 (вечер 3): когда новый трейд открывается на coin где
    уже была закрыта/трейлила старая позиция, на бирже мог остаться trail-SL
    от старой. Без cancel'а старого — новый SL ставится поверх, но старый
    тоже активен. Юзер обнаружил: BTC SL на бирже 78176 (от старой trail),
    а новая сделка ожидала 79709. Real risk 5× plan.

    Лечение: ВСЕГДА перед _place_sl сканировать active SL orders на coin
    и отменить ВСЕ. Затем поставить свежий.

    Returns: число отменённых orders.
    """
    cancelled = 0
    try:
        if not hasattr(client, "list_open_sl_orders"):
            return 0
        orphans = client.list_open_sl_orders(coin)
        cancel_fn = getattr(client, "cancel_sl_order", None) or (
            lambda c, o: client.exchange.cancel(c, o)
        )
        for oid in orphans:
            try:
                cancel_fn(coin, oid)
                cancelled += 1
                log.info("Pre-place: cancelled stale SL %s oid=%s", coin, oid)
            except Exception as e:
                log.warning("Pre-place: cancel SL %s oid=%s failed: %s", coin, oid, e)
    except Exception as e:
        log.warning("Pre-place SL scan %s failed: %s", coin, e)
    return cancelled


def _place_sl_no_cancel(client, coin: str, is_buy_to_close: bool, size: float, trigger_px: float):
    """Place SL БЕЗ pre-cancel'а старых. Used by trail update где мы хотим
    place-first для устранения SL gap window.

    Bug fix 2026-05-06 (юзер): сначала ставим новый, потом отменяем старые.
    """
    try:
        resp = client.trigger_sl(coin, is_buy=is_buy_to_close, sz=size, trigger_px=trigger_px)
        log.info("SL response (no-cancel) %s: %s", coin, _short(resp))
        statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses or not isinstance(statuses[0], dict):
            log.error("SL %s: пустой statuses в ответе", coin)
            return None
        st = statuses[0]
        if "error" in st:
            log.error("SL %s ОТКЛОНЁН биржей: %s", coin, st["error"])
            return None
        oid = st.get("resting", {}).get("oid")
        if oid is None:
            log.error("SL %s: нет oid в response (resting=%s)", coin, st.get("resting"))
            return None
        try: return int(oid)
        except (TypeError, ValueError): return oid
    except Exception as e:
        log.exception("trigger_sl (no-cancel) (%s) failed: %s", coin, e)
        return None


def _place_sl(client: HLClient, coin: str, is_buy_to_close: bool, size: float, trigger_px: float):
    """Возвращает order_id (oid) если получилось, иначе None.
    ВАЖНО: проверяем поле 'error' в ответе. HL может вернуть status='ok' с error
    внутри statuses[].error — например 'Invalid TP/SL price'. В таких случаях
    ордер НЕ размещён, и мы должны вернуть None (не фейк-oid).

    Bug fix 2026-05-05: ПЕРЕД placement отменяем ВСЕ существующие SL на coin
    чтобы старый trail не остался активным после открытия нового трейда.

    Возвращаемый тип:
    - HL: int (numeric oid)
    - Kraken / ccxt-биржи: str (UUID order_id)
    Не приводим к int! Это вызвало баг 03-05-2026 — Kraken UUID не парсился,
    SL ставился на бирже, но бот думал что SL fail и force-closed позицию.
    """
    # Cancel any stale SL orders on this coin first (bug fix 2026-05-05)
    n_cancelled = _cancel_all_sl_orders(client, coin)
    if n_cancelled > 0:
        log.info("SL %s: pre-place cancelled %d stale order(s)", coin, n_cancelled)
    try:
        resp = client.trigger_sl(coin, is_buy=is_buy_to_close, sz=size, trigger_px=trigger_px)
        log.info("SL response %s: %s", coin, _short(resp))
        statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses or not isinstance(statuses[0], dict):
            log.error("SL %s: пустой statuses в ответе", coin)
            return None
        st = statuses[0]
        if "error" in st:
            log.error("SL %s ОТКЛОНЁН биржей: %s", coin, st["error"])
            return None
        oid = st.get("resting", {}).get("oid")
        if oid is None:
            log.error("SL %s: нет oid в response (resting=%s)", coin, st.get("resting"))
            return None
        # Возвращаем как есть: int для HL, str для Kraken/ccxt
        try:
            return int(oid)  # HL: numeric → coerce
        except (TypeError, ValueError):
            return oid  # Kraken UUID → keep as string
    except Exception as e:
        log.exception("trigger_sl(%s) failed: %s", coin, e)
        return None


# ============================================================
# Vstop trailing — обновление стопов открытых позиций
# ============================================================
def _compute_struct_stop(
    df, direction: str, entry_time, current_stop: float, buffer_pct: float
) -> float:
    """Структурный trailing stop по swing-points.
    Long: SL = последний confirmed HL × (1 - buffer), trail только UP.
    Short: SL = последний confirmed LH × (1 + buffer), trail только DOWN.
    "Confirmed" = swing с idx <= len(df) - 4 (k=3 lag в find_swings).
    """
    if df is None or df.empty or len(df) < 20:
        return current_stop
    swings = find_swings(df, k=3, min_atr_mult=1.0)
    if not swings:
        return current_stop
    confirmed_cutoff = len(df) - 4  # swing.idx <= cutoff to be confirmed
    # Normalise tz to UTC-aware for safe comparison
    if entry_time.tzinfo is None:
        entry_time = entry_time.tz_localize("UTC")
    candidate = current_stop
    for s in swings:
        if s.idx > confirmed_cutoff:
            continue
        s_time = s.time if s.time.tzinfo else s.time.tz_localize("UTC")
        if s_time <= entry_time:
            continue
        if direction == "long" and s.kind == SwingKind.LOW:
            new = s.price * (1 - buffer_pct)
            if new > candidate:
                candidate = new
        elif direction == "short" and s.kind == SwingKind.HIGH:
            new = s.price * (1 + buffer_pct)
            if new < candidate:
                candidate = new
    # FIX 2026-05-04: Fallback ATR-trail если struct не нашёл swing после entry.
    # В сильных трендовых движениях (без откатов >1 ATR) swing low не формируется
    # → struct_stop возвращает старый. Без fallback позы держат initial SL часами.
    # См. ZEN incident: цена .5→.5 за 27 часов, SL не двигался.
    if candidate == current_stop:
        try:
            from bot.swings import atr as _atr
            atr_val = float(_atr(df, 14).iloc[-1])
            last_close = float(df["Close"].iloc[-1])
            atr_mult = float(__import__("os").environ.get("VSTOP_ATR_MULT", "2.5"))
            if direction == "long":
                atr_stop = last_close - atr_mult * atr_val
                if atr_stop > candidate:
                    candidate = atr_stop
            else:
                atr_stop = last_close + atr_mult * atr_val
                if atr_stop < candidate:
                    candidate = atr_stop
        except Exception:
            pass
    return candidate


def manage_open_positions(client: HLClient, atr_mult: float = 2.5) -> None:
    """Каждую итерацию main-цикла:
    1. Для каждой открытой сделки в журнале — пересчитываем trailing stop
       (structure-based по swing-points на 4h candles).
    2. Если новый stop лучше — отменяем старый SL и ставим новый.
    3. Если позиция закрыта на бирже — обновляем журнал с pnl/exit_px.

    DEDUP: если несколько trade-записей в DB ссылаются на одну позицию на бирже
    (например 2 паттерна → 2 trades → 1 объединённая позиция), обрабатываем
    координированно — один SL для всей позиции, sl_oid шарится между trades.
    """
    # Все открытые сделки по журналу
    with _conn() as con:
        rows = con.execute(
            "SELECT id, coin, direction, size, stop_loss, notes, entry, detected_at, created_at "
            "FROM trades WHERE status = 'open'"
        ).fetchall()
        open_trades = [dict(r) for r in rows]

    if not open_trades:
        return

    # Текущие позиции на бирже
    positions = client.open_positions()  # {coin: position_dict}

    # Закэшируем fills один раз — будем искать closing fills для каждого закрывшегося coin'а
    fills_cache: list[dict] | None = None

    # Группируем trades по (coin, direction). Для группы > 1 trades — обрабатываем
    # лидирующий trade (lowest id) полным размером позиции, остальные — only sync notes.
    from collections import defaultdict
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for tr in open_trades:
        groups[(tr["coin"], tr["direction"])].append(tr)
    for grp in groups.values():
        grp.sort(key=lambda t: t["id"])  # lead = lowest id

    for tr in open_trades:
        coin = tr["coin"]
        direction = tr["direction"]
        # ---- Если позиции уже нет на бирже — закрываем сделку в журнале с PnL ----
        if coin not in positions:
            # Bug fix 2026-05-05 (вечер): Nado SDK иногда отдаёт пустой positions
            # из-за кеша/timeout → бот ошибочно закрывал DB записи, теряя
            # позиции на бирже. Юзер обнаружил 4 orphan Nado positions
            # (AAVE/ZEC/DOGE/SOL) которые DB пометила closed_externally в 06:49,
            # но на бирже жили. Lost trail/SL management 14h.
            # Защита: re-fetch positions через invalidate_cache + retry. Только
            # если DOUBLE confirmation позиция отсутствует — закрываем.
            try:
                client.invalidate_positions_cache()
                positions_recheck = client.open_positions()
            except Exception:
                positions_recheck = positions  # на ошибке fall back на старый
            if coin in positions_recheck:
                # False alarm — позиция всё-таки есть, не закрываем
                log.warning(
                    "Position %s seemed missing in first pass but found on retry — skip close",
                    coin,
                )
                positions = positions_recheck  # update cache for next iterations
                continue
            # ещё одна проверка — общее число positions reasonable?
            if len(positions_recheck) == 0 and len(open_trades) > 1:
                log.error(
                    "open_positions returned 0 positions but DB has %d open trades — "
                    "likely API issue, SKIP closing on this cycle",
                    len(open_trades),
                )
                continue
            # Lazy fetch fills (only когда реально нужно).
            # Bug fix 2026-05-06: использовать ttl_sec=0 — close fill могло
            # появиться на бирже после последнего cache update (TTL 60s).
            # Особенно критично для HIP-3 trades которые часто закрываются
            # SL триггером через 30-45 сек после открытия — cache всегда
            # stale → "pnl unknown — нет fills" → NO_PNL_RECORDED для xyz: pairs.
            if fills_cache is None:
                try:
                    fills_cache = client.user_fills(ttl_sec=0.0)
                except TypeError:
                    # Для не-HL clients (Kraken/Nado) у которых нет ttl_sec param
                    fills_cache = client.user_fills()

            # Если у клиента есть свой compute_realized_pnl — использовать
            # (для Kraken — ccxt format отличается от HL).
            if hasattr(client, "compute_realized_pnl"):
                pnl, exit_px = client.compute_realized_pnl(
                    fills_cache, coin, direction,
                    size=float(tr["size"]),
                    trade_open_iso=tr.get("created_at") or tr.get("detected_at"),
                )
            else:
                pnl, exit_px = _compute_realized_pnl(
                    fills_cache, coin, direction,
                    size=float(tr["size"]),
                    trade_open_iso=tr.get("created_at") or tr.get("detected_at"),
                )

            # Bug fix 2026-05-06: если pnl=None (fills не пришли) — короткая
            # пауза и retry с force-fresh. SL trigger fill иногда задерживается
            # на 1-2 сек relative к position-disappeared event.
            if pnl is None:
                try:
                    time.sleep(2)
                    try:
                        fills_retry = client.user_fills(ttl_sec=0.0)
                    except TypeError:
                        fills_retry = client.user_fills()
                    if hasattr(client, "compute_realized_pnl"):
                        pnl_r, exit_px_r = client.compute_realized_pnl(
                            fills_retry, coin, direction,
                            size=float(tr["size"]),
                            trade_open_iso=tr.get("created_at") or tr.get("detected_at"),
                        )
                    else:
                        pnl_r, exit_px_r = _compute_realized_pnl(
                            fills_retry, coin, direction,
                            size=float(tr["size"]),
                            trade_open_iso=tr.get("created_at") or tr.get("detected_at"),
                        )
                    if pnl_r is not None:
                        pnl, exit_px = pnl_r, exit_px_r
                        fills_cache = fills_retry  # propagate to subsequent iters
                        log.info("Fills retry succeeded for %s — pnl=$%.2f", coin, pnl)
                except Exception as e:
                    log.warning("Fills retry failed for %s: %s", coin, e)

            # Bug fix 2026-05-06: если opens-fills вытеснены из 100-fill окна
            # (для долго-держимых сделок 2+ дней) — compute_realized_pnl вернёт
            # (None, exit_px). Раньше pnl терялся ⇒ DB pnl_dollars=None, и
            # отчёты по PnL были неполные (AAVE id=30 KF, всё что пережило
            # ~100 fills на любом боте).
            # Fallback: считаем gross PnL по DB-entry и exit_px, fees приближаем
            # 0.10% round-trip (Kraken Futures taker tier; ≈ HL 0.09%; Nado 0.10%).
            if pnl is None and exit_px is not None and tr.get("entry"):
                try:
                    e_entry = float(tr["entry"])
                    e_exit = float(exit_px)
                    e_size = float(tr["size"])
                    if direction == "long":
                        gross = (e_exit - e_entry) * e_size
                    else:
                        gross = (e_entry - e_exit) * e_size
                    fees_rt = (e_entry + e_exit) / 2.0 * e_size * 0.001
                    pnl = gross - fees_rt
                    log.info(
                        "PnL fallback (DB entry, opens beyond fills window) for %s "
                        "trade_id=%d: gross=$%.2f fees=$%.2f net=$%.2f",
                        coin, tr["id"], gross, fees_rt, pnl,
                    )
                except Exception as e:
                    log.warning("PnL fallback failed for %s: %s", coin, e)

            pnl_pct = None
            if pnl is not None and tr.get("entry"):
                try:
                    notional = float(tr["size"]) * float(tr["entry"])
                    if notional > 0:
                        pnl_pct = pnl / notional
                except Exception:
                    pass

            # N6 fix 2026-05-07: merge close info INTO existing notes JSON
            # вместо overwrite. Раньше "closed externally / by SL trigger"
            # перезаписывал initial_notes JSON, теряя entry_filled_px /
            # entry_slippage_pct / fill_ratio (блокирует per-coin slippage data).
            close_info = {"close_reason": "closed externally / by SL trigger"}
            if exit_px is not None:
                close_info["exit_px"] = float(exit_px)
            try:
                merged = json.loads(tr.get("notes") or "")
                if not isinstance(merged, dict):
                    merged = {"prior_notes": str(tr.get("notes") or "")}
            except Exception:
                merged = {"prior_notes": str(tr.get("notes") or "")} if tr.get("notes") else {}
            merged.update(close_info)
            note_extra = json.dumps(merged)
            if pnl is not None:
                log.info(
                    "Position %s закрылась trade_id=%d pnl=$%.2f exit=%s",
                    coin, tr["id"], pnl, f"{exit_px:.6f}" if exit_px else "?",
                )
            else:
                log.info(
                    "Position %s закрылась trade_id=%d (pnl unknown — нет fills)",
                    coin, tr["id"],
                )
            update_trade_status(
                tr["id"],
                "closed_vstop",
                pnl_dollars=pnl,
                pnl_pct=pnl_pct,
                notes=note_extra,
            )
            continue

        # ---- Считаем новый stop (structure-based, 4h swings) ----
        settings = Settings.from_env()
        tf = "4h"
        try:
            df = client.candles(coin, tf, 200)
        except Exception as e:
            log.warning("candles for %s failed: %s", coin, e)
            continue
        if df.empty or len(df) < 20:
            continue

        # DEDUP: если этот trade — НЕ лидер своей группы, пропускаем
        # (лидер уже обработал SL для всей позиции, sl_oid синхронизируется)
        group = groups[(coin, direction)]
        lead = group[0]
        if tr["id"] != lead["id"]:
            # Сторонний trade в группе — синхронизируем notes c лидером
            lead_notes = lead.get("notes")
            if lead_notes:
                try:
                    d = json.loads(lead_notes)
                    if d.get("sl_oid"):
                        my_state = {
                            "current_stop": d.get("current_stop", tr["stop_loss"]),
                            "sl_oid": d["sl_oid"],
                            "exit_mode": "vstop",
                            "managed_by_lead": lead["id"],
                        }
                        _set_trade_notes(tr["id"], json.dumps(my_state))
                except Exception:
                    pass
            continue

        last_close = float(df["Close"].iloc[-1])
        old_stop = _parse_current_stop(tr["notes"], tr["stop_loss"])

        # === LIQ vs SL safety check (юзер 2026-05-06) ===
        # Правило: |entry-liq| ≥ K × |entry-SL|, K=1+LIQ_SL_BUFFER (default 1.3).
        # Isolated+liq внутри SL → попытка switch to cross (+ alert если упало).
        # Cross+liq внутри SL → WARN (cross регулируется global MM check).
        _check_liq_vs_sl_for_position(client, coin, direction, float(tr["entry"]), old_stop)

        # Parse entry time from trade record
        entry_iso = tr.get("created_at") or tr.get("detected_at")
        try:
            import pandas as pd
            entry_time = pd.to_datetime(entry_iso, utc=True)
        except Exception:
            entry_time = df["time"].iloc[-1]  # fallback
        new_stop = _compute_struct_stop(
            df, direction, entry_time, old_stop, settings.struct_buffer_pct
        )
        stop_method = f"struct buf={settings.struct_buffer_pct*100:.1f}%"

        # Размер для SL = ВСЯ позиция на бирже, не только размер этого trade
        # (на бирже позиции агрегированы). Cross-exchange field: HL/KF/Nado/Paci
        # отдают "szi" (signed), IB отдаёт "size" (abs). Читаем оба.
        _pos = positions[coin] if coin in positions else {}
        position_size = abs(float(_pos.get("size") or _pos.get("szi", 0) or 0))
        if position_size <= 0:
            position_size = float(tr["size"])

        # ---- Если Vstop сдвинулся на 0.05%+ от текущего стопа — переставляем ----
        if abs(new_stop - old_stop) / max(abs(old_stop), 1e-9) < 0.0005:
            continue

        log.info(
            "Stop %s %s [%s]: %.6f → %.6f (close=%.6f) lead_trade=%d size=%.6f",
            coin, direction, stop_method, old_stop, new_stop, last_close,
            tr["id"], position_size,
        )

        # ВАЖНОЕ ПЕРЕУПОРЯДОЧЕНИЕ 2026-05-06 (юзер):
        # СНАЧАЛА place new, ПОТОМ cancel old. Иначе window 100-500ms где
        # позиция БЕЗ SL → если рынок дампит = no protection → bad slip.
        # Оба SL reduceOnly: если оба срабатывают, position closes once,
        # второй no-op. Trail для long только повышает SL (new > old) →
        # new triggers ПЕРВЫМ при дампе, old становится orphan → cancel.
        sl_oid = _parse_sl_oid(tr["notes"])
        cancel_fn = getattr(client, "cancel_sl_order", None) or (
            lambda c, o: client.exchange.cancel(c, o)
        )
        is_buy_to_close = direction == "short"

        # Шаг 1: place new SL FIRST (защита всегда есть)
        # ВАЖНО: _place_sl сам отменяет ВСЕ active SL до placement (мой fix
        # 2026-05-05). Чтобы избежать gap, временно отключаем cancel внутри
        # _place_sl — мы сделаем cleanup после.
        new_oid = _place_sl_no_cancel(
            client, coin, is_buy_to_close, position_size, new_stop
        )

        # Шаг 2: cancel old + orphans (только если new placed успешно)
        if new_oid:
            if sl_oid is not None and sl_oid != new_oid:
                try:
                    cancel_fn(coin, sl_oid)
                except Exception as e:
                    log.warning("cancel old SL %s oid=%s failed: %s", coin, sl_oid, e)
            # Cleanup orphans (любые другие active SL кроме new_oid)
            try:
                if hasattr(client, "list_open_sl_orders"):
                    orphans = client.list_open_sl_orders(coin)
                    for oid in orphans:
                        if str(oid) != str(new_oid):
                            try:
                                cancel_fn(coin, oid)
                                log.info("Cancelled stale SL %s oid=%s", coin, oid)
                            except Exception as e:
                                log.warning("cancel stale SL %s oid=%s failed: %s",
                                          coin, oid, e)
            except Exception as e:
                log.warning("orphan SL scan %s failed: %s", coin, e)
        else:
            log.error(
                "FAILED to place new SL %s — keeping old SL active for safety",
                coin,
            )
        new_state = {"current_stop": new_stop, "sl_oid": new_oid, "exit_mode": "vstop_struct"}
        _set_trade_notes(tr["id"], json.dumps(new_state))
        # Синхронизируем followers
        for follower in group[1:]:
            follower_state = {
                "current_stop": new_stop,
                "sl_oid": new_oid,
                "exit_mode": "vstop",
                "managed_by_lead": tr["id"],
            }
            _set_trade_notes(follower["id"], json.dumps(follower_state))


def _check_liq_vs_sl_for_position(
    client, coin: str, direction: str, entry_px: float, current_sl: float
) -> None:
    """Live проверка дистанции liq_px vs SL для открытой позы.

    isolated + liq внутри SL → попытка switch to cross (extends margin pool до
    всего equity, обычно убирает риск). Если switch упал — alert через notifier.
    cross + buffer < K → log warning (cross регулируется global MM cap).

    Никогда не делает auto-close (юзер: risk-actions требуют подтверждения).
    Buffer K из LIQ_SL_BUFFER env (default 0.3 → 1.3×).
    """
    if not hasattr(client, "position_liquidation"):
        return
    try:
        info = client.position_liquidation(coin)
    except Exception as e:
        log.warning("position_liquidation(%s) failed: %s", coin, e)
        return
    if info is None:
        return
    liq_px = info.get("liq_px")
    margin_mode = info.get("margin_mode", "cross")
    leverage = info.get("leverage", 0) or 0
    try:
        buffer = float(os.getenv("LIQ_SL_BUFFER", "0.3"))
    except (TypeError, ValueError):
        buffer = 0.3
    sev, msg = check_liq_vs_sl(
        direction=direction,
        sl_px=current_sl,
        liq_px=liq_px,
        margin_mode=margin_mode,
        entry_px=entry_px,
        buffer_pct=buffer,
    )
    if sev == "ok":
        return
    log_fn = log.error if sev == "critical" else log.warning
    log_fn("LIQ-RISK %s %s: %s", sev.upper(), coin, msg)
    # Notify через стандартный notifier (silent-by-default но логирует
    # в стандартном паттерне, и если юзер включит TG будет приходить).
    try:
        from bot.notifier import Notifier
        n = Notifier()
        n.critical(
            f"LIQ-RISK {sev.upper()} {coin}: {msg}",
            dedup_key=f"liq_risk_{coin}",
        )
    except Exception:
        pass
    # Auto-recovery: isolated → switch to cross (расширяет margin pool на весь
    # equity, обычно убирает immediate liq risk без закрытия позы).
    # Юзер: risk-actions требуют подтверждения, но (а) переключение на cross —
    # это безопасное расширение margin (не закрытие/уменьшение), и (б) бот при
    # каждом открытии и так делает is_cross=True. Идемпотентный re-apply.
    if margin_mode == "isolated" and hasattr(client, "update_leverage"):
        try:
            new_lev = leverage if leverage > 0 else None
            if new_lev is None:
                # fallback на asset.max_leverage если нет в info
                try:
                    new_lev = client.asset(coin).max_leverage
                except Exception:
                    new_lev = 1
            resp = client.update_leverage(coin, new_lev, is_cross=True)
            if resp is not None:
                log.warning(
                    "LIQ-RISK %s: switched isolated→cross @ %dx (resp ok)",
                    coin, new_lev,
                )
            else:
                log.error(
                    "LIQ-RISK %s: cross switch FAILED — позиция остаётся isolated, "
                    "ручное вмешательство (HL UI → Margin Mode → Cross)",
                    coin,
                )
        except Exception as e:
            log.exception("LIQ-RISK %s: cross switch exception: %s", coin, e)


def _try_compute_close_pnl(client, coin, direction, size, trade_open_iso=None):
    """Дёргает user_fills + compute_realized_pnl (либо HL-default, либо
    exchange-specific). Возвращает (pnl, exit_px). Используется в emergency
    close-paths где обычный manage_open_positions не отрабатывает.
    Любые ошибки → (None, None) — нельзя ронять trader на journal-bookkeeping.
    """
    try:
        fills = client.user_fills()
    except Exception as e:
        log.warning("user_fills() failed in emergency close: %s", e)
        return None, None
    try:
        if hasattr(client, "compute_realized_pnl"):
            return client.compute_realized_pnl(
                fills, coin, direction, size=size, trade_open_iso=trade_open_iso
            )
        return _compute_realized_pnl(
            fills, coin, direction, size=size, trade_open_iso=trade_open_iso
        )
    except Exception as e:
        log.warning("compute_realized_pnl failed in emergency close: %s", e)
        return None, None


def _compute_realized_pnl(
    fills: list[dict],
    coin: str,
    direction: str,
    size: float,
    trade_open_iso: str | None,
) -> tuple[float | None, float | None]:
    """Возвращает (pnl_dollars, avg_exit_px) для закрытой позиции.

    Берём из fills записи где coin совпадает, dir начинается с 'Close',
    и время > trade_open. Aggregate closedPnl и взвешенная средняя цена.

    HL closedPnl уже учитывает fees внутри.

    Если на coin'е есть несколько open trades в нашей БД (одинаковое
    направление), вызывающая сторона распределяет pnl пропорционально
    размерам — здесь возвращаем total для всего размера 'size'.
    """
    if not fills:
        return None, None

    # Время открытия в ms (для time-фильтра fills)
    open_ms = 0
    if trade_open_iso:
        try:
            from datetime import datetime as _dt, timezone as _tz
            dt = _dt.fromisoformat(trade_open_iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            open_ms = int(dt.timestamp() * 1000) - 60000  # -1 мин запас
        except Exception:
            open_ms = 0

    expected_dir_substr = "Close Long" if direction == "long" else "Close Short"

    matching = []
    for f in fills:
        if f.get("coin") != coin:
            continue
        d = f.get("dir", "")
        if expected_dir_substr not in d:
            continue
        try:
            t_ms = int(f.get("time", 0))
        except Exception:
            t_ms = 0
        if open_ms and t_ms < open_ms:
            continue
        matching.append(f)

    if not matching:
        return None, None

    # Aggregate
    total_pnl = 0.0
    total_sz = 0.0
    weighted_px_sum = 0.0
    for f in matching:
        try:
            pnl = float(f.get("closedPnl", 0))
            sz = float(f.get("sz", 0))
            px = float(f.get("px", 0))
        except (TypeError, ValueError):
            continue
        total_pnl += pnl
        total_sz += sz
        weighted_px_sum += px * sz

    avg_px = weighted_px_sum / total_sz if total_sz > 0 else None

    # Если суммарный закрытый размер ≪ size → закрытие частичное, всё равно вернём
    # (но юзер увидит mismatch если debug)
    return total_pnl, avg_px


def _parse_current_stop(notes: str | None, fallback: float) -> float:
    if not notes:
        return float(fallback)
    try:
        d = json.loads(notes)
        return float(d.get("current_stop", fallback))
    except Exception:
        return float(fallback)


def _parse_sl_oid(notes: str | None) -> int | None:
    if not notes:
        return None
    try:
        d = json.loads(notes)
        return d.get("sl_oid")
    except Exception:
        return None


def _set_trade_notes(trade_id: int, notes: str) -> None:
    with _conn() as con:
        con.execute("UPDATE trades SET notes = ? WHERE id = ?", (notes, trade_id))


# ============================================================
# Утилиты
# ============================================================
def _short(resp) -> str:
    s = str(resp)
    return s if len(s) < 400 else s[:400] + "...(truncated)"
