"""Extended Exchange (Starknet) adapter — mirror of HLClient interface.

DRAFT 2026-05-10 — pending testnet 4-step probe per
`feedback_verify_endpoint_returns_oid_not_just_signature`. НЕ использовать в prod
без verify isolated/cross, TPSL POSITION semantics, IOC partial-fill behaviour.

SDK: x10-python-trading-starknet v1.4.1+, async-only. Adapter использует
background event-loop pattern: один daemon-thread держит asyncio loop,
sync-стороне предоставляется .run(coro) bridge для совместимости с остальным
threading-based bot codebase.

Auth: Stark SNIP-12 для writes (через StarkPerpetualAccount.private_key),
X-Api-Key для read. ETH ключ не нужен в runtime — onboarding отдельным
скриптом, после которого .env содержит только api_key + stark keys + vault_id.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import pandas as pd

from bot.config import Settings, TF_MS

log = logging.getLogger(__name__)

# Lazy SDK imports — fail-fast при init если пакет не установлен
try:
    from x10.config import MAINNET_CONFIG, TESTNET_CONFIG
    from x10.core.stark_account import StarkPerpetualAccount
    from x10.clients.rest import RestApiClient
    from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient
    from x10.perpetual.stream_client import PerpetualStreamClient
    from x10.perpetual.order_object import (
        create_order_object, OrderTpslTriggerParam,
    )
    from x10.models.order import (
        OrderSide, OrderType, OrderTpslType, OrderTriggerPriceType,
        OrderPriceType, TimeInForce,
    )
    from x10.errors import ApiError, ApiRateLimitError, ApiNotAuthorizedError
    from x10.utils.order import get_price_with_slippage
    _SDK_OK = True
except ImportError:
    _SDK_OK = False
    log.error("x10-python-trading-starknet not installed — pip install x10-python-trading-starknet")


# Интервалы Extended → ISO 8601 duration
_INTERVAL_MAP = {
    "1m": "PT1M", "5m": "PT5M", "15m": "PT15M", "30m": "PT30M",
    "1h": "PT1H", "2h": "PT2H", "4h": "PT4H", "1d": "P1D",
}


@dataclass(frozen=True)
class AssetMeta:
    name: str
    sz_decimals: int
    max_leverage: int


class _AsyncBridge:
    """Daemon-thread asyncio loop для sync→async моста.

    SDK async-only; threading-based bot codebase зовёт adapter sync.
    Один loop переиспользует aiohttp session (без TCP/TLS handshake на каждый call).
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro, timeout: float = 30.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


