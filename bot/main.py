"""Главный цикл бота (v2: trend-only + Vstop).
Запуск: python -m bot.main [--dry-run] [--once]
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from bot.config import (
    COIN_BLACKLIST,
    COINS,
    FORCE_LONG_COINS,
    FORCE_SHORT_COINS,
    HIGHER_TF,
    HIGHER_TF_MAP,
    MAX_OPENS_PER_CYCLE,
    MAX_OPENS_PER_DAY,
    WORKING_TF,
    WORKING_TFS,
    Settings,
)
try:
    from bot.exchange import HLClient
except ImportError:
    HLClient = object  # type-only fallback, actual client comes from exchange_factory
from bot.exchange_factory import get_exchange_client
from bot.journal import get_state, init_db, set_state
from bot.notifier import (
    Notifier,
    check_maintenance_margin,
)
from bot.patterns_v2 import detect_all_v2
from bot.risk import higher_tf_trend, is_btc_regime_blocked
from bot.trader import execute_signal, manage_open_positions

log = logging.getLogger("bot")

_stop = False


def _handle_sigterm(signum, frame):  # noqa: ARG001
    global _stop
    log.warning("Получен сигнал %s, останавливаемся после текущей итерации", signum)
    _stop = True


def _check_drawdown_halt(account_value: float, halt_pct: float, notifier=None) -> bool:
    """Update equity_peak in bot_state and return True if drawdown >= halt_pct.

    halt_pct=0 disables the gate (legacy behaviour: no DD halt). When triggered
    the caller should skip opening NEW entries; manage_open_positions still runs.
    """
    if halt_pct <= 0 or account_value <= 0:
        return False
    try:
        peak_str = get_state("equity_peak")
        peak = float(peak_str) if peak_str else 0.0
    except Exception:
        peak = 0.0
    if account_value > peak:
        try:
            set_state("equity_peak", f"{account_value:.6f}")
        except Exception as exc:
            log.warning("equity_peak update failed: %s", exc)
        return False
    if peak <= 0:
        return False
    dd_pct = 100.0 * (peak - account_value) / peak
    if dd_pct >= halt_pct:
        log.warning("DD HALT active: equity=$%.2f peak=$%.2f dd=%.2f%% >= halt=%.2f%% — new entries blocked",
                    account_value, peak, dd_pct, halt_pct)
        if notifier is not None:
            try:
                notifier.critical(
                    f"⛔ DD halt: {dd_pct:.1f}% >= {halt_pct:.0f}%\n"
                    f"  equity ${account_value:,.0f} (peak ${peak:,.0f})\n"
                    "Новые сделки заблокированы. Trail SL продолжает работать.",
                    dedup_key="dd_halt",
                )
            except Exception:
                pass
        return True
    return False


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bot", description="Hyperliquid Pattern Trading Bot v2 (trend-only + Vstop)")
    p.add_argument("--dry-run", action="store_true", help="не торгует, только логирует сигналы")
    p.add_argument("--once", action="store_true", help="один проход и выход")
    p.add_argument("--paper-balance", type=float, default=0.0,
                   help="Подменить account_value (только в --dry-run, для калибровки)")
    p.add_argument("--vstop-mult", type=float, default=2.5,
                   help="Множитель ATR для Vstop (default 2.5)")
    p.add_argument("--dump-config", action="store_true",
                   help="Печатает разрешённую конфигурацию (Settings + module constants + inline env "
                        "+ git HEAD) в JSON на stdout и выходит. Не трогает БД, биржу, торговлю.")
    return p.parse_args()


def _mask_secret(val: str) -> str:
    if not val:
        return "<empty>"
    n = len(val)
    tail = val[-4:] if n >= 4 else ""
    return f"<set:len={n},tail={tail}>"


def dump_config() -> dict:
    """Собирает РЕАЛЬНО разрешённый конфиг (как его видит этот процесс прямо сейчас).

    Включает:
      - Settings.from_env() (dataclass)
      - module-level константы из bot.config и bot.coin_filter (вычислены на import-time)
      - inline os.getenv() из bot.trader / bot.patterns_v2 / bot.notifier — резолвим
        через те же env-vars и те же дефолты, что используют hot-path функции
      - git HEAD (rev + commit time)
      - python / cwd

    Секреты маскируются (показываем длину + хвост).
    """
    import subprocess as _sp
    import sys as _sys
    from dataclasses import asdict as _asdict
    from pathlib import Path as _Path

    from bot import config as _cfg
    from bot import coin_filter as _cf

    s = _cfg.Settings.from_env()
    settings_d = _asdict(s)
    for k in ("agent_private_key", "kraken_api_secret", "kraken_spot_api_secret"):
        settings_d[k] = _mask_secret(settings_d.get(k, ""))

    module_const = {
        "config.COINS": _cfg.COINS,
        "config.FORCE_TREND": _cfg.FORCE_TREND,
        "config.FORCE_LONG_COINS": sorted(_cfg.FORCE_LONG_COINS),
        "config.FORCE_SHORT_COINS": sorted(_cfg.FORCE_SHORT_COINS),
        "config.PATTERN_MIN_RR": _cfg.PATTERN_MIN_RR,
        "config.MAX_OPENS_PER_DAY": _cfg.MAX_OPENS_PER_DAY,
        "config.MAX_OPENS_PER_CYCLE": _cfg.MAX_OPENS_PER_CYCLE,
        "config.COIN_BLACKLIST": sorted(_cfg.COIN_BLACKLIST),
        "config.LOW_LEV_THRESHOLD": _cfg.LOW_LEV_THRESHOLD,
        "config.LOW_LEV_MIN_RR": _cfg.LOW_LEV_MIN_RR,
        "config.MID_LEV_THRESHOLD": _cfg.MID_LEV_THRESHOLD,
        "config.MID_LEV_MIN_RR": _cfg.MID_LEV_MIN_RR,
        "config.WORKING_TF": _cfg.WORKING_TF,
        "config.HIGHER_TF": _cfg.HIGHER_TF,
        "config.CANDLES_LIMIT": _cfg.CANDLES_LIMIT,
        "config.EMA_LENGTH": _cfg.EMA_LENGTH,
        "config.DB_PATH": str(_cfg.DB_PATH),
        "coin_filter.WHITELIST_FILE": _cf.WHITELIST_FILE,
        "coin_filter.MIN_OI_USD": _cf.MIN_OI_USD,
        "coin_filter.MIN_VOL_24H_USD": _cf.MIN_VOL_24H_USD,
        "coin_filter.MAX_SPREAD_PCT": _cf.MAX_SPREAD_PCT,
        "coin_filter.MIN_DEPTH_05_USD": _cf.MIN_DEPTH_05_USD,
        "coin_filter.MIN_LEVELS_05": _cf.MIN_LEVELS_05,
        "coin_filter.WHITELIST_TTL_HOURS": _cf.WHITELIST_TTL_HOURS,
    }

    # Inline env reads из hot-path. Дефолты ДОЛЖНЫ совпадать с теми, что в коде —
    # если рассинхрон, это сам баг. Ключи отсортированы для стабильного diff.
    inline_env_specs = [
        # bot/trader.py
        ("CASCADE_WINDOW_MIN", "60", int),
        ("CASCADE_THRESHOLD", "3", int),
        ("MIN_1H_LIQUIDITY_RATIO_HIP3", "5", float),
        ("MIN_1H_LIQUIDITY_RATIO", "5", float),
        ("LIQUIDITY_TIER_HIGH_USD", "2000000", float),
        ("LIQUIDITY_TIER_MID_USD", "300000", float),
        ("LIQUIDITY_RISK_MULT_HIGH", "2.0", float),
        ("LIQUIDITY_RISK_MULT_MID", "1.5", float),
        ("PATTERN_BOOST_MULT", "", str),
        ("MIN_NOTIONAL_USD", "10", float),
        ("MIN_SL_NOTIONAL_USD", "100", float),
        ("MAX_MM_PCT", "0.50", float),
        # bot/patterns_v2.py
        ("INITIAL_BUFFER_PCT", "0.0001", float),
        ("ATR_REGIME_THRESHOLD", "0.7", float),
        ("QF_VOL_USD_MIN", "10000", float),
        # bot/config._coins_from_env / liquidity selectors
        ("COIN_FILTER", "none", str),
        ("INCLUDE_HIP3", "true", str),
        ("COINS_TOP_N", "", str),
        ("MIN_OI_USD", "200000", float),
        ("MIN_VOLUME_24H_USD", "500000", float),
    ]
    inline_env = {}
    for name, default, caster in inline_env_specs:
        raw = os.getenv(name, default)
        try:
            inline_env[name] = caster(raw) if raw != "" or caster is str else raw
        except (ValueError, TypeError):
            inline_env[name] = raw

    # bot/notifier.py — секреты
    inline_env["TG_BOT_TOKEN"] = _mask_secret(os.environ.get("TG_BOT_TOKEN", "").strip())
    inline_env["TG_CHAT_ID"] = os.environ.get("TG_CHAT_ID", "").strip()

    # git
    git_info: dict = {}
    repo_root = _cfg.PROJECT_ROOT
    try:
        rev = _sp.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                               stderr=_sp.DEVNULL, timeout=5).decode().strip()
        ts = _sp.check_output(["git", "-C", str(repo_root), "log", "-1", "--format=%cI"],
                              stderr=_sp.DEVNULL, timeout=5).decode().strip()
        dirty = _sp.check_output(["git", "-C", str(repo_root), "status", "--porcelain"],
                                 stderr=_sp.DEVNULL, timeout=5).decode().strip()
        git_info = {"rev": rev, "commit_time": ts, "dirty": bool(dirty),
                    "dirty_files": dirty.splitlines() if dirty else []}
    except (_sp.CalledProcessError, _sp.TimeoutExpired, FileNotFoundError) as e:
        git_info = {"error": f"{type(e).__name__}: {e}"}

    # .env mtime — ключевой сигнал расхождения "файл новее процесса"
    env_path = _Path(repo_root) / ".env"
    env_meta: dict
    if env_path.exists():
        st = env_path.stat()
        env_meta = {"path": str(env_path), "mtime_unix": int(st.st_mtime), "size": st.st_size}
    else:
        env_meta = {"path": str(env_path), "exists": False}

    return {
        "settings": settings_d,
        "module_constants": module_const,
        "inline_env": inline_env,
        "git": git_info,
        "env_file": env_meta,
        "runtime": {
            "python": _sys.version.split()[0],
            "cwd": os.getcwd(),
            "project_root": str(_cfg.PROJECT_ROOT),
            "pid": os.getpid(),
        },
    }


_NOTIFIER: Notifier | None = None


def run_iteration(
    client: HLClient, settings: Settings, dry_run: bool,
    paper_balance: float = 0.0, vstop_mult: float = 2.5,
) -> None:
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = Notifier()

    try:
        account_value = client.account_value()
    except Exception as e:
        log.exception("Не удалось получить account_value: %s", e)
        return

    log.info("Account value: $%.2f", account_value)

    if dry_run and paper_balance > 0:
        log.info("PAPER MODE: %s → $%.2f", account_value, paper_balance)
        account_value = paper_balance

    # --- Cat B catastrophe checks (только в проде) ---
    if not dry_run and account_value > 0:
        # Maintenance margin check — реальная близость к liquidation
        # (в отличие от initial margin / accountValue который вводил в заблуждение)
        try:
            state = client.info.user_state(client.settings.account_address)
            # Bug fix 2026-05-05 (вечер 2): использовать API's crossMaintenanceMarginUsed
            # напрямую вместо самопала. Юзер показал HL UI MM 35% / Nado 21%, бот
            # выдавал 70% / 50%. Метрика бота была initial margin, не real MM.
            cmm_used = float(state.get("crossMaintenanceMarginUsed", 0) or 0)
            if cmm_used > 0 and account_value > 0:
                mm_ratio = cmm_used / account_value
                if mm_ratio >= 0.80:  # юзер: план MM 50%, алерт при реальной близости к liq
                    _NOTIFIER.critical(
                        f"⚠️ Maintenance margin {mm_ratio*100:.1f}% от equity (real liquidation risk)\n"
                        f"  maint:  ${cmm_used:,.2f}\n"
                        f"  equity: ${account_value:,.2f}\n"
                        "Threshold: 80%\n"
                        "Близко к liquidation — добавь margin или закрой часть позиций.",
                        dedup_key="mm_breach",
                    )
            else:
                # fallback на старую формулу если API не вернул cmm
                check_maintenance_margin(
                    _NOTIFIER,
                    state.get("assetPositions", []),
                    account_value,
                    threshold_pct=0.80,
                )
        except Exception:
            pass

    # FIX 2026-05-04: manage_open_positions ВСЕГДА работает, даже в CB.
    # Иначе trail SL не подтягивается → позы держат initial stop часами.
    # 1. Управление существующими позициями (Vstop trail) — ВСЕГДА
    if not dry_run:
        try:
            manage_open_positions(client, atr_mult=vstop_mult)
        except Exception as e:
            log.exception("manage_open_positions failed: %s", e)

    # 1b. DD halt — block NEW entries when equity_dd >= MAX_DRAWDOWN_PCT_HALT.
    # Disabled by default (halt_pct=0). Trail SL of existing positions continues.
    # Re-enabled with pyramid 2026-05-09 (DD profile widens — see backtest).
    if not dry_run and _check_drawdown_halt(
        account_value, settings.max_drawdown_pct_halt, _NOTIFIER,
    ):
        log.info("DD halt active — skipping new-entry pass; trail-only this cycle")
        return

    # 2. Поиск новых сигналов
    # Two-pass approach для max selectivity:
    #   pass 1: собираем ВСЕ signals с их R/R по всем coins (без открытия)
    #   pass 2: сортируем по R/R desc, открываем top-N с учётом дневного лимита
    # Daily-refreshed whitelist filter (юзер 06-05): skip coins не прошедшие
    # OI/Vol/orderbook check. Reduces HL load 301→~60 coins. Safe fallback:
    # если whitelist stale/empty → full universe.
    try:
        from bot.coin_filter import load_whitelist
        whitelist = load_whitelist()
    except Exception as e:
        log.warning("whitelist load failed: %s — full universe", e)
        whitelist = None
    coin_universe = COINS if whitelist is None else [c for c in COINS if c in whitelist]
    if whitelist is not None:
        log.info("Whitelist active: %d/%d coins (filtered out %d)",
                 len(coin_universe), len(COINS), len(COINS) - len(coin_universe))

    # BTC 4h regime — для market-regime фильтра шортов на крипте.
    # Fetched once per cycle. Логируется только при изменении регима (см.
    # _get_btc_4h_trend). Empty BTC_REGIME_TICKER = silent skip.
    btc_4h_trend = _get_btc_4h_trend(client)

    if dry_run:
        # В dry-run просто прогоняем как раньше (для логов)
        for coin in coin_universe:
            try:
                _process_coin(client, settings, coin, account_value, dry_run, btc_4h_trend)
            except Exception as e:
                log.exception("Ошибка по %s: %s — продолжаем", coin, e)
    else:
        # Caps disabled (>=999) → skip _count_opens_today SQL и daily-log spam.
        # Юзер выключил caps 03-05; текущий prod держит 999/999. Гейтинг по cycle-cap
        # тоже становится no-op, но выполняется быстро в пасс-2.
        caps_active = MAX_OPENS_PER_DAY < 999 or MAX_OPENS_PER_CYCLE < 999
        if caps_active:
            opens_today = _count_opens_today()
            opens_remaining_today = max(0, MAX_OPENS_PER_DAY - opens_today)
            log.info("Selectivity: opens_today=%d (cap %d), cycle_cap=%d",
                     opens_today, MAX_OPENS_PER_DAY, MAX_OPENS_PER_CYCLE)
        else:
            opens_remaining_today = MAX_OPENS_PER_DAY  # фактически не лимитирует
        opens_remaining_cycle = MAX_OPENS_PER_CYCLE

        # pass 1: собираем candidates per coin
        all_candidates = []
        for coin in coin_universe:
            if coin in COIN_BLACKLIST:
                continue
            try:
                cands = _collect_signals(client, settings, coin, btc_4h_trend)
                all_candidates.extend(cands)
            except Exception as e:
                # N17 fix 2026-05-07: 429 после retry-exhaust = transient, нет
                # пользы от traceback. Bot и так skip-ает coin на этот cycle.
                if "429" in str(e):
                    log.warning("collect_signals %s skipped (429 retry exhausted): %s",
                                coin, str(e)[:100])
                else:
                    log.exception("collect_signals %s failed: %s", coin, e)

        # pass 2: best-RR-first, открываем до cycle/day cap
        all_candidates.sort(key=lambda x: -x[0].rr)
        opened_count = 0
        for sig, funding, higher_trend in all_candidates:
            if opened_count >= opens_remaining_cycle:
                log.info("SKIP %s — cycle cap reached (%d)", sig.coin, MAX_OPENS_PER_CYCLE)
                break
            if opens_remaining_today - opened_count <= 0:
                log.info("SKIP %s — daily cap reached (%d)", sig.coin, MAX_OPENS_PER_DAY)
                break
            try:
                trade_id = execute_signal(
                    client=client, settings=settings, signal=sig,
                    funding_hourly=funding, higher_trend=higher_trend,
                    account_value=account_value, dry_run=dry_run,
                )
                if trade_id is not None:
                    opened_count += 1
            except Exception as e:
                log.exception("execute_signal %s failed: %s", sig.coin, e)


def _count_opens_today() -> int:
    """Считает 'настоящие' trade-records открытые сегодня (UTC date).
    Исключаем cancelled (не исполнились), dry-run (тест), closed_manual
    (юзер/админ закрыл force).
    Считаем: open + closed_vstop + closed_no_sl (= реальные лайфциклы)."""
    from bot.journal import _conn
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) as n FROM trades WHERE date(created_at)=? "
            "AND status NOT IN ('cancelled','dry-run','closed_manual')",
            (today,),
        ).fetchone()
        return int(row["n"]) if row else 0


_LAST_BTC_REGIME: str | None = None
_BTC_FETCH_FAIL_LOGGED: bool = False


def _get_btc_4h_trend(client) -> str:
    """BTC 4h EMA200 regime → 'long' (bull), 'short' (bear), 'unknown'.

    Used as a market-regime filter for short patterns on crypto. Returns 'unknown'
    on any failure (filter disabled, all shorts allowed) to fail open.

    Empty BTC_REGIME_TICKER = filter explicitly disabled (silent, no warning spam).
    Logs INFO only when regime changes; fetch failures logged once.
    """
    global _LAST_BTC_REGIME, _BTC_FETCH_FAIL_LOGGED
    ticker = os.getenv("BTC_REGIME_TICKER", "BTC").strip()
    if not ticker:
        return "unknown"  # explicitly disabled, no log noise

    try:
        df_btc = client.candles(ticker, HIGHER_TF)
        if df_btc is None or df_btc.empty:
            tr = "unknown"
        else:
            tr = higher_tf_trend(df_btc)
        _BTC_FETCH_FAIL_LOGGED = False
    except Exception as e:
        if not _BTC_FETCH_FAIL_LOGGED:
            log.warning("BTC 4h regime fetch failed: %s — фильтр отключён", e)
            _BTC_FETCH_FAIL_LOGGED = True
        tr = "unknown"

    if tr != _LAST_BTC_REGIME:
        log.info("BTC 4h regime: %s (was %s)", tr, _LAST_BTC_REGIME or "—")
        _LAST_BTC_REGIME = tr
    return tr


def _collect_signals_for_tf(
    client, settings, coin,
    working_tf: str, higher_tf: str,
    btc_4h_trend: str = "unknown",
):
    """Сканирует ОДИН рабочий TF на coin'е. Возвращает list of (signal, funding, higher_dir)."""
    df = client.candles(coin, working_tf)
    df_higher = client.candles(coin, higher_tf)
    if df.empty or df_higher.empty:
        return []
    funding = client.funding_rate(coin)
    signals = detect_all_v2(df, df_higher, coin, working_tf)
    if not signals:
        return []

    # SKIP_FLAG_SHORT=true (KF 4h switch 2026-05-07) → отключить flag_short
    # globally (показал avgR=-0.18 в 4-летнем backtest, единственный убыточный паттерн).
    if os.getenv("SKIP_FLAG_SHORT", "false").lower() in ("true", "1", "yes"):
        signals = [s for s in signals if s.pattern != 'flag_short']
        if not signals:
            return []

    # IB long-only (rip 2026-05-12): IB-бот торгует только лонги, шорты любого
    # инструмента запрещены. Crypto-боты (HL/KF/Nado/Paci/Extended) трогают шорты
    # как обычно. Было: env-toggle LONG_ONLY + LONG_ONLY_EXEMPT_SEC_TYPES=CFD
    # (FX shorts проскакивали). Now hardcoded в коде, exempt mechanism удалён.
    # Validated 4h/1h sweep 2026-05-09: avgR +0.30 → +0.57, mdd 16.7% → 14.0%.
    if settings.exchange == "ib":
        signals = [s for s in signals if s.direction == "long"]
        if not signals:
            return []

    # Liquidity hard floor (LIQUIDITY_MIN_4H_USD): skip signal if instrument's last
    # 4h volume × close < threshold. Default 0 = disabled (no-op for crypto bots).
    # Used by IB to skip thin stocks (validated on 4h IB backtest 2026-05-09).
    if settings.liquidity_min_4h_usd > 0:
        try:
            vol_4h_usd = client.last_4h_volume_usd(coin)
            if vol_4h_usd is not None and vol_4h_usd < settings.liquidity_min_4h_usd:
                log.debug("LIQ_FLOOR SKIP %s: vol_4h=$%.0f < $%.0f",
                          coin, vol_4h_usd, settings.liquidity_min_4h_usd)
                return []
        except Exception as e:
            log.debug("liquidity floor check %s failed: %s — pass-through", coin, e)

    # BTC market-regime filter: flag_short/123_short на крипте при BTC bull = no edge.
    # See risk.is_btc_regime_blocked(). HIP-3 (xyz:*) exempt.
    if btc_4h_trend == "long":
        before = len(signals)
        kept = []
        for s in signals:
            blocked, reason = is_btc_regime_blocked(s.pattern, coin, btc_4h_trend)
            if blocked:
                log.info("BTC-regime filter SKIP %s %s — %s", coin, s.pattern, reason)
                continue
            kept.append(s)
        signals = kept
        if len(signals) != before:
            log.debug("%s: BTC-regime filter %d → %d signals", coin, before, len(signals))
    if not signals:
        return []

    if coin in FORCE_LONG_COINS:
        signals = [s for s in signals if s.direction == "long"]
        for s in signals:
            s.higher_trend = "up"
    elif coin in FORCE_SHORT_COINS:
        signals = [s for s in signals if s.direction == "short"]
        for s in signals:
            s.higher_trend = "down"

    # Dedup pattern signals (max RR per direction).
    # MAX_RR_DEDUP=false (KF 4h switch 2026-05-07) → пропускать ВСЕ сигналы.
    _dedup_on = os.getenv("MAX_RR_DEDUP", "true").lower() in ("true", "1", "yes")
    if _dedup_on and len(signals) > 1:
        longs = [s for s in signals if s.direction == "long"]
        shorts = [s for s in signals if s.direction == "short"]
        kept = []
        if longs: kept.append(max(longs, key=lambda s: s.rr))
        if shorts: kept.append(max(shorts, key=lambda s: s.rr))
        signals = kept

    trend_to_dir = {"up": "long", "down": "short"}
    return [(s, funding, trend_to_dir.get(s.higher_trend, "unknown")) for s in signals]


