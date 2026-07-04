"""Обёртка над hyperliquid-python-sdk: свечи, метадата, ордера."""
from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
from dataclasses import dataclass

import pandas as pd
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from bot.config import CANDLES_LIMIT, TF_MS, Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetMeta:
    name: str
    sz_decimals: int
    max_leverage: int


class HLClient:
    """Тонкая обёртка вокруг Info+Exchange."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        url = (
            constants.MAINNET_API_URL
            if settings.network == "mainnet"
            else constants.TESTNET_API_URL
        )
        self.url = url
        # HIP-3 поддержка: тянем список perp_dexes и передаём их в Info,
        # иначе name_to_coin не знает xyz:NVDA, cash:USA500 и т.д.
        # → candles_snapshot падает с KeyError.
        # ВАЖНО: SDK при perp_dexs=[...] НЕ добавляет main dex автоматически —
        # надо явно передать "" в начале списка. Иначе BTC/ETH/SOL пропадают.
        hip3_names = self._fetch_perp_dex_names(url)
        perp_dexs_list = [""] + hip3_names  # "" = main perp dex
        # WS candle subscribe (step 2 refactor 2026-05-08): подписываемся на push
        # вместо REST poll. HL_WS_CANDLES=false для rollback на чистый REST.
        ws_enabled = os.getenv("HL_WS_CANDLES", "true").lower() in ("true", "1", "yes")
        self._ws_enabled = ws_enabled
        try:
            self.info = Info(url, skip_ws=not ws_enabled, perp_dexs=perp_dexs_list)
            log.info("Info init with %d dexes (main + %d HIP-3): %s, ws=%s",
                     len(perp_dexs_list), len(hip3_names), perp_dexs_list, ws_enabled)
        except Exception as e:
            log.warning("Info с perp_dexs упал (%s) — фоллбэк без HIP-3", e)
            self.info = Info(url, skip_ws=not ws_enabled)
        self.wallet = Account.from_key(settings.agent_private_key)
        # Bug fix 2026-05-05 (вечер): Exchange создаёт СВОЙ внутренний Info, и без
        # perp_dexs параметра тот Info не знает HIP-3 pairs (xyz:NVDA, xyz:LLY).
        # Тогда update_leverage(xyz:LLY) → name_to_asset KeyError → trade cancelled.
        # Это был root cause "HIP-3 не торгует никогда". Утром залечил симптомы
        # (отключил xyz), не fix root cause. Теперь fixed properly.
        self.exchange = Exchange(
            self.wallet, url, account_address=settings.account_address,
            perp_dexs=perp_dexs_list,
        )
        self._meta_cache: dict[str, AssetMeta] | None = None
        # Bar-aligned cache: (coin, interval) -> (current_bar_start_ms, df, fetched_at_ts)
        # Свечи 1h/4h меняются раз в час/4ч — нет смысла пуллить чаще.
        # fetched_at_ts: unix-time когда последний раз дёрнули API. Используется
        # для min-TTL gate (см. CANDLES_MIN_TTL_SEC ниже): если bar boundary
        # перешёл, но мы запросили <TTL сек назад — отдаём stale кэш, чтобы
        # снизить 429 burst при top-of-hour invalidation.
        self._candles_cache: dict[tuple[str, str], tuple[int, pd.DataFrame, float]] = {}
        # WS candle state (step 2 2026-05-08): _ws_active_bar буферит in-progress
        # бар (последний row REST + push'ы WS) до его close, тогда переносим в
        # _candles_cache.df_closed. _ws_subscriptions: (coin, interval) -> sid для
        # unsubscribe/reconnect. _ws_disabled — coins у которых subscribe упал
        # (REST-only fallback). Всё под _ws_lock потому что WS callback идёт в
        # отдельном thread.
        self._ws_lock: threading.RLock = threading.RLock()
        self._ws_subscriptions: dict[tuple[str, str], int] = {}
        self._ws_active_bar: dict[tuple[str, str], dict] = {}
        self._ws_disabled: set[tuple[str, str]] = set()
        self._ws_last_msg_at: dict[tuple[str, str], float] = {}
        self._ws_stop = threading.Event()
        self._ws_msg_total: int = 0
        # Funding map cache: (timestamp, {coin: funding_rate})
        # meta_and_asset_ctxs возвращает funding для ВСЕХ пар одним запросом —
        # глупо звать его 100 раз за цикл
        self._funding_cache: tuple[float, dict[str, float]] | None = None
        self._funding_ttl = 60.0  # сек
        log.info("HL client init: network=%s url=%s", settings.network, url)
        if ws_enabled:
            t = threading.Thread(target=self._ws_watchdog, daemon=True,
                                 name="HL-WS-Watchdog")
            t.start()
            log.info("HL WS candle watchdog started (interval=30s)")

    # Whitelist HIP-3 dexes которые используют USDC как collateral.
    # cash/flx/vntl/hyna/km/abcd/para — у них другие токены для маржи,
    # бот их торговать не может (нужен соответствующий токен в кошельке).
    # 2026-05-05 (вечер): юзер вернул xyz обратно после анализа.
    # ВНИМАНИЕ: xyz содержит и акции/forex (DXY, BABA, KRW, AMZN, META).
    # Паттерны бот будет искать и на них. Если в логах flood "xyz:..." без
    # сделок — рассмотреть отдельный crypto-only filter поверх xyz.
    HIP3_USDC_DEXES: list[str] = ["xyz"]

    @classmethod
    def _fetch_perp_dex_names(cls, url: str) -> list[str]:
        """Возвращает список HIP-3 dexes которые НА USDC margin (whitelist).
        Передаётся в Info(perp_dexs=...) чтобы name_to_coin знал HIP-3 пары."""
        import requests
        try:
            r = requests.post(url + "/info", json={"type": "perpDexs"}, timeout=10)
            data = r.json()
            if not isinstance(data, list):
                return []
            available = []
            for d in data:
                if d is None:
                    continue
                if isinstance(d, dict):
                    name = d.get("name")
                    if name and name in cls.HIP3_USDC_DEXES:
                        available.append(name)
            return available
        except Exception as e:
            log.warning("fetch_perp_dex_names failed: %s", e)
            return []

    # ---------- Метадата ----------
    def get_meta(self, force: bool = False) -> dict[str, AssetMeta]:
        """Возвращает {coin: AssetMeta} для main perps + всех whitelisted HIP-3.

        До фикса тянули info.meta() БЕЗ dex param — это возвращало только main
        universe (BTC, ETH, ...). HIP-3 пары (xyz:TSLA и т.п.) попадали в
        execute_signal с KeyError в client.asset(coin) → "No meta for xyz:..."
        и сделка не открывалась.

        Теперь: проходим по всем dexes из perp_dexs (включая ""), агрегируем.
        """
        if self._meta_cache is not None and not force:
            return self._meta_cache

        out: dict[str, AssetMeta] = {}
        # Main universe (без dex param)
        try:
            raw = self.info.meta()
            for asset in raw.get("universe", []):
                name = asset["name"]
                out[name] = AssetMeta(
                    name=name,
                    sz_decimals=int(asset.get("szDecimals", 4)),
                    max_leverage=int(asset.get("maxLeverage", 1)),
                )
        except Exception as e:
            log.warning("meta() main fetch failed: %s", e)

        # HIP-3 dexes по whitelist
        import requests
        url = self.url + "/info"
        for dex_name in self.HIP3_USDC_DEXES:
            try:
                r = requests.post(url, json={"type": "meta", "dex": dex_name}, timeout=10)
                if r.status_code != 200:
                    continue
                data = r.json()
                for asset in data.get("universe", []):
                    name = asset["name"]
                    out[name] = AssetMeta(
                        name=name,
                        sz_decimals=int(asset.get("szDecimals", 4)),
                        max_leverage=int(asset.get("maxLeverage", 1)),
                    )
            except Exception as e:
                log.warning("meta(dex=%s) failed: %s", dex_name, e)

        self._meta_cache = out
        log.info("get_meta loaded %d assets (main + HIP-3 %s)", len(out), self.HIP3_USDC_DEXES)
        return out

    def asset(self, coin: str) -> AssetMeta:
        return self.get_meta()[coin]

    # ---------- Свечи ----------
    @staticmethod
    def _coin_cache_offset_ms(coin: str, interval: str, bar_ms: int) -> int:
        """Deterministic per-(coin, interval) offset.

        Bug fix 2026-05-06: при top-of-hour ВСЕ 60 coins одновременно invalidated
        1h/4h cache → thundering herd 60-120 cache-miss requests за секунды → 429
        burst (12:00U: 21 errors, 13:00U: 16 errors, потеря XRP signal).

        Bump 2026-05-07 (N8 follow-up): cap был `bar_ms // 12` → для 1m bar
        даёт всего 5s spread, и 60+ coins всё равно invalidate в окне 5s
        → burst 18 × 429 в первые 37s каждой минуты (00:00U observed). Меняем
        на `bar_ms // 2` чтобы 1m staggerился на 30s (24 RPS spread → ~2 RPS).
        Для 1h/4h cap прежний (capped at 60s).

        Bump 2026-05-08 (N20): на 4h-boundaries (00/04/08/12/16/20U) ОБА
        WORKING_TF=1h И HIGHER_TF=4h invalidated одновременно → 60×2=120 cache
        miss requests в 60s window = превышает HL weight budget (~60 candles_snapshot/min).
        Logs 7h: 28 × 429 clustered top-of-hour (16:00:13, 12:00:12, 00:00:10),
        peaks at 4h boundaries. Расширяем cap для bar >1h до 600s (10 min) —
        4h-burst spreads over 10 min (~0.1/s), не пересекается с 1h-burst (60s).
        Trade-off: 4h candle data можно быть до 10 min stale после bar close;
        signal latency на 4h TF — приемлемо (next loop @ 30s сек подтянет).
        """
        h = hashlib.md5(f"{coin}:{interval}".encode()).digest()
        raw = int.from_bytes(h[:4], "big")
        # 2026-05-08 (N21): 1h-fetch для liquidity check выпилен (trader.py юзает
        # cached 4h volume), top-of-1h burst исчез → 4h cap можно вернуть к 60s
        # (max-lag для 4h-decisions = 1 мин). 60 coins / 60s = 1 RPS — под HL ~10 RPS.
        cap_ms = min(30_000, bar_ms // 2)  # 1m→30s, 1h→30s, 4h→30s, 1d→30s (max-lag для 4h-decisions ≤ 30с, top-of-4h burst = 60 coins / 30s = 2 RPS — под HL ~10 RPS)
        return raw % max(1, cap_ms) if cap_ms > 0 else 0

    def candles(self, coin: str, interval: str, limit: int = CANDLES_LIMIT) -> pd.DataFrame:
        ms = TF_MS[interval]
        now_ms = int(time.time() * 1000)
        # Per-(coin,interval) offset стаггерит cache-invalidation чтобы избежать
        # синхронного thundering herd в момент закрытия бара (top-of-hour для 1h,
        # 00/04/08/12/16/20U для 4h). Effective bar boundary = floor((now-offset)/ms)*ms.
        offset_ms = self._coin_cache_offset_ms(coin, interval, ms)
        effective_now_ms = now_ms - offset_ms
        current_bar_start = (effective_now_ms // ms) * ms
        # Bar-aligned cache: пока текущий бар (со сдвигом) не закрылся, отдаём кэш.
        # 2026-05-08 (step 2): cached[0] = real bar boundary (last_bar_t из REST/WS).
        # Условие изменено == → >= потому что WS может выкатить новый bar до того,
        # как наш offset-shifted clock advance'ится (offset до 30s).
        cache_key = (coin, interval)
        with self._ws_lock:
            cached = self._candles_cache.get(cache_key)
        if cached is not None and cached[0] >= current_bar_start:
            return cached[1].copy()
        # Min-TTL re-fetch gate (2026-05-07): даже если bar boundary перешёл,
        # не дёргаем API чаще чем CANDLES_MIN_TTL_SEC (default 30s). На 1h-баре
        # после top-of-hour все coins одновременно инвалидируют cache → burst
        # 60+ requests → 429 spam. С TTL 30s доп. fetch'и в первые 30с просто
        # отдадут stale кэш (предыдущий закрытый бар) → next loop @ +30s
        # подтянет реальный новый бар.
        # 2026-05-08 (step 2): WS push постоянно обновляет fetched_at, поэтому
        # для WS-subscribed coins этот gate в стаб'е срабатывает редко — обычно
        # cached[0] >= current_bar_start уже отдал данные.
        if cached is not None:
            min_ttl = float(os.getenv("CANDLES_MIN_TTL_SEC", "30"))
            now_sec = now_ms / 1000.0
            if min_ttl > 0 and (now_sec - cached[2]) < min_ttl:
                return cached[1].copy()
        end = now_ms
        start = end - ms * (limit + 1)
        # Rate-limit: 300ms ± 100ms jitter между cache-miss requests.
        # 2026-05-08 (step 2): WS bootstrap выполняет N=130 REST-fetches sequential
        # с этим sleep — ~50-60s warmup. Steady-state: 0 REST для WS-subscribed coins.
        time.sleep(0.3 + random.uniform(0, 0.1))
        # Retry на 429 с экспоненциальным backoff (bumped 2026-05-06: 4→5 attempts,
        # был budget 7s, стал 15s — single coin не теряет signal при перегрузке).
        raw = None
        for attempt in range(5):
            try:
                raw = self.info.candles_snapshot(coin, interval, start, end)
                break
            except KeyError:
                log.warning("Coin %s не найден на %s — пропускаю", coin, self.settings.network)
                return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close", "Volume"])
            except Exception as e:
                msg = str(e)
                if "429" in msg and attempt < 4:
                    # 1s, 2s, 4s, 8s + jitter ±25%
                    base = 2 ** attempt
                    backoff = base * (1 + random.uniform(-0.25, 0.25))
                    log.warning("429 rate limit на %s — retry через %.1fs (attempt %d/5)",
                                coin, backoff, attempt + 1)
                    time.sleep(backoff)
                    continue
                raise
        if raw is None:
            return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close", "Volume"])
        if not raw:
            return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close", "Volume"])
        df_full = pd.DataFrame(raw)
        df_full["time"] = pd.to_datetime(df_full["t"], unit="ms", utc=True)
        df_full = df_full.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        for col in ("Open", "High", "Low", "Close", "Volume"):
            df_full[col] = df_full[col].astype(float)
        df_full = df_full[["time", "Open", "High", "Low", "Close", "Volume"]].reset_index(drop=True)
        # Захватываем in-progress бар (последний row) в _ws_active_bar — WS handler
        # потом будет апдейтить его и на transition append'ить в df_closed.
        active_bar_dict: dict | None = None
        last_bar_t_ms = current_bar_start
        if len(df_full) > 0:
            last_row = df_full.iloc[-1]
            try:
                last_bar_t_ms = int(pd.Timestamp(last_row["time"]).value // 10**6)
            except Exception:
                pass
            try:
                active_bar_dict = {
                    "time": pd.Timestamp(last_row["time"]),
                    "Open": float(last_row["Open"]),
                    "High": float(last_row["High"]),
                    "Low": float(last_row["Low"]),
                    "Close": float(last_row["Close"]),
                    "Volume": float(last_row["Volume"]),
                }
            except (TypeError, ValueError):
                active_bar_dict = None
        # Последняя свеча обычно ещё не закрыта — отбрасываем
        if len(df_full) > 1:
            df = df_full.iloc[:-1].reset_index(drop=True)
        else:
            df = df_full.reset_index(drop=True)
        # Сохраняем в bar-aligned cache до закрытия следующего бара.
        # 3rd элемент — fetched_at_sec для min-TTL gate выше.
        with self._ws_lock:
            self._candles_cache[cache_key] = (last_bar_t_ms, df, time.time())
            if active_bar_dict is not None:
                self._ws_active_bar[cache_key] = active_bar_dict
        # Lazy WS subscribe: после первого REST-fetch на (coin, interval) подписываемся
        # на push, чтобы следующие обновления приходили без REST. Idempotent.
        self._maybe_ws_subscribe(coin, interval)
        return df

    def last_4h_volume_usd(self, coin: str) -> float | None:
        """Returns last closed 4h bar's volume в USD (vol × close) из in-memory кэша.

        Используется trader.py для liquidity check вместо отдельного 1h fetch
        (выпилен 2026-05-08 для устранения top-of-1h burst). 4h candle уже
        фетчится в _collect_signals для каждой монеты — читаем готовый кэш.

        Returns None если кэш пустой (cold start / coin не в universe).
        Caller должен gracefully fallback на no-adjust.
        """
        with self._ws_lock:
            cached = self._candles_cache.get((coin, "4h"))
        if cached is None:
            return None
        df = cached[1]
        if df.empty or len(df) < 1:
            return None
        last_bar = df.iloc[-1]
        try:
            vol = float(last_bar.get("Volume", 0))
            px = float(last_bar.get("Close", 0))
        except (TypeError, ValueError):
            return None
        if vol <= 0 or px <= 0:
            return None
        return vol * px

    # ---------- WebSocket candle subscription (step 2 2026-05-08) ----------
    # Step 1 (commit ec7b...) выпилил 1h-fetch и снизил 4h cap до 30s. Step 2:
    # переход с REST polling на WS push. Bootstrap: первый REST candles_snapshot
    # на каждый (coin, interval) → cache + lazy subscribe. После bootstrap WS
    # сам обновляет cache через _on_candle_msg → 0 REST в steady-state.

    def _maybe_ws_subscribe(self, coin: str, interval: str) -> None:
        """Idempotent: подписываемся на (coin, interval) candle channel один раз.
        HIP-3 (xyz:*) — пробуем; если HL не пушит, через _ws_disabled не дисэйблим
        автоматически (cache stale → REST refetch через TTL/bar-boundary fallback).
        """
        if not getattr(self, "_ws_enabled", False):
            return
        wm = getattr(self.info, "ws_manager", None)
        if wm is None:
            return
        key = (coin, interval)
        with self._ws_lock:
            if key in self._ws_subscriptions or key in self._ws_disabled:
                return
            sub = {"type": "candle", "coin": coin, "interval": interval}
            try:
                sid = self.info.subscribe(sub, self._on_candle_msg)
                self._ws_subscriptions[key] = sid
                self._ws_last_msg_at[key] = 0.0
                log.info("WS subscribe candle %s %s (sid=%d, total=%d)",
                         coin, interval, sid, len(self._ws_subscriptions))
            except Exception as e:
                log.warning("WS subscribe candle %s %s failed: %s — disabled",
                            coin, interval, e)
                self._ws_disabled.add(key)

    def _on_candle_msg(self, msg) -> None:
        """WS callback. Runs on WS thread → нужен _ws_lock на любых _candles_cache /
        _ws_active_bar операциях.

        Message shape (HL WS candle channel):
          {"channel": "candle", "data": {"s": coin, "i": interval, "t": bar_start_ms,
            "T": bar_end_ms, "o": open_str, "h": high, "l": low, "c": close, "v": vol, "n": n}}

        Cache update logic:
          - bar_t == prev_active.t → still on same bar; refresh active_bar tracker
            and fetched_at, df_closed unchanged
          - bar_t > prev_active.t → bar transition: append prev_active to df_closed
            (last-known state of just-closed bar), update current_bar_start
          - bar_t < prev_active.t → stale message, ignore
          - cached is None → not bootstrapped yet, just stash active_bar; first REST
            candles() call will inflate _candles_cache properly
        """
        try:
            data = msg.get("data") if isinstance(msg, dict) else None
            if not isinstance(data, dict):
                return
            coin = data.get("s")
            interval = data.get("i")
            if not coin or not interval:
                return
            try:
                bar_t = int(data.get("t", 0))
                o = float(data.get("o", 0))
                h = float(data.get("h", 0))
                lo = float(data.get("l", 0))
                c = float(data.get("c", 0))
                v = float(data.get("v", 0))
            except (TypeError, ValueError):
                return
            if bar_t <= 0 or c <= 0:
                return
            new_bar = {
                "time": pd.Timestamp(bar_t, unit="ms", tz="UTC"),
                "Open": o, "High": h, "Low": lo, "Close": c, "Volume": v,
            }
            key = (coin, interval)
            now_sec = time.time()
            with self._ws_lock:
                self._ws_last_msg_at[key] = now_sec
                self._ws_msg_total += 1
                prev = self._ws_active_bar.get(key)
                cached = self._candles_cache.get(key)
                if cached is None:
                    # Bootstrap REST ещё не зашёл — просто буферизуем active bar
                    self._ws_active_bar[key] = new_bar
                    return
                cur_start, df, _ = cached
                prev_t_ms = (
                    int(prev["time"].value // 10**6) if prev is not None else cur_start
                )
                if bar_t < prev_t_ms:
                    return  # stale
                if bar_t == prev_t_ms:
                    # Same active bar — refresh tracker и fetched_at
                    self._ws_active_bar[key] = new_bar
                    self._candles_cache[key] = (cur_start, df, now_sec)
                    return
                # Transition: bar_t > prev_t_ms — append closing bar to df
                if prev is not None:
                    appended = pd.concat(
                        [df, pd.DataFrame([prev])], ignore_index=True
                    )
                    if len(appended) > CANDLES_LIMIT:
                        appended = appended.iloc[-CANDLES_LIMIT:].reset_index(drop=True)
                    new_df = appended
                else:
                    new_df = df
                self._candles_cache[key] = (bar_t, new_df, now_sec)
                self._ws_active_bar[key] = new_bar
        except Exception as e:
            # Никогда не ронять WS thread из-за одного бэд-msg
            log.warning("WS candle handler exception: %s", e)

    def _ws_watchdog(self) -> None:
        """Periodic WS health check. WebsocketManager — обычный threading.Thread,
        ws.run_forever() exit'ится при network drop без auto-reconnect (SDK так
        написан). Watchdog детектит mort'ность thread'а → reinit.
        """
        check_interval = 30.0
        log_every_n = 10  # лог раз в 5 минут heartbeat если всё ОК
        i = 0
        while not self._ws_stop.is_set():
            try:
                if self._ws_stop.wait(check_interval):
                    break
                i += 1
                wm = getattr(self.info, "ws_manager", None)
                if wm is None:
                    continue
                if not wm.is_alive():
                    log.warning("WS thread мёртв — reinit")
                    self._reinit_ws()
                    continue
                if i % log_every_n == 0:
                    with self._ws_lock:
                        n_subs = len(self._ws_subscriptions)
                        n_total = self._ws_msg_total
                        n_active = len([
                            k for k, t_ in self._ws_last_msg_at.items()
                            if (time.time() - t_) < 600.0
                        ])
                    log.info("WS heartbeat: %d subs, %d total msgs, %d active <10min",
                             n_subs, n_total, n_active)
            except Exception as e:
                log.warning("WS watchdog exception: %s", e)

    def _reinit_ws(self) -> None:
        """WS reconnect: stop dead thread, новый WebsocketManager, re-subscribe всё.
        REST candles fallback автоматически отработает для пропущенных баров через
        существующий TTL/bar-boundary check в candles().
        """
        from hyperliquid.websocket_manager import WebsocketManager
        with self._ws_lock:
            prev_subs = list(self._ws_subscriptions.keys())
            try:
                if self.info.ws_manager is not None:
                    self.info.ws_manager.stop()
            except Exception:
                pass
            try:
                self.info.ws_manager = WebsocketManager(self.info.base_url)
                self.info.ws_manager.start()
            except Exception as e:
                log.error("WS reinit failed: %s — будем пытаться снова через 30s", e)
                return
            self._ws_subscriptions.clear()
            self._ws_disabled.clear()
            for coin, interval in prev_subs:
                try:
                    sub = {"type": "candle", "coin": coin, "interval": interval}
                    sid = self.info.subscribe(sub, self._on_candle_msg)
                    self._ws_subscriptions[(coin, interval)] = sid
                except Exception as e:
                    log.warning("WS re-subscribe %s %s after reconnect failed: %s",
                                coin, interval, e)
                    self._ws_disabled.add((coin, interval))
            log.info("WS reinit done — %d/%d subs restored",
                     len(self._ws_subscriptions), len(prev_subs))

        # ---------- Funding ----------
    def funding_rate(self, coin: str) -> float:
        """Текущий funding rate (часовой) для пары.
        Тянет ВСЕ funding одним запросом meta_and_asset_ctxs() и кэширует на 60с.
        """
        funding_map = self._get_funding_map()
        return funding_map.get(coin, 0.0)

    def _get_funding_map(self) -> dict[str, float]:
        """Возвращает {coin: funding_rate} с TTL-кэшем 60с + retry на 429."""
        now = time.time()
        if self._funding_cache is not None:
            cached_ts, cached_map = self._funding_cache
            if now - cached_ts < self._funding_ttl:
                return cached_map

        # Cache miss — fetch with retry на 429
        for attempt in range(4):
            try:
                ctxs = self.info.meta_and_asset_ctxs()
                if not isinstance(ctxs, list) or len(ctxs) < 2:
                    break
                universe = ctxs[0].get("universe", [])
                asset_ctxs = ctxs[1]
                fmap: dict[str, float] = {}
                for i, asset in enumerate(universe):
                    if i < len(asset_ctxs):
                        fmap[asset["name"]] = float(asset_ctxs[i].get("funding", 0.0))
                self._funding_cache = (now, fmap)
                return fmap
            except Exception as e:
                msg = str(e)
                if "429" in msg and attempt < 3:
                    backoff = 2 ** attempt
                    log.warning("429 на funding map — retry через %ds", backoff)
                    time.sleep(backoff)
                    continue
                log.warning("funding map fetch failed: %s", e)
                break

        # Если всё упало — возвращаем кэш если был, иначе пустой dict
        if self._funding_cache is not None:
            return self._funding_cache[1]
        return {}

    # ---------- Состояние ----------
    def account_value(self) -> float:
        """Реальный trading equity в Unified Account (исправлено 03-05-2026).

        Правильная формула (эмпирически проверена против HL UI):
          total = (spot.USDC.total - spot.USDC.hold) + non_USDC_value + perps.accountValue

        Где:
          spot.USDC.total — вся USDC в спот-кошельке (вкл. заблокированную как margin)
          spot.USDC.hold — заблокированная часть (= perps initial margin)
          perps.accountValue — collateral + unrealized PnL по перпам

        Старый bug: бот возвращал spot.USDC.total + non_USDC БЕЗ учёта perps_av,
        что недосчитывало unrealized PnL → бот сайзил на ~22% меньше реального.
        Старший комментарий про "double-count" был неверным.

        Эмпирическая проверка 03-05-2026:
          spot.total $2138 - hold $1213 = free $925
          non_USDC HYPE 0.4 × $41.5 = $16.6
          perps_av $1227 (=$1213 margin + $14 unrealized PnL)
          TOTAL = $925 + $16.6 + $1227 = $2168 ← matches HL UI ~$2k
        """
        # Retry on 429 (3 attempts × 1+2 = 3s budget). REVERTED 2026-05-08 from
        # 5 attempts (15s budget) — 5-attempt storm спамил API в rate-limited window
        # → 8 × ClientError за 17 мин (vs 1/7h при 3-attempt). Cleaner cycle skip
        # каждые 30s, HL per-user throttle успевает recover.
        spot = None
        spot_last_exc: Exception | None = None
        for attempt in range(3):
            try:
                spot = self.info.spot_user_state(self.settings.account_address)
                spot_last_exc = None
                break
            except Exception as e:
                spot_last_exc = e
                msg = str(e)
                if "429" in msg and attempt < 2:
                    backoff = 2 ** attempt
                    log.warning("429 на spot_user_state (account_value) — retry через %ds (attempt %d/3)",
                                backoff, attempt + 1)
                    time.sleep(backoff)
                    continue
                break
        if spot_last_exc is not None:
            raise spot_last_exc
        spot_usdc_total = 0.0
        spot_usdc_hold = 0.0
        non_usdc_value = 0.0
        # Use cached mids if recent (5s TTL via _mids_cache)
        try:
            now = time.time()
            cache = getattr(self, "_mids_cache", None)
            if cache is not None and (now - cache[0]) < 5.0:
                mids = cache[1]
            else:
                mids = self.info.all_mids()
                self._mids_cache = (now, mids)
        except Exception:
            mids = {}
        for b in spot.get("balances", []):
            coin = b.get("coin")
            try:
                total = float(b.get("total", 0))
                hold = float(b.get("hold", 0))
            except (TypeError, ValueError):
                continue
            if coin == "USDC":
                spot_usdc_total += total
                spot_usdc_hold += hold
            elif total > 0 and coin in mids:
                try:
                    px = float(mids[coin])
                    non_usdc_value += total * px
                except (TypeError, ValueError):
                    pass

        spot_usdc_free = spot_usdc_total - spot_usdc_hold

        # Perps accountValue (margin used + unrealized PnL).
        # Retry on 429 (3 attempts × exp backoff = 1+2 = 3s budget). REVERTED
        # 2026-05-08 from 5 attempts (15s) — same storm reasoning as spot_user_state.
        # Silent fallback к perps_av=0 катастрофически недосчитывал equity (cash
        # only) → false positive в MM-warning (378% от equity 22:00U 06-05).
        # Если retry exhausted — raise: caller ловит и skip, что корректнее чем
        # сломанная equity-метрика. ClientError в логе ожидаем редко (1-2 в час).
        perps_av = 0.0
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                state = self.info.user_state(self.settings.account_address)
                perps_av = float(state.get("marginSummary", {}).get("accountValue", 0))
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                msg = str(e)
                if "429" in msg and attempt < 2:
                    backoff = 2 ** attempt  # 1, 2 = 3s
                    log.warning("429 на user_state (account_value) — retry через %ds (attempt %d/3)",
                                backoff, attempt + 1)
                    time.sleep(backoff)
                    continue
                break
        if last_exc is not None:
            raise last_exc

        equity = spot_usdc_free + non_usdc_value + perps_av
        if spot_usdc_total > 0.5 or perps_av > 0.5:
            log.info(
                "account_value: spot_free $%.2f (= total $%.2f - hold $%.2f) + "
                "non-USDC $%.2f + perps_av $%.2f = $%.2f total",
                spot_usdc_free, spot_usdc_total, spot_usdc_hold,
                non_usdc_value, perps_av, equity,
            )
        return equity

    def spot_usdc(self) -> float:
        try:
            spot = self.info.spot_user_state(self.settings.account_address)
            return sum(
                float(b.get("total", 0))
                for b in spot.get("balances", [])
                if b.get("coin") == "USDC"
            )
        except Exception as e:
            log.warning("spot_user_state failed: %s", e)
            return 0.0

    def open_positions(self) -> dict[str, dict]:
        """Кэш на 20с + retry на 429.
        Concentration check зовётся для каждого сигнала, без кэша 429 заваливают.

        Bug fix 2026-05-07: info.user_state() возвращает только main perp dex,
        HIP-3 (xyz:*) позиции в отдельном clearinghouseState с dex=xyz. До этого
        фикса все HIP-3 trades в DB закрывались как "closed externally" через
        ~30-90с после открытия (bot не находил позицию в positions[]), но на
        бирже жили без trail (orphan AMZN/CRWV/META). Теперь мерджим main +
        HIP3_USDC_DEXES в один dict.
        """
        now = time.time()
        cache = getattr(self, "_positions_cache", None)
        if cache is not None and now - cache[0] < 20.0:
            return cache[1]
        # Retry budget REVERTED 2026-05-08: 5→3 attempts (15s→3s) — long retry
        # storm спамил API в rate-limited window → 8 × ClientError за 17 мин.
        # 3-attempt + 30s loop interval даёт HL throttle время recover.
        for attempt in range(3):
            try:
                state = self.info.user_state(self.settings.account_address)
                out: dict[str, dict] = {}
                for ap in state.get("assetPositions", []):
                    pos = ap.get("position", {})
                    coin = pos.get("coin")
                    if coin and float(pos.get("szi", 0)) != 0:
                        out[coin] = pos
                # HIP-3 dexes (xyz и т.д.) — отдельные clearinghouseState
                for dex in self.HIP3_USDC_DEXES:
                    try:
                        dex_state = self.info.post("/info", {
                            "type": "clearinghouseState",
                            "user": self.settings.account_address,
                            "dex": dex,
                        })
                        for ap in (dex_state or {}).get("assetPositions", []):
                            pos = ap.get("position", {})
                            coin = pos.get("coin")
                            if coin and float(pos.get("szi", 0)) != 0:
                                out[coin] = pos
                    except Exception as e:
                        log.warning("open_positions HIP-3 dex=%s failed: %s", dex, e)
                self._positions_cache = (now, out)
                return out
            except Exception as e:
                msg = str(e)
                if "429" in msg and attempt < 2:
                    backoff = 2 ** attempt  # 1, 2 = 3s
                    log.warning("429 на open_positions — retry через %ds (attempt %d/3)",
                                backoff, attempt + 1)
                    time.sleep(backoff)
                    continue
                log.warning("open_positions failed: %s", e)
                break
        # Возвращаем старый кэш если был, иначе пусто
        if cache is not None:
            return cache[1]
        return {}

    def invalidate_positions_cache(self) -> None:
        """Вызвать после открытия/закрытия позиции для актуальности concentration."""
        self._positions_cache = None

    def _user_state_cached(self, ttl_sec: float = 10.0) -> dict | None:
        """user_state с TTL-кэшем. Bug fix 2026-05-07 (N8 follow-up):
        manage_open_positions вызывает position_liquidation для каждой из 30
        открытых поз → 30 user_state requests/cycle = 429 burst при concentration.
        TTL 10s достаточен (margin/liq меняются только при mark price drift,
        который мы и так читаем отдельно через mark_price). Возвращает None
        если последний fetch < ttl_sec назад был неудачным и нет stale-кеша.
        """
        now = time.time()
        cache = getattr(self, "_user_state_cache", None)
        if cache is not None and (now - cache[0]) < ttl_sec:
            return cache[1]
        try:
            state = self.info.user_state(self.settings.account_address)
            self._user_state_cache = (now, state)
            return state
        except Exception as e:
            # Stale ok если есть — лучше чем None (вызывающий просто пропустит итерацию)
            if cache is not None:
                log.warning("user_state failed (using stale cache age=%.0fs): %s",
                            now - cache[0], e)
                return cache[1]
            log.warning("user_state failed (no cache): %s", e)
            return None

    def position_liquidation(self, coin: str) -> dict | None:
        """{liq_px, margin_mode, leverage} для open позиции. None если позы нет.

        margin_mode: 'cross' | 'isolated' (HL: position.leverage.type).
        liq_px: HL отдаёт liquidationPx только когда liq близок (cross), у
        isolated всегда. Может быть None даже для open позы.
        """
        state = self._user_state_cached(ttl_sec=10.0)
        if state is None:
            return None
        for ap in state.get("assetPositions", []) or []:
            p = ap.get("position", {}) or {}
            if p.get("coin") != coin:
                continue
            if abs(float(p.get("szi", 0) or 0)) < 1e-12:
                return None
            lev = p.get("leverage", {}) or {}
            liq_raw = p.get("liquidationPx")
            try:
                liq_px = float(liq_raw) if liq_raw is not None else None
            except (TypeError, ValueError):
                liq_px = None
            return {
                "liq_px": liq_px,
                "margin_mode": str(lev.get("type", "cross")).lower(),
                "leverage": int(lev.get("value", 1) or 1),
            }
        return None

    def user_fills(self, ttl_sec: float = 60.0) -> list[dict]:
        """Recent fills с TTL-кэшем. Используется для PnL/slippage tracking
        при закрытии позиций."""
        now = time.time()
        cache = getattr(self, "_fills_cache", None)
        if cache is not None and now - cache[0] < ttl_sec:
            return cache[1]
        for attempt in range(3):
            try:
                fills = self.info.user_fills(self.settings.account_address)
                if not isinstance(fills, list):
                    fills = []
                self._fills_cache = (now, fills)
                return fills
            except Exception as e:
                msg = str(e)
                if "429" in msg and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                log.warning("user_fills failed: %s", e)
                break
        if cache is not None:
            return cache[1]
        return []

    # ---------- Slippage estimate (per side) ----------
    # Used by trader.py LIQUIDITY DRAG CHECK. Backed by data/hl_slippage.json
    # generated from prior fills. HIP-3 (xyz:*) — observed prod slip 0.1-0.5%.
    # Fee RT estimate: HL taker = 0.045%, RT = 0.09% ≈ 0.001 conservative.
    _fee_rt_estimate = 0.001
    _slip_table_cache: dict[str, float] | None = None

    def slip_per_side(self, coin: str, notional_usd: float) -> float:
        """Per-side fill slippage estimate as fraction of notional.

        Looks up data/hl_slippage.json (rows: [coin, slip_at_X1, slip_at_X2,
        slip_at_X3, slip_at_X4]). Conservative pick = max across rows. Falls
        back to defaults: HIP-3 0.005 (0.5%), other 0.001 (0.1%).
        """
        del notional_usd  # not size-sensitive yet (table is single-bucket avg)
        cache = HLClient._slip_table_cache
        if cache is None:
            cache = {}
            try:
                import json
                from pathlib import Path
                p = Path(__file__).resolve().parent.parent / "data" / "hl_slippage.json"
                if p.exists():
                    rows = json.loads(p.read_text())
                    for r in rows:
                        if isinstance(r, list) and len(r) >= 2:
                            slips = [abs(float(x)) for x in r[1:] if x is not None]
                            if slips:
                                cache[str(r[0])] = max(slips)
            except Exception as e:
                log.warning("slip_per_side: cannot load hl_slippage.json: %s", e)
            HLClient._slip_table_cache = cache
        if coin in cache:
            return cache[coin]
        # HIP-3 default: 0.5% per side (observed 0.1-0.5% in prod)
        if coin.startswith("xyz:"):
            return 0.005
        return 0.001

    def mark_price(self, coin: str, ttl: float = 5.0) -> float:
        """Returns current mark price, used cached all_mids snapshot if fresh.

        Bug fix 2026-05-06: раньше mark_price звал info.all_mids() per-coin.
        all_mids возвращает ВСЕ coins одной операцией. 270× per-coin =
        270× same data + 270× rate limit cost. Кэш 5s = 1 call вместо 270.

        Bug fix 2026-05-06 (2): info.all_mids() БЕЗ dex= возвращает только main
        perp dex (543 keys), HIP-3 (xyz:*) пары там нет. Verified live: 0 xyz:*
        в default ответе, 71 HIP-3 через info.post(.., {"dex":"xyz"}). Раньше
        для xyz:* возвращали 0 → SIGNAL FRESHNESS GATE silently skip'ался для
        HIP-3 trades. Fix: при HIP-3 coin отдельный кэш через post-call.
        """
        now = time.time()
        # HIP-3 path: отдельный кэш по dex (xyz пока единственный USDC HIP-3)
        if coin.startswith("xyz:"):
            dex = coin.split(":", 1)[0]  # "xyz"
            hip3_cache = getattr(self, "_mids_cache_hip3", None)
            if hip3_cache is not None:
                cache_ts, cache_by_dex = hip3_cache
                if (now - cache_ts) < ttl and dex in cache_by_dex:
                    return float(cache_by_dex[dex].get(coin, 0.0))
            try:
                mids_dex = self.info.post("/info", {"type": "allMids", "dex": dex})
                if not isinstance(mids_dex, dict):
                    return 0.0
                cache_by_dex = (hip3_cache[1] if hip3_cache else {})
                cache_by_dex[dex] = mids_dex
                self._mids_cache_hip3 = (now, cache_by_dex)
                return float(mids_dex.get(coin, 0.0))
            except Exception as e:
                log.warning("mark_price(%s) HIP-3 failed: %s", coin, e)
                if hip3_cache is not None and dex in hip3_cache[1]:
                    return float(hip3_cache[1][dex].get(coin, 0.0))
                return 0.0
        # Main perp dex path (default)
        cache = getattr(self, "_mids_cache", None)
        if cache is not None and (now - cache[0]) < ttl:
            return float(cache[1].get(coin, 0.0))
        try:
            mids = self.info.all_mids()
            self._mids_cache = (now, mids)
            return float(mids.get(coin, 0.0))
        except Exception as e:
            log.warning("mark_price(%s) failed: %s", coin, e)
            if cache is not None:
                return float(cache[1].get(coin, 0.0))
        return 0.0

    # ---------- Округление цены под правила Hyperliquid ----------
    def round_price(self, coin: str, px: float) -> float:
        """Hyperliquid: цена должна иметь <=5 значащих цифр И <=(6-szDecimals) знаков после запятой.
        Целочисленные цены (>=100000) всегда валидны.
        """
        import math
        if px <= 0:
            return px
        try:
            sz_decimals = self.asset(coin).sz_decimals
        except KeyError:
            sz_decimals = 4

        max_decimals = max(0, 6 - sz_decimals)
        magnitude = math.floor(math.log10(abs(px)))
        sig_decimals = max(0, 4 - magnitude)
        decimals = min(max_decimals, sig_decimals)
        rounded = round(px, decimals)
        # Защита: если число всё ещё имеет лишние цифры из-за float-imprecision — форматируем
        formatted = f"{rounded:.{decimals}f}" if decimals > 0 else f"{int(round(rounded))}"
        return float(formatted)

    # ---------- Торговля ----------
    def update_leverage(self, coin: str, leverage: int, is_cross: bool = True) -> dict | None:
        """Retry на 429 с exponential backoff. Если все retry упали — None.
        Caller (trader) проверяет результат и не открывает позицию если leverage
        не установлен (иначе HL может market_open с непонятным leverage)."""
        for attempt in range(4):
            try:
                return self.exchange.update_leverage(leverage, coin, is_cross)
            except Exception as e:
                msg = str(e)
                if "429" in msg and attempt < 3:
                    backoff = 2 ** attempt  # 1s, 2s, 4s
                    log.warning(
                        "update_leverage(%s, %s) 429 — retry через %ds",
                        coin, leverage, backoff,
                    )
                    time.sleep(backoff)
                    continue
                log.warning("update_leverage(%s, %s) failed: %s", coin, leverage, e)
                return None
        return None

    def _retry_429(self, fn, op_name: str, attempts: int = 5):
        """Универсальный retry-обёртка для critical order ops.
        429 — exp backoff 1/2/4/8/16с (макс 31с total).
        Любая другая ошибка — кидаем сразу."""
        last_err = None
        for attempt in range(attempts):
            try:
                return fn()
            except Exception as e:
                msg = str(e)
                last_err = e
                if "429" in msg and attempt < attempts - 1:
                    backoff = 2 ** attempt  # 1, 2, 4, 8, 16
                    log.warning("429 на %s — retry через %dс (попытка %d/%d)",
                                op_name, backoff, attempt + 1, attempts)
                    time.sleep(backoff)
                    continue
                raise
        raise last_err  # never reached

    def market_open(self, coin: str, is_buy: bool, sz: float) -> dict:
        return self._retry_429(
            lambda: self.exchange.market_open(
                coin, is_buy=is_buy, sz=sz, slippage=self.settings.slippage
            ),
            f"market_open({coin})",
        )

    def market_close(self, coin: str) -> dict:
        return self._retry_429(
            lambda: self.exchange.market_close(coin),
            f"market_close({coin})",
        )

    def trigger_sl(self, coin: str, is_buy: bool, sz: float, trigger_px: float) -> dict:
        px = self.round_price(coin, trigger_px)
        order_type = {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": "sl"}}
        return self._retry_429(
            lambda: self.exchange.order(
                coin, is_buy=is_buy, sz=sz, limit_px=px,
                order_type=order_type, reduce_only=True,
            ),
            f"trigger_sl({coin})",
        )

    def trigger_tp(self, coin: str, is_buy: bool, sz: float, trigger_px: float) -> dict:
        px = self.round_price(coin, trigger_px)
        order_type = {"trigger": {"triggerPx": px, "isMarket": False, "tpsl": "tp"}}
        return self._retry_429(
            lambda: self.exchange.order(
                coin, is_buy=is_buy, sz=sz, limit_px=px,
                order_type=order_type, reduce_only=True,
            ),
            f"trigger_tp({coin})",
        )


# Bug fix 2026-05-05: list open SL orders for orphan cleanup
# 2026-05-08: wrap with _retry_429 — наблюдалось 16:00:16 cancel SL INJ 429
# silent fail → ORPHAN SL остаётся, на следующем trail двойной cancel logic
# разруливает, но в лог уходит warning. _retry_429 даёт 31s budget на 5 attempts.
def _hl_cancel_sl_order(self, coin: str, oid) -> dict:
    """Unified cancel API (works for HL/KF/Nado wrappers identically)."""
    return self._retry_429(
        lambda: self.exchange.cancel(coin, oid),
        f"cancel_sl({coin})",
    )


HLClient.cancel_sl_order = _hl_cancel_sl_order


def _hl_list_open_sl_orders(self, coin: str) -> list[int]:
    """List all triggered/stop orders for a coin via HL info.

    Bug fix 2026-05-05 (вечер 4): info.open_orders() возвращает orders БЕЗ
    trigger полей → используем frontend_open_orders().

    Bug fix 2026-05-06: frontend_open_orders без `dex=` возвращает только
    main dex. Для HIP-3 (xyz:*) orphan cleanup всегда находил [] → старые
    SL накапливались на каждом trail/re-entry. Юзер обнаружил xyz:AMZN с
    SL sz=61 при позиции 0.633 и xyz:CRWV с двумя SL 35.6+47.29 при поз 23.57.
    Фикс: опрашиваем main dex + все HIP3_USDC_DEXES, фильтруем по coin.
    """
    addr = self.settings.account_address
    dexes = [""] + list(HLClient.HIP3_USDC_DEXES)
    all_orders: list = []
    for dex in dexes:
        try:
            payload = {"type": "frontendOpenOrders", "user": addr}
            if dex:
                payload["dex"] = dex
            chunk = self.info.post("/info", payload)
            if chunk:
                all_orders.extend(chunk)
        except Exception:
            continue
    if not all_orders:
        # Last-resort fallback: legacy SDK call (main dex only, no trigger fields)
        try:
            all_orders = self.info.open_orders(addr) or []
        except Exception:
            return []
    out: list[int] = []
    for o in all_orders:
        if o.get("coin") != coin: continue
        t = str(o.get("orderType", "")).lower()
        if "stop" in t or o.get("isTrigger") or o.get("reduceOnly"):
            oid = o.get("oid")
            if oid:
                try:
                    out.append(int(oid))
                except (TypeError, ValueError):
                    pass
    return out

# Inject method into HLClient at runtime
HLClient.list_open_sl_orders = _hl_list_open_sl_orders
