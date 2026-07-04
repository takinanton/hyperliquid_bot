"""Kraken Futures адаптер через ccxt — mirror of HLClient interface.

Kraken Futures (a.k.a. Cryptofacilities) — perpetual futures с leverage
до 50x на BTC/ETH, до 10-25x на альтах. Margin coin: USD (USDC через
USDC futures pairs) или BTC. По умолчанию работаем с USD-margined.

Symbol naming в ccxt:
  Spot: 'BTC/USD'
  Futures: 'BTC/USD:USD' — perp inverse
           'PI_XBTUSD' — internal Kraken Futures symbol

Не-обязательно реализованные методы (raises NotImplementedError):
- HIP-3 dexes (это HL-only концепция)
- funding map cache (Kraken возвращает funding по requesto, без global cache)
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
from dataclasses import dataclass

import ccxt
import pandas as pd

from bot.config import Settings, TF_MS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetMeta:
    name: str
    sz_decimals: int
    max_leverage: int


class KrakenClient:
    """Тонкая обёртка вокруг ccxt.krakenfutures для совместимости с HLClient."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        creds = {
            "apiKey": settings.kraken_api_key,
            "secret": settings.kraken_api_secret,
            "enableRateLimit": True,  # ccxt auto-throttle
        }
        if settings.network != "mainnet":
            # Kraken sandbox URL — sb.cryptofacilities.com
            self.exchange = ccxt.krakenfutures(creds)
            self.exchange.set_sandbox_mode(True)
        else:
            self.exchange = ccxt.krakenfutures(creds)
        # Cache markets metadata
        try:
            self._markets = self.exchange.load_markets()
        except Exception as e:
            log.warning("Kraken load_markets failed: %s — будет lazy-load", e)
            self._markets = {}
        # Cache leverage tiers (Kraken Futures specific endpoint)
        self._leverage_cache: dict[str, int] = {}
        # Bar-aligned candle cache (mirror HL's): (coin, interval) -> (last_bar_t_ms, df, fetched_at_sec)
        # Multi-TF mode (4h+1h, 280 coins) даёт ~1120 fetches/cycle без кэша = ~10min cycle
        # → entry slippage 0.3-0.5% на 1h. С кэшем cycle падает до ~30-60s.
        self._candles_cache: dict[tuple[str, str], tuple[int, pd.DataFrame, float]] = {}
        self._cache_lock = threading.RLock()
        # Funding rate cache (TTL 60s). Multi-TF без этого = 560 funding fetches/cycle (4-5min sequential).
        # Funding rate меняется per-1h, 60s stale абсолютно ОК.
        self._funding_cache: dict[str, tuple[float, float]] = {}  # coin -> (rate, fetched_at_sec)
        try:
            tiers = self.exchange.fetch_leverage_tiers(list(self._markets.keys())[:100])
            for sym, t in tiers.items():
                if t:
                    max_lev = max(int(x.get("maxLeverage", 1) or 1) for x in t)
                    self._leverage_cache[sym] = max_lev
            log.info("Loaded leverage tiers for %d Kraken pairs", len(self._leverage_cache))
        except Exception as e:
            log.warning("fetch_leverage_tiers failed: %s — defaulting to 10x", e)
        log.info(
            "Kraken Futures client init: %d markets, network=%s",
            len(self._markets),
            settings.network,
        )

    # ------ Account state ------
    def account_value(self) -> float:
        """Total account equity в USD.
        Kraken Futures multi-collateral (flex) account: реальное equity лежит в
        info.accounts.flex.portfolioValue (не в стандартном ccxt 'USD').
        Fallback на ccxt 'USD' для старых cash accounts.
        """
        try:
            balance = self.exchange.fetch_balance()
            info = balance.get("info", {})
            accounts = info.get("accounts", {})
            flex = accounts.get("flex", {})
            if flex:
                pv = float(flex.get("portfolioValue", 0) or 0)
                if pv > 0:
                    log.info("Kraken account_value: $%.2f (flex portfolio)", pv)
                    return pv
            # Fallback for cash accounts
            usd_total = float(balance.get("USD", {}).get("total", 0))
            log.info("Kraken account_value: $%.2f (cash USD fallback)", usd_total)
            return usd_total
        except Exception as e:
            log.error("fetch_balance failed: %s", e)
            return 0.0

    def open_positions(self) -> dict:
        """{symbol: position_dict} format совместимый с HL.

        ⚠️ ALSO: Detects ISOLATED-margin positions and sends Telegram alert.
        Isolated mode = liquidation risk on small adverse moves (see partial-liq incident
        2026-05-04 DOGE 50x). Production should use CROSS margin (set in Kraken Pro UI:
        Trading Preferences → Margin Mode → Cross).
        """
        try:
            positions = self.exchange.fetch_positions()
        except Exception as e:
            log.warning("fetch_positions failed: %s", e)
            return {}
        result = {}
        isolated_positions = []
        for p in positions:
            contracts = float(p.get("contracts", 0) or 0)
            if abs(contracts) < 1e-9:
                continue
            symbol = p["symbol"]
            margin_mode = (p.get("marginMode") or p.get("marginType") or "").lower()
            leverage = int(p.get("leverage", 1) or 1)
            if margin_mode == "isolated" and leverage >= 10:
                isolated_positions.append((symbol, leverage))
            side = p.get("side")
            sz_signed = contracts if side == "long" else -contracts
            result[symbol] = {
                "coin": symbol,
                "szi": str(sz_signed),
                "entryPx": str(p.get("entryPrice", 0)),
                "unrealizedPnl": str(p.get("unrealizedPnl", 0)),
                "marginUsed": str(p.get("initialMargin", 0)),
                "leverage": {"value": leverage},
                "marginMode": margin_mode,
            }
        # Telegram alert if any isolated high-leverage positions detected
        if isolated_positions:
            try:
                from bot.notifier import Notifier
                n = Notifier()
                if n.enabled:
                    msg = "ISOLATED HIGH-LEVERAGE positions on K-Futures (LIQ RISK):\n"
                    for sym, lev in isolated_positions:
                        msg += f"  {sym} @ {lev}x\n"
                    msg += "→ Switch to CROSS margin in Kraken Pro UI."
                    n.critical(msg, dedup_key="kf_isolated_warn")
            except Exception:
                pass
        return result

    # ------ Market data ------
    @staticmethod
    def _cache_offset_ms(coin: str, interval: str, bar_ms: int) -> int:
        """Per-(coin,interval) deterministic offset для staggered cache invalidation.
        Mirror HL — избегаем thundering herd на bar boundaries (top-of-hour для 1h)."""
        h = hashlib.md5(f"{coin}:{interval}".encode()).digest()
        raw = int.from_bytes(h[:4], "big")
        cap_ms = min(30_000, bar_ms // 2)
        return raw % max(1, cap_ms) if cap_ms > 0 else 0

    def candles(self, coin: str, interval: str, limit: int = 200) -> pd.DataFrame:
        """OHLCV в pandas DataFrame с bar-aligned cache (mirror HL).

        Cache hit: пока bar boundary не пересечён → отдаём cached df.
        Min-TTL gate: даже после boundary, не дёргаем API чаще CANDLES_MIN_TTL_SEC (30s)
        чтобы избежать burst при синхронной cache-invalidation top-of-hour.
        """
        bar_ms = TF_MS.get(interval)
        if bar_ms is None:
            # Unknown TF — fallback на direct fetch без кеша
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
        # Min-TTL gate
        if cached is not None:
            min_ttl = float(os.getenv("CANDLES_MIN_TTL_SEC", "30"))
            if min_ttl > 0 and (now_ms / 1000.0 - cached[2]) < min_ttl:
                return cached[1].copy()

        # Cache miss → fetch fresh + retry на 429
        df = self._fetch_candles_direct(coin, interval, limit)
        if df.empty:
            return df
        # last bar timestamp в ms
        last_bar_t_ms = current_bar_start
        try:
            last_bar_t_ms = int(pd.Timestamp(df["time"].iloc[-1]).value // 10**6)
        except Exception:
            pass
        # Save cache (последний bar обычно in-progress, но мы его в df оставляем
        # как делает HL — bot.patterns_v2 знает про detected_at == последний бар)
        with self._cache_lock:
            self._candles_cache[cache_key] = (last_bar_t_ms, df, time.time())
        return df

    def _fetch_candles_direct(self, coin: str, interval: str, limit: int) -> pd.DataFrame:
        """Прямой fetch_ohlcv без кеша — с retry на 429."""
        for attempt in range(3):
            try:
                ohlcv = self.exchange.fetch_ohlcv(coin, timeframe=interval, limit=limit)
                if not ohlcv:
                    return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close", "Volume"])
                df = pd.DataFrame(ohlcv, columns=["t", "Open", "High", "Low", "Close", "Volume"])
                df["time"] = pd.to_datetime(df["t"], unit="ms", utc=True)
                return df[["time", "Open", "High", "Low", "Close", "Volume"]]
            except ccxt.RateLimitExceeded as e:
                backoff = (2 ** attempt) * (1 + random.uniform(-0.25, 0.25))
                log.warning("Kraken 429 на %s %s — retry через %.1fs (attempt %d/3)",
                            coin, interval, backoff, attempt + 1)
                time.sleep(backoff)
            except Exception as e:
                log.warning("fetch_ohlcv(%s, %s) failed: %s", coin, interval, e)
                return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close", "Volume"])
        return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close", "Volume"])

    def last_4h_volume_usd(self, coin: str) -> float | None:
        """Last closed 4h bar's volume в USD (vol × close) из in-memory кеша.

        Mirror HL: trader.py liquidity_tier_mult читает этот метод. Если кеш пуст
        (cold start / coin не сканировался) → None, caller fall-back на no-adjust.
        """
        with self._cache_lock:
            cached = self._candles_cache.get((coin, "4h"))
        if cached is None:
            return None
        df = cached[1]
        if df.empty or len(df) < 1:
            return None
        try:
            vol = float(df["Volume"].iloc[-1])
            px = float(df["Close"].iloc[-1])
        except (TypeError, ValueError):
            return None
        if vol <= 0 or px <= 0:
            return None
        return vol * px

    def funding_rate(self, coin: str) -> float:
        """Текущий funding rate (per hour). TTL-cached 60s — funding меняется ~1h, 60s stale ок."""
        ttl = float(os.getenv("FUNDING_TTL_SEC", "60"))
        now = time.time()
        with self._cache_lock:
            cached = self._funding_cache.get(coin)
        if cached is not None and (now - cached[1]) < ttl:
            return cached[0]
        try:
            fr = self.exchange.fetch_funding_rate(coin)
            rate = float(fr.get("fundingRate", 0) or 0)
        except Exception as e:
            log.debug("funding_rate(%s) failed: %s", coin, e)
            rate = 0.0
        with self._cache_lock:
            self._funding_cache[coin] = (rate, now)
        return rate

    # Hard cap для CROSS-эмуляции на Kraken Flex.
    # Kraken не имеет true cross — maxLeverage cap определяет per-position margin.
    # При stop=2.5%, leverage>40 → liq быстрее стопа. Cap=10 даёт 4x buffer
    # (liq на -10% при стопе -2.5%). См. SCALING_50K_REPORT.md / DOGE liq incident
    # 2026-05-04.
    KRAKEN_LEVERAGE_CAP = 10

    def asset(self, coin: str) -> AssetMeta:
        """Возвращает AssetMeta — sz_decimals и max_leverage.
        max_leverage берётся из leverage tier cache, НО капится на KRAKEN_LEVERAGE_CAP
        (10x) для безопасности от partial-liquidation на тонких движениях.
        """
        market = self._markets.get(coin) or {}
        precision = market.get("precision", {})
        amt_prec = precision.get("amount", 0.0001)
        # ccxt amount precision = step size (0.0001) → decimals = 4
        import math as _math
        sz_decimals = max(0, int(round(-_math.log10(amt_prec)))) if amt_prec > 0 else 4
        raw_max = self._leverage_cache.get(coin, 10)
        max_leverage = min(raw_max, self.KRAKEN_LEVERAGE_CAP)
        return AssetMeta(name=coin, sz_decimals=sz_decimals, max_leverage=max_leverage)

    # ------ HL-compat helpers ------
    def invalidate_positions_cache(self) -> None:
        """No-op for Kraken (no positions cache like HL)."""
        pass

    # ---------- Mark price (used by signal-freshness gate) ----------
    _mark_cache: tuple[float, dict[str, float]] | None = None

    def mark_price(self, coin: str, ttl: float = 5.0) -> float:
        """Current mark/last price for `coin`. Used by trader.py signal-freshness
        gate. ccxt fetch_ticker → mark or last. Cached short-term to avoid hammering.
        """
        now = time.time()
        cache = KrakenClient._mark_cache
        if cache is not None and (now - cache[0]) < ttl and coin in cache[1]:
            return float(cache[1][coin])
        try:
            t = self.exchange.fetch_ticker(coin)
            # Prefer mark, fall back to last
            px = float(t.get("info", {}).get("markPrice")
                       or t.get("mark") or t.get("last") or t.get("close") or 0)
            if px > 0:
                if cache is None or (now - cache[0]) >= ttl:
                    KrakenClient._mark_cache = (now, {coin: px})
                else:
                    cache[1][coin] = px
                return px
        except Exception as e:
            log.warning("mark_price(%s) failed: %s", coin, e)
        return 0.0

    # ---------- Slip estimate (per side) ----------
    # Used by trader.py LIQUIDITY DRAG CHECK. Flat per-side estimate — tier
    # classification без реальных данных = guessing. Реальная защита от плохих
    # fill'ов — MAX_FILL_SLIP_PCT (post-execution abort, trader.py).
    _fee_rt_estimate = 0.001  # KF taker tier ≈ 0.05% per side, 0.10% RT

    def slip_per_side(self, coin: str, notional_usd: float) -> float:
        del coin, notional_usd
        return 0.0015

    def _to_hl_response(self, ccxt_resp: dict, kind: str = "filled") -> dict:
        """Wrap ccxt order response into HL-shape for trader.py compat.
        kind: 'filled' (market) | 'resting' (trigger/limit)
        """
        avg_px = ccxt_resp.get("average") or ccxt_resp.get("price") or 0
        oid = ccxt_resp.get("id", "")
        sz = ccxt_resp.get("filled") or ccxt_resp.get("amount", 0)
        if kind == "filled":
            status = {"filled": {"avgPx": str(avg_px), "totalSz": str(sz), "oid": oid}}
        else:
            status = {"resting": {"oid": oid}}
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}

    # ------ Orders ------
    def market_open(self, coin: str, is_buy: bool, sz: float) -> dict:
        side = "buy" if is_buy else "sell"
        try:
            resp = self.exchange.create_market_order(coin, side, sz)
            return self._to_hl_response(resp, kind="filled")
        except Exception as e:
            # Return HL-style error response
            return {
                "status": "ok",
                "response": {"type": "order", "data": {
                    "statuses": [{"error": str(e)[:200]}]
                }}
            }

    def market_close(self, coin: str) -> dict:
        # Kraken: используем reduceOnly на market order противоположного направления.
        positions = self.open_positions()
        pos = positions.get(coin)
        if not pos:
            log.warning("market_close: no position for %s", coin)
            return {}
        sz = abs(float(pos["szi"]))
        side = "sell" if float(pos["szi"]) > 0 else "buy"
        return self.exchange.create_market_order(
            coin, side, sz, params={"reduceOnly": True}
        )

    def trigger_sl(self, coin: str, is_buy: bool, sz: float, trigger_px: float) -> dict:
        """Stop-loss = stop-market order с reduceOnly. HL-shaped response."""
        side = "buy" if is_buy else "sell"
        px = self.round_price(coin, trigger_px)
        params = {"stopPrice": px, "reduceOnly": True}
        try:
            resp = self.exchange.create_order(coin, "stop", side, sz, None, params)
            return self._to_hl_response(resp, kind="resting")
        except Exception as e:
            return {"status": "ok", "response": {"type": "order", "data": {
                "statuses": [{"error": str(e)[:200]}]
            }}}

    def trigger_tp(self, coin: str, is_buy: bool, sz: float, trigger_px: float) -> dict:
        side = "buy" if is_buy else "sell"
        px = self.round_price(coin, trigger_px)
        params = {"stopPrice": px, "reduceOnly": True}
        try:
            resp = self.exchange.create_order(coin, "take_profit", side, sz, None, params)
            return self._to_hl_response(resp, kind="resting")
        except Exception as e:
            return {"status": "ok", "response": {"type": "order", "data": {
                "statuses": [{"error": str(e)[:200]}]
            }}}

    def round_price(self, coin: str, px: float) -> float:
        return float(self.exchange.price_to_precision(coin, px))

    # ------ Stubs for HL-specific methods (no-op on Kraken) ------
    def update_leverage(self, coin: str, leverage: int, is_cross: bool = True) -> dict:
        # Cap к KRAKEN_LEVERAGE_CAP (10x) для безопасности от partial-liq.
        # Kraken Flex не поддерживает true cross — каждая позиция держит margin
        # = notional/leverage. При leverage>40 и stop=2.5% — liq до стопа.
        capped = min(leverage, self.KRAKEN_LEVERAGE_CAP)
        if capped != leverage:
            log.info("Kraken: capping leverage %s %dx → %dx (KRAKEN_LEVERAGE_CAP)",
                     coin, leverage, capped)
        try:
            return self.exchange.set_leverage(capped, coin)
        except Exception as e:
            log.warning("set_leverage(%s, %dx) failed: %s", coin, capped, e)
            return None

    def user_fills(self) -> list:
        try:
            return self.exchange.fetch_my_trades(limit=100)
        except Exception as e:
            log.warning("fetch_my_trades failed: %s", e)
            return []

    def compute_realized_pnl(
        self, fills, coin: str, direction: str, size: float, trade_open_iso=None,
    ):
        """Вычислить realized PnL для закрытой Kraken Futures позиции из ccxt fills.

        Kraken Futures fills НЕ содержат realisedPnl в info (в отличие от HL). Поэтому
        парсим opening + closing fills сами:
          - opening side = 'buy' для long, 'sell' для short, до trade_open_iso (с запасом)
          - closing side = противоположное, после trade_open_iso
          - pnl = (avg_close - avg_open) × matched_size  (long)
                  (avg_open - avg_close) × matched_size  (short)
          - минус оценка taker fees 0.10% round-trip (Kraken Futures, BTC tier)

        Если найдены closing fills но нет matching opens (старые fills вытеснены
        из 100-trade окна) — fallback к estimate: считаем pnl от entry-price из DB,
        который должен быть передан в trade record (но мы его тут не видим, поэтому
        возвращаем (None, exit_px) и trader.py решит).
        """
        from datetime import datetime as _dt, timezone as _tz
        if not fills:
            return None, None
        open_ms = 0
        if trade_open_iso:
            try:
                dt = _dt.fromisoformat(trade_open_iso.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                open_ms = int(dt.timestamp() * 1000) - 60000
            except Exception:
                open_ms = 0
        opening_side = "buy" if direction == "long" else "sell"
        closing_side = "sell" if direction == "long" else "buy"

        opens, closes = [], []
        # open_ms = created_at - 60s. Opening fills happen AFTER created_at
        # (insert_trade fires before market_open). Window for opens: [open_ms, open_ms+180s].
        # closing fills: t_ms >= open_ms (anything after creation onward).
        open_window_end = (open_ms + 180_000) if open_ms else 0
        for f in fills:
            if f.get("symbol") != coin:
                continue
            t_ms = int(f.get("timestamp", 0) or 0)
            side = f.get("side")
            if side == closing_side and (not open_ms or t_ms >= open_ms):
                closes.append(f)
            elif side == opening_side and (
                not open_ms or (open_ms <= t_ms <= open_window_end)
            ):
                opens.append(f)
        if not closes:
            return None, None

        def _wavg(fills_list):
            tot_sz = sum(float(f.get("amount", 0) or 0) for f in fills_list)
            if tot_sz <= 0:
                return None, 0.0
            wsum = sum(float(f.get("price", 0) or 0) * float(f.get("amount", 0) or 0)
                       for f in fills_list)
            return wsum / tot_sz, tot_sz

        avg_close, close_sz = _wavg(closes)
        avg_open, open_sz = _wavg(opens) if opens else (None, 0.0)

        # Если open fills не нашлись — pnl нельзя посчитать без entry (вернуть exit_px только)
        if avg_open is None or open_sz <= 0:
            return None, avg_close

        matched = min(close_sz, open_sz)
        if direction == "long":
            gross_pnl = (avg_close - avg_open) * matched
        else:
            gross_pnl = (avg_open - avg_close) * matched

        # Taker fees: ~0.05% per side для Kraken Futures (taker tier)
        notional = (avg_open + avg_close) * matched / 2
        fees = notional * 0.001  # 0.10% round-trip approx
        net_pnl = gross_pnl - fees
        return net_pnl, avg_close

    def spot_usdc(self) -> float:
        """Kraken Futures не имеет spot. Return 0."""
        return 0.0

    @property
    def info(self):
        """HL compat shim: client.info.user_state(...). Тут заглушка."""
        return self  # KrakenClient methods called as if it were HL .info

    def user_state(self, address: str = "") -> dict:
        """HL-compat: возвращаем структуру похожую на HL marginSummary.
        Использует Kraken flex multi-collateral account.
        """
        try:
            balance = self.exchange.fetch_balance()
            info = balance.get("info", {})
            flex = info.get("accounts", {}).get("flex", {})
            ms = {
                "accountValue": str(flex.get("portfolioValue", 0)),
                "totalMarginUsed": str(flex.get("initialMargin", 0)),
                "totalNtlPos": str(flex.get("balanceValue", 0)),
            }
            positions = self.open_positions()
            asset_positions = [{"position": p} for p in positions.values()]
            return {
                "marginSummary": ms,
                "crossMarginSummary": ms,
                "withdrawable": str(flex.get("availableMargin", 0)),
                "crossMaintenanceMarginUsed": str(flex.get("maintenanceMargin", 0)),
                "assetPositions": asset_positions,
            }
        except Exception as e:
            log.error("user_state failed: %s", e)
            return {"marginSummary": {}, "assetPositions": []}

    def spot_user_state(self, address: str = "") -> dict:
        return {"balances": []}

    def cancel_sl_order(self, coin: str, oid) -> dict:
        """Bug fix 2026-05-05: ccxt KF использует cancel_order(id, symbol), не cancel().
        Unified API чтобы trader.py работал на всех биржах одинаково."""
        return self.exchange.cancel_order(oid, coin)

    def list_open_sl_orders(self, coin: str) -> list[str]:
        """Bug fix 2026-05-05: list all active stop orders for a coin (для cleanup orphans)."""
        try:
            orders = self.exchange.fetch_open_orders(coin)
        except Exception:
            return []
        out = []
        for o in orders:
            t = str(o.get("type", "")).lower()
            info_t = str((o.get("info") or {}).get("orderType", "")).lower()
            if "stop" in t or "stop" in info_t:
                out.append(o.get("id"))
        return [oid for oid in out if oid]