def _collect_signals(client, settings, coin, btc_4h_trend: str = "unknown"):
    """Multi-TF wrapper: scans ВСЕ TFs из WORKING_TFS (по умолчанию 1 TF — backward compat).

    Strategies (юзер 05-05 — все активные стратегии):
      1. patterns_v2 (flag/triangle/123) — detect_all_v2
      2. EMA 50/200 cross (отдельная стратегия)

    btc_4h_trend: 'long'/'short'/'unknown' — для BTC market-regime фильтра
        flag_short/123_short на крипте (HIP-3 xyz:* exempt).
    """
    out = []
    for tf in WORKING_TFS:
        higher = HIGHER_TF_MAP.get(tf, HIGHER_TF)
        try:
            cands = _collect_signals_for_tf(
                client, settings, coin, tf, higher, btc_4h_trend,
            )
            out.extend(cands)
        except Exception as e:
            if "429" in str(e):
                log.warning("collect_signals %s [%s] skipped (429): %s", coin, tf, str(e)[:80])
            else:
                log.exception("collect_signals %s [%s] failed: %s", coin, tf, e)
    return out


def _process_coin(
    client: HLClient, settings: Settings, coin: str, account_value: float, dry_run: bool,
    btc_4h_trend: str = "unknown",
) -> None:
    """Dry-run path. Multi-TF aware via _collect_signals."""
    cands = _collect_signals(client, settings, coin, btc_4h_trend)
    if not cands:
        return
    log.info("%s: найдено %d сигналов (across %d TFs)", coin, len(cands), len(WORKING_TFS))
    for sig, funding, higher_trend in cands:
        try:
            execute_signal(
                client=client,
                settings=settings,
                signal=sig,
                funding_hourly=funding,
                higher_trend=higher_trend,
                account_value=account_value,
                dry_run=dry_run,
            )
        except Exception as e:
            log.exception("execute_signal %s/%s/%s упал: %s", coin, sig.pattern, sig.timeframe, e)


def main() -> int:
    args = parse_args()

    if args.dump_config:
        import json as _json
        print(_json.dumps(dump_config(), indent=2, ensure_ascii=False, sort_keys=True, default=str))
        return 0

    setup_logging()
    log.info("Старт бота v2. dry_run=%s once=%s vstop_mult=%.1f", args.dry_run, args.once, args.vstop_mult)
    init_db()

    try:
        settings = Settings.from_env()
    except RuntimeError as e:
        log.error("Конфиг: %s", e)
        log.error("Скопируй .env.example в .env и заполни ключи.")
        return 2

    log.info("Сеть: %s | Coins: %s", settings.network, COINS)

    client = get_exchange_client(settings)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    if args.once:
        run_iteration(client, settings, args.dry_run, args.paper_balance, args.vstop_mult)
        return 0

    while not _stop:
        run_iteration(client, settings, args.dry_run, args.paper_balance, args.vstop_mult)
        if _stop:
            break
        for _ in range(settings.loop_interval_sec):
            if _stop:
                break
            time.sleep(1)

    log.info("Бот остановлен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