class ExtendedClient:
    """Extended Exchange (Starknet) client — mirror HLClient interface."""

    def __init__(self, settings: Settings) -> None:
        if not _SDK_OK:
            raise RuntimeError(
                "x10-python-trading-starknet SDK not installed. "
                "pip install x10-python-trading-starknet"
            )
        self.settings = settings
        self._bridge = _AsyncBridge()

        cfg = MAINNET_CONFIG if settings.network == "mainnet" else TESTNET_CONFIG
        self._cfg = cfg

        self._stark_acc = StarkPerpetualAccount(
            vault=int(settings.extended_vault_id),
            api_key=settings.extended_api_key,
            public_key=settings.extended_stark_public,
            private_key=settings.extended_stark_private,
        )

        # RestApiClient как async context — мы держим его открытым через bg loop.
        # Создание клиента обёрнуто в init coroutine, выполняется через bridge.
        async def _init():
            rest = RestApiClient(cfg, self._stark_acc)
            # __aenter__ вручную чтобы не выходить из контекста
            await rest.__aenter__()
            return rest

        self._client: RestApiClient = self._bridge.run(_init())

        # Trading helper — даёт filled OpenOrderModel из create_and_place_order
        self._trader = BlockingTradingClient(
            api_url=cfg.endpoints.api_url,
            stark_account=self._stark_acc,
            config=cfg,
        )
        self._bridge.run(self._trader.start())

        # Кэши
        self._markets_cache: dict | None = None  # name → MarketModel
        self._candles_cache: dict[tuple[str, str], tuple[int, pd.DataFrame, float]] = {}
        self._mark_cache: dict[str, tuple[float, float]] = {}  # coin → (px, fetched_at)
        self._funding_cache: dict[str, tuple[float, float]] = {}
        self._cache_lock = threading.RLock()
        self._positions_cache: tuple[dict, float] | None = None

        # Стартовая загрузка markets
        self._load_markets()

        log.info(
            "Extended client init: network=%s, vault=%s, %d markets loaded",
            settings.network, settings.extended_vault_id,
            len(self._markets_cache) if self._markets_cache else 0,
        )

    # ===== Internal helpers =====

    def _load_markets(self) -> None:
        async def _go():
            return await self._client.info.get_markets_dict()
        try:
            self._markets_cache = self._bridge.run(_go(), timeout=15)
        except Exception as e:
            log.warning("get_markets_dict failed: %s — будет lazy-load", e)
            self._markets_cache = {}

    def _market(self, coin: str):
        """Возвращает MarketModel; lazy refresh если miss."""
        if not self._markets_cache:
            self._load_markets()
        m = (self._markets_cache or {}).get(coin)
        if not m:
            # На Extended символы вида "BTC-USD"; coin-список в config может быть "BTC"
            m = (self._markets_cache or {}).get(f"{coin}-USD")
        if not m:
            raise KeyError(f"Extended market not found: {coin}")
        return m

    def _to_hl_response(self, sdk_model, kind: str = "filled") -> dict:
        """Wrap SDK response в HL-shape для совместимости с trader.py."""
        if kind == "filled":
            avg_px = getattr(sdk_model, "average_price", None) or getattr(sdk_model, "price", 0)
            sz = getattr(sdk_model, "filled_qty", None) or getattr(sdk_model, "qty", 0)
            oid = getattr(sdk_model, "id", "")
            status = {"filled": {"avgPx": str(avg_px), "totalSz": str(sz), "oid": oid}}
        else:
            oid = getattr(sdk_model, "id", "")
            status = {"resting": {"oid": oid}}
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}

    # ===== Account state =====

    def account_value(self) -> float:
        async def _go():
            r = await self._client.account.get_balance()
            return float(r.data.equity)
        try:
            return self._bridge.run(_go(), timeout=10)
        except Exception as e:
            log.warning("Extended account_value failed: %s", e)
            return 0.0

    def open_positions(self) -> dict:
        # 5s TTL cache как у KF
        with self._cache_lock:
            if self._positions_cache:
                pos, t = self._positions_cache
                if time.time() - t < 5.0:
                    return pos

        async def _go():
            r = await self._client.account.get_positions()
            return r.data

        try:
            sdk_positions = self._bridge.run(_go(), timeout=10)
        except Exception as e:
            log.warning("Extended open_positions failed: %s", e)
            return {}

        out = {}
        for p in sdk_positions:
            sz_signed = float(p.size) if str(p.side).upper() == "LONG" else -float(p.size)
            out[p.market] = {
                "szi": str(sz_signed),
                "entryPx": str(p.open_price),
                "leverage": {"value": int(p.leverage), "type": "cross"},  # cross-only по SDK 1.4.x
                "liquidationPx": str(p.liquidation_price) if p.liquidation_price else None,
                "marginUsed": str(p.value),
                "unrealizedPnl": str(p.unrealised_pnl),
            }
        with self._cache_lock:
            self._positions_cache = (out, time.time())
        return out

    def invalidate_positions_cache(self) -> None:
        with self._cache_lock:
            self._positions_cache = None

    def spot_usdc(self) -> float:
        async def _go():
            r = await self._client.account.get_spot_balances()
            for b in r.data:
                if str(getattr(b, "asset", "")).upper() == "USDC":
                    return float(getattr(b, "balance", 0))
            return 0.0
        try:
            return self._bridge.run(_go(), timeout=10)
        except Exception:
            return 0.0

    # ===== Market data =====

    def candles(self, coin: str, interval: str, limit: int = 200) -> pd.DataFrame:
        iso = _INTERVAL_MAP.get(interval)
        if not iso:
            raise ValueError(f"Extended unsupported interval: {interval}")
        # TODO: реализовать кэш с bar-aligned offset (см. exchange_kraken.py:165)

        async def _go():
            return await self._client.info.get_candles_history(
                market_name=self._market(coin).name,
                candle_type="trades", interval=iso, limit=limit,
            )

        try:
            sdk_candles = self._bridge.run(_go(), timeout=15)
        except Exception as e:
            log.warning("candles(%s,%s) failed: %s", coin, interval, e)
            return pd.DataFrame()

        rows = []
        for c in sdk_candles:
            rows.append({
                "t": int(c.timestamp),
                "o": float(c.open), "h": float(c.high),
                "l": float(c.low), "c": float(c.close),
                "v": float(c.volume),
            })
        df = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
        return df

    def last_4h_volume_usd(self, coin: str) -> float | None:
        # Нет dedicated endpoint — sum последних 4× PT1H candles × close
        df = self.candles(coin, "1h", limit=4)
        if df.empty or len(df) < 4:
            return None
        return float((df["v"] * df["c"]).sum())

    def funding_rate(self, coin: str) -> float:
        # 60s TTL
        with self._cache_lock:
            cached = self._funding_cache.get(coin)
            if cached and time.time() - cached[1] < 60:
                return cached[0]

        async def _go():
            r = await self._client.info.get_market_statistics(
                market_name=self._market(coin).name
            )
            return float(r.data.funding_rate or 0.0)

        try:
            rate = self._bridge.run(_go(), timeout=10)
            with self._cache_lock:
                self._funding_cache[coin] = (rate, time.time())
            return rate
        except Exception as e:
            log.warning("funding_rate(%s) failed: %s", coin, e)
            return 0.0

    def asset(self, coin: str) -> AssetMeta:
        m = self._market(coin)
        tc = m.trading_config
        return AssetMeta(
            name=m.name,
            sz_decimals=int(getattr(tc, "quantity_precision", 4)),
            max_leverage=int(getattr(tc, "max_leverage", 10)),
        )

    def mark_price(self, coin: str, ttl: float = 5.0) -> float:
        with self._cache_lock:
            cached = self._mark_cache.get(coin)
            if cached and time.time() - cached[1] < ttl:
                return cached[0]

        async def _go():
            r = await self._client.info.get_market_statistics(
                market_name=self._market(coin).name
            )
            return float(r.data.mark_price)

        try:
            px = self._bridge.run(_go(), timeout=10)
            with self._cache_lock:
                self._mark_cache[coin] = (px, time.time())
            return px
        except Exception as e:
            log.warning("mark_price(%s) failed: %s", coin, e)
            return 0.0

    def slip_per_side(self, coin: str, notional_usd: float) -> float:
        # TODO: walk orderbook для accurate slippage; пока fallback на default 0.001 (10bps)
        # Implementation: c.info.get_orderbook_snapshot(market_name=coin).data.{bid,ask}
        # walk levels until cumulative qty*price >= notional_usd
        return 0.001

    def round_price(self, coin: str, px: float) -> float:
        m = self._market(coin)
        try:
            return float(m.trading_config.round_price(Decimal(str(px))))
        except Exception:
            return px

    # ===== Orders =====

    def market_open(self, coin: str, is_buy: bool, sz: float) -> dict:
        """Market entry через BlockingTradingClient (filled OpenOrderModel returned)."""
        side = OrderSide.BUY if is_buy else OrderSide.SELL
        m = self._market(coin)

        async def _go():
            r = await self._trader.create_and_place_order(
                market_name=m.name, side=side,
                amount_of_synthetic=Decimal(str(sz)),
                # tif default IOC для market
            )
            return r  # OpenOrderModel с average_price, filled_qty, id, external_id

        try:
            opened = self._bridge.run(_go(), timeout=20)
            return self._to_hl_response(opened, kind="filled")
        except Exception as e:
            return {
                "status": "ok",
                "response": {"type": "order", "data": {
                    "statuses": [{"error": str(e)[:200]}]
                }},
            }

    def market_close(self, coin: str) -> dict:
        positions = self.open_positions()
        pos = positions.get(coin) or positions.get(self._market(coin).name)
        if not pos:
            log.warning("market_close: no position for %s", coin)
            return {}
        sz_signed = float(pos["szi"])
        if sz_signed == 0:
            return {}
        sz = abs(sz_signed)
        side = OrderSide.SELL if sz_signed > 0 else OrderSide.BUY
        m = self._market(coin)

        async def _go():
            return await self._trader.create_and_place_order(
                market_name=m.name, side=side,
                amount_of_synthetic=Decimal(str(sz)),
                reduce_only=True,
            )

        try:
            closed = self._bridge.run(_go(), timeout=20)
            return self._to_hl_response(closed, kind="filled")
        except Exception as e:
            return {
                "status": "ok",
                "response": {"type": "order", "data": {
                    "statuses": [{"error": str(e)[:200]}]
                }},
            }

    def trigger_sl(self, coin: str, is_buy: bool, sz: float, trigger_px: float) -> dict:
        """Position-wide SL через TPSL POSITION (не synthetic_amount-based).

        is_buy = направление CLOSE side (т.е. для long-position SL это is_buy=False).
        Validator (order_object.py:190-201) требует amount_of_synthetic=0, price=0,
        reduce_only=True для tp_sl_type=POSITION.
        """
        side = OrderSide.BUY if is_buy else OrderSide.SELL
        m = self._market(coin)
        px = self.round_price(coin, trigger_px)

        async def _go():
            order = create_order_object(
                account=self._stark_acc, market=m,
                starknet_domain=self._cfg.signing.starknet_domain,
                order_type=OrderType.TPSL, time_in_force=TimeInForce.GTT,
                side=side,
                amount_of_synthetic=Decimal(0),
                price=Decimal(0),
                reduce_only=True,
                tp_sl_type=OrderTpslType.POSITION,
                stop_loss=OrderTpslTriggerParam(
                    trigger_price=Decimal(str(px)),
                    trigger_price_type=OrderTriggerPriceType.MARK,
                    price=Decimal(str(px)),
                    price_type=OrderPriceType.MARKET,
                ),
            )
            return await self._client.orders.place_order(order)

        try:
            placed = self._bridge.run(_go(), timeout=15)
            return self._to_hl_response(placed.data, kind="resting")
        except Exception as e:
            return {
                "status": "ok",
                "response": {"type": "order", "data": {
                    "statuses": [{"error": str(e)[:200]}]
                }},
            }

    def trigger_tp(self, coin: str, is_buy: bool, sz: float, trigger_px: float) -> dict:
        side = OrderSide.BUY if is_buy else OrderSide.SELL
        m = self._market(coin)
        px = self.round_price(coin, trigger_px)

        async def _go():
            order = create_order_object(
                account=self._stark_acc, market=m,
                starknet_domain=self._cfg.signing.starknet_domain,
                order_type=OrderType.TPSL, time_in_force=TimeInForce.GTT,
                side=side,
                amount_of_synthetic=Decimal(0),
                price=Decimal(0),
                reduce_only=True,
                tp_sl_type=OrderTpslType.POSITION,
                take_profit=OrderTpslTriggerParam(
                    trigger_price=Decimal(str(px)),
                    trigger_price_type=OrderTriggerPriceType.MARK,
                    price=Decimal(str(px)),
                    price_type=OrderPriceType.MARKET,
                ),
            )
            return await self._client.orders.place_order(order)

        try:
            placed = self._bridge.run(_go(), timeout=15)
            return self._to_hl_response(placed.data, kind="resting")
        except Exception as e:
            return {
                "status": "ok",
                "response": {"type": "order", "data": {
                    "statuses": [{"error": str(e)[:200]}]
                }},
            }

    def cancel_sl_order(self, coin: str, oid) -> dict:
        async def _go():
            return await self._client.orders.cancel_order(int(oid))
        try:
            self._bridge.run(_go(), timeout=10)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}

    def list_open_sl_orders(self, coin: str) -> list:
        m = self._market(coin)

        async def _go():
            r = await self._client.account.get_open_orders(
                market_names=[m.name], order_type=OrderType.TPSL,
            )
            return [str(o.id) for o in r.data]

        try:
            return self._bridge.run(_go(), timeout=10)
        except Exception as e:
            log.warning("list_open_sl_orders(%s) failed: %s", coin, e)
            return []

    def update_leverage(self, coin: str, leverage: int, is_cross: bool = True) -> Optional[dict]:
        # SDK 1.4.x не поддерживает isolated/cross flag — assume cross-only (verify testnet!)
        if not is_cross:
            log.warning("Extended SDK 1.4.x does not expose isolated mode — leveraging cross")
        m = self._market(coin)

        async def _go():
            return await self._client.account.update_leverage(
                market_name=m.name, leverage=Decimal(int(leverage)),
            )

        try:
            self._bridge.run(_go(), timeout=10)
            return {"status": "ok"}
        except Exception as e:
            log.warning("update_leverage(%s, %dx) failed: %s", coin, leverage, e)
            return None

    def user_fills(self) -> list:
        async def _go():
            r = await self._client.account.get_trades()
            return r.data
        try:
            sdk_fills = self._bridge.run(_go(), timeout=15)
        except Exception as e:
            log.warning("user_fills failed: %s", e)
            return []
        # TODO: map в HL-style fills shape (см. exchange.py:819 / exchange_kraken.py:424)
        return [
            {
                "coin": f.market,
                "side": "B" if str(f.side).upper() == "BUY" else "A",
                "px": str(f.price),
                "sz": str(f.qty),
                "time": int(f.timestamp),
                "oid": f.id,
            }
            for f in sdk_fills
        ]

    # ===== HL compat shims =====

    @property
    def info(self):
        """HL-compat: client.info.user_state() — заглушка."""
        return self

    def user_state(self, address: str = "") -> dict:
        """HL-compat: marginSummary shape."""
        async def _go():
            return (await self._client.account.get_balance()).data

        try:
            bal = self._bridge.run(_go(), timeout=10)
        except Exception:
            return {}
        ms = {
            "accountValue": str(bal.equity),
            "totalMarginUsed": str(bal.initial_margin),
            "totalNtlPos": str(bal.equity),  # TODO: точнее через positions sum
        }
        positions = self.open_positions()
        asset_positions = [{"position": p} for p in positions.values()]
        return {
            "marginSummary": ms,
            "crossMarginSummary": ms,
            "withdrawable": str(bal.available_for_withdrawal),
            "crossMaintenanceMarginUsed": "0",  # SDK не expose'ит maintenance отдельно
            "assetPositions": asset_positions,
        }


# ===== Pending TODOs (testnet probe) =====
# 1. Verify TPSL POSITION закрывает всю позицию post-trigger (synthetic_amount=0)
# 2. Verify cross-only assumption — попробовать update_leverage с isolated mode
# 3. Implement WS subscription для live position/order/fill updates (push в queue.Queue)
# 4. WS auto-reconnect: recursive pattern по примеру BlockingTradingClient.___order_stream
# 5. ApiRateLimitError backoff (нет Retry-After parsing в SDK)
# 6. IOC market partial-fill behaviour: probe oversized order, какой signal
# 7. Реализовать candles cache с bar-aligned offset (mirror exchange_kraken.py:165)
# 8. slip_per_side через get_orderbook_snapshot (сейчас fallback 0.001)
# 9. Add `extended_vault_id: int` в Settings dataclass + .env.extended.example
