"""Nado perpetual DEX client (Ink L2) — exchange-agnostic interface for trader.py.

Wraps the official `nado-protocol` Python SDK to expose the same shape that
HLClient and KrakenClient use, so trader.py / manage_open_positions / watchdog
work unchanged.

Design choices:
- Coin id format = native Nado symbol e.g. "BTC-PERP", "ETH-PERP". Set this
  in `COINS` env var.
- Subaccount = main wallet + "default" tag (1CT setup from app.nado.xyz UI).
- Linked Signer pattern: SDK signer key = the agent (1CT) private key extracted
  from the Nado UI localStorage. Main wallet stays in Rabby.
- All Nado prices/amounts are x18-scaled big-int strings — we encapsulate all
  conversions here.
"""
from __future__ import annotations

import logging
import time
import warnings
from dataclasses import dataclass

import pandas as pd

# Suppress noisy pkg_resources DeprecationWarning from eth_keyfile
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

from nado_protocol.client import (
    NadoClient,
    NadoClientMode,
    create_nado_client,
)
from nado_protocol.engine_client.types.execute import (
    PlaceMarketOrderParams,
    CancelOrdersParams,
)
from nado_protocol.trigger_client.types.execute import (
    CancelTriggerOrdersParams,
)
from nado_protocol.utils.execute import MarketOrderParams
from nado_protocol.utils.bytes32 import subaccount_to_hex
from nado_protocol.indexer_client.types.query import (
    IndexerCandlesticksParams,
    IndexerMatchesParams,
)
from nado_protocol.indexer_client.types.models import IndexerCandlesticksGranularity

from bot.config import CANDLES_LIMIT, TF_MS, Settings

log = logging.getLogger(__name__)


def _is_order_not_found_2020(exc: Exception) -> bool:
    """True if Nado SDK exception body indicates error_code=2020 ("order not
    found"). Body comes through as a JSON-shaped string in exception args.

    Examples of matched bodies:
      {"status":"failure", ..., "error_code":2020, "error":"Order with the
       provided digest (...) could not be found. ..."}
    """
    try:
        msg = str(exc)
    except Exception:
        return False
    return ('"error_code":2020' in msg) or ("'error_code': 2020" in msg) \
        or ("error_code=2020" in msg)


# Granularity translation
_TF_TO_GRANULARITY: dict[str, IndexerCandlesticksGranularity] = {
    "1m": IndexerCandlesticksGranularity.ONE_MINUTE,
    "5m": IndexerCandlesticksGranularity.FIVE_MINUTES,
    "15m": IndexerCandlesticksGranularity.FIFTEEN_MINUTES,
    "1h": IndexerCandlesticksGranularity.ONE_HOUR,
    "2h": IndexerCandlesticksGranularity.TWO_HOURS,
    "4h": IndexerCandlesticksGranularity.FOUR_HOURS,
    "1d": IndexerCandlesticksGranularity.ONE_DAY,
    "1w": IndexerCandlesticksGranularity.ONE_WEEK,
}

# 1e18 — Nado's universal scaling factor
X18 = 10**18


@dataclass(frozen=True)
class AssetMeta:
    """HL-compat meta tuple. Nado's max_leverage we look up from market data
    if available, else default 20x (Nado standard for most perps)."""
    name: str
    sz_decimals: int
    max_leverage: int
    product_id: int
    tick_size: float
    min_size: float           # in base asset units (e.g. 100 LINK)
    size_increment_x18: int = 0  # raw wei, used for integer-grid alignment


def _to_x18(v: float | int) -> str:
    """Convert float to x18-scaled big-int string."""
    return str(int(round(v * X18)))


def _from_x18(s: str | int | float) -> float:
    """Convert x18 big-int string to float."""
    if s is None:
        return 0.0
    return int(s) / X18


class _ExchangeShim:
    """Wraps parent so trader.py / watchdog can call client.exchange.X uniformly:
    - cancel(coin, oid)
    - fetch_open_orders() — returns ccxt-shape list (used by watchdog_kraken)
    Trader/watchdog don't need to know whether it's HL or Nado underneath."""

    def __init__(self, parent: "NadoClient_"):
        self.parent = parent

    def cancel(self, coin: str, oid):
        return self.parent.cancel_order(coin, oid)

    def fetch_open_orders(self):
        """ccxt-shape: list of {symbol, type, side, amount, reduceOnly, ...}.
        Used by watchdog_kraken.py to detect existing SL trigger orders.
        Watchdog cares about: symbol, type ('stop'/'trigger'/...), reduceOnly.
        """
        return self.parent.fetch_open_orders_ccxt_shape()


class _InfoShim:
    """Wraps parent for HL-style `client.info.user_state(addr)` and
    `client.info.frontend_open_orders(addr)` calls used by trader / watchdog."""

    def __init__(self, parent: "NadoClient_"):
        self.parent = parent

    def user_state(self, _address: str = "") -> dict:
        return self.parent.user_state()

    def spot_user_state(self, _address: str = "") -> dict:
        return self.parent.spot_user_state()

    def frontend_open_orders(self, _address: str = "") -> list:
        return self.parent.open_orders()


class NadoClient_:
    """Thin wrapper around nado-protocol SDK with HL-shape interface.

    Note class name has trailing underscore to avoid collision with the SDK's
    own `NadoClient` (we import it but don't expose it publicly).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        # Connect to mainnet via SDK helper (handles contracts auto-discovery)
        mode = NadoClientMode.MAINNET if settings.network == "mainnet" else NadoClientMode.TESTNET
        self._sdk: NadoClient = create_nado_client(
            mode=mode, signer=settings.agent_private_key
        )

        # Subaccount = main wallet + "default" tag (set up via 1CT UI)
        self.main_wallet = settings.account_address
        self.subaccount_name = settings.nado_subaccount or "default"
        self.subaccount_hex = subaccount_to_hex(self.main_wallet, self.subaccount_name)
        log.info(
            "Nado client init: signer=%s wallet=%s subaccount=%s",
            self._sdk.context.signer.address,
            self.main_wallet,
            self.subaccount_hex,
        )

        # Cache product_id ↔ symbol map at startup
        self._symbol_to_pid: dict[str, int] = {}
        self._pid_to_symbol: dict[int, str] = {}
        self._meta_cache: dict[str, AssetMeta] = {}
        self._load_markets()

        # State caches
        self._candles_cache: dict[tuple[str, str], tuple[int, pd.DataFrame]] = {}
        self._positions_cache: tuple[float, dict[str, dict]] | None = None
        self._fills_cache: tuple[float, list[dict]] | None = None
        self._summary_cache: tuple[float, object] | None = None  # SubaccountInfoData
        self._mark_cache: tuple[float, dict[str, float]] | None = None

        # Shims for HL-compat surface
        self.exchange = _ExchangeShim(self)
        self.info = _InfoShim(self)

    # ------------ Markets / metadata ------------

    def _load_markets(self) -> None:
        """One-time load: build product_id ↔ symbol maps and per-asset metadata."""
        try:
            symbols = self._sdk.market.get_all_product_symbols()
        except Exception as e:
            log.error("Failed to load Nado markets: %s", e)
            return

        for s in symbols:
            sym = s.symbol
            pid = s.product_id
            self._symbol_to_pid[sym] = pid
            self._pid_to_symbol[pid] = sym

        # Get richer metadata (tick + min_size) via engine get_symbols.
        # Nado сохранил Vertex-style scaling: amount передаётся в size_increments
        # (а не базовых units). min_size = 100 значит 100 increments.
        # min_notional_base = min_size * (size_increment / 1e18).
        try:
            details = self._sdk.context.engine_client.get_symbols(product_type="perp")
            items = details.symbols if hasattr(details, "symbols") else details
            if isinstance(items, dict):
                items = list(items.values())
            for d in items:
                row = d.dict() if hasattr(d, "dict") else d
                sym = row.get("symbol")
                pid = row.get("product_id")
                if not sym or pid is None:
                    continue
                pid = int(pid)
                tick = _from_x18(row.get("price_increment_x18", 0)) or 0.0001
                # `size_increment` is raw wei (×1e18), but `min_size` is
                # actually COUNT of increments × 1e18 (Vertex/Nado encoding).
                # Bug fix 2026-05-06: Раньше делили min_size на 1e18 и думали
                # что это base units → получалось 100 BTC, 100 ETH, 100 LINK
                # (~$8M, $300k, $2k respectively). Реально нужно (count) × (size_inc).
                # Для BTC: 100 × 5e-05 = 0.005 BTC (~$400). Куча trades
                # отклонялась с "below_min_size" хотя реально проходила бы.
                size_inc_x18 = int(row.get("size_increment", 0)) or 100000000000000  # 0.0001 default
                min_size_count_raw = int(row.get("min_size", 0))
                size_inc = size_inc_x18 / X18
                if min_size_count_raw > 0:
                    min_count = min_size_count_raw / X18  # обычно 100
                    min_base = min_count * size_inc
                else:
                    min_base = 0.001  # fallback
                sz_decimals = max(0, -int(round(__import__("math").log10(size_inc)))) if size_inc > 0 else 4
                self._meta_cache[sym] = AssetMeta(
                    name=sym,
                    sz_decimals=sz_decimals,
                    max_leverage=20,
                    product_id=pid,
                    tick_size=tick,
                    min_size=min_base,
                    size_increment_x18=size_inc_x18,
                )
        except Exception as e:
            log.warning("get_symbols(perp) failed (%s) — using defaults", e)

        log.info("Nado markets loaded: %d products (%d perps with meta)",
                 len(self._symbol_to_pid), len(self._meta_cache))

    def asset(self, coin: str) -> AssetMeta:
        if coin in self._meta_cache:
            return self._meta_cache[coin]
        if coin in self._symbol_to_pid:
            # Fallback meta with defaults
            return AssetMeta(
                name=coin, sz_decimals=4, max_leverage=20,
                product_id=self._symbol_to_pid[coin],
                tick_size=0.0001, min_size=0.001,
            )
        raise KeyError(coin)

    def _pid(self, coin: str) -> int:
        try:
            return self._symbol_to_pid[coin]
        except KeyError:
            raise KeyError(f"Unknown Nado symbol: {coin}")

    # ------------ Candles ------------

    def candles(self, coin: str, interval: str, limit: int = CANDLES_LIMIT) -> pd.DataFrame:
        """OHLCV DataFrame with columns: time, Open, High, Low, Close, Volume.
        Bar-aligned cache like HL — pull only when the current bar boundary changes."""
        granularity = _TF_TO_GRANULARITY.get(interval)
        if granularity is None:
            raise ValueError(f"Unsupported timeframe for Nado: {interval}")
        ms = TF_MS[interval]
        now_ms = int(time.time() * 1000)
        current_bar_start = (now_ms // ms) * ms
        cache_key = (coin, interval)
        cached = self._candles_cache.get(cache_key)
        if cached is not None and cached[0] == current_bar_start:
            return cached[1].copy()

        pid = self._pid(coin)
        try:
            data = self._sdk.context.indexer_client.get_candlesticks(
                IndexerCandlesticksParams(
                    product_id=pid, granularity=granularity, limit=limit + 2,
                )
            )
        except Exception as e:
            log.warning("get_candlesticks(%s, %s) failed: %s", coin, interval, e)
            return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close", "Volume"])

        rows = data.candlesticks if hasattr(data, "candlesticks") else (data if isinstance(data, list) else [])
        if not rows:
            return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close", "Volume"])

        records = []
        for r in rows:
            # Nado IndexerCandlestick: open_x18, high_x18, low_x18, close_x18, volume, timestamp
            records.append({
                "time": pd.to_datetime(int(r.timestamp), unit="s", utc=True),
                "Open": _from_x18(r.open_x18),
                "High": _from_x18(r.high_x18),
                "Low": _from_x18(r.low_x18),
                "Close": _from_x18(r.close_x18),
                "Volume": _from_x18(r.volume) if hasattr(r, "volume") else 0.0,
            })
        df = pd.DataFrame(records).sort_values("time").reset_index(drop=True)
        # Drop the still-forming bar (matches HL behavior)
        if len(df) > 1:
            df = df.iloc[:-1].reset_index(drop=True)
        self._candles_cache[cache_key] = (current_bar_start, df)
        return df

    # ------------ Funding ------------

    def funding_rate(self, coin: str) -> float:
        """Nado has funding for perps. Stub returns 0 for now (not used by Vstop strategy)."""
        return 0.0

    # ------------ Account state ------------

    def _summary(self, ttl: float = 5.0):
        """Cached SubaccountInfoData."""
        now = time.time()
        if self._summary_cache and now - self._summary_cache[0] < ttl:
            return self._summary_cache[1]
        try:
            s = self._sdk.subaccount.get_engine_subaccount_summary(subaccount=self.subaccount_hex)
            self._summary_cache = (now, s)
            return s
        except Exception as e:
            log.warning("get_engine_subaccount_summary failed: %s", e)
            return self._summary_cache[1] if self._summary_cache else None

    def account_value(self) -> float:
        """Total equity in USDT0 (= sum of all balances at oracle prices, minus liabilities).
        Nado returns this directly via healths[0].health (cross-margin total)."""
        s = self._summary()
        if s is None:
            return 0.0
        try:
            # healths[0] = cross-margin health; .health = net equity after liabilities
            return _from_x18(s.healths[0].health)
        except Exception as e:
            log.warning("account_value parse failed: %s", e)
            return 0.0

    def spot_usdc(self) -> float:
        """USDT0 balance (Nado's settlement asset, product_id=0)."""
        s = self._summary()
        if s is None:
            return 0.0
        for b in (s.spot_balances or []):
            if b.product_id == 0:
                return _from_x18(b.balance.amount)
        return 0.0

    def open_positions(self, ttl: float = 20.0) -> dict[str, dict]:
        """Returns {coin: {"szi": signed_size, "entryPx": avg_entry, "coin": coin}}.
        Mirror of HL format so trader.py works unchanged."""
        now = time.time()
        if self._positions_cache and now - self._positions_cache[0] < ttl:
            return self._positions_cache[1]
        s = self._summary(ttl=2.0)
        out: dict[str, dict] = {}
        if s is None:
            return out
        for b in (s.perp_balances or []):
            try:
                amount = _from_x18(b.balance.amount)
                if abs(amount) < 1e-12:
                    continue
                pid = b.product_id
                sym = self._pid_to_symbol.get(pid, f"PID_{pid}")
                v_quote = _from_x18(b.balance.v_quote_balance)  # negative for long, positive for short
                # entry_px = abs(v_quote / amount). v_quote is opposite sign of position.
                entry_px = abs(v_quote / amount) if abs(amount) > 0 else 0.0
                out[sym] = {
                    "coin": sym,
                    "szi": str(amount),
                    "entryPx": str(entry_px),
                    "product_id": pid,
                }
            except Exception as e:
                log.warning("open_positions parse %s: %s", b, e)
        self._positions_cache = (now, out)
        return out

    def invalidate_positions_cache(self) -> None:
        self._positions_cache = None
        self._summary_cache = None

    def position_liquidation(self, coin: str) -> dict | None:
        """{liq_px, margin_mode, leverage} для open позиции. None если позы нет.

        Nado/Vertex: cross-only health system на subaccount уровне. Per-position
        liq_px нативно нет — liq происходит когда healths[1].health (maint) < 0.
        Marginal liq_px = "при какой цене этого coin'а maint health = 0,
        если остальное держится". Long: liq = entry - h1.health/size_signed.

        margin_mode всегда 'cross' для бот-subaccount (isolated = отдельный
        subaccount, бот его не использует).
        """
        s = self._summary()
        if s is None:
            return None
        if not s.healths or len(s.healths) < 2:
            return None
        try:
            h1_health = _from_x18(s.healths[1].health)  # USD buffer (maintenance)
        except Exception:
            return None
        for b in (s.perp_balances or []):
            try:
                pid = b.product_id
                sym = self._pid_to_symbol.get(pid, f"PID_{pid}")
                if sym != coin:
                    continue
                amt = _from_x18(b.balance.amount)
                if abs(amt) < 1e-12:
                    return None
                v_quote = _from_x18(b.balance.v_quote_balance)
                entry_px = abs(v_quote / amt) if abs(amt) > 0 else 0.0
                # Marginal liq: при движении цены этого coin'а на ΔP, P&L Δ = amt × ΔP.
                # Liq когда h1_health + amt × (X - entry) ≤ 0 (для long amt>0).
                # Для long: X = entry - h1_health / amt
                # Для short (amt<0): X = entry + h1_health / |amt|
                if amt > 0:
                    liq_px = entry_px - h1_health / amt
                else:
                    liq_px = entry_px + h1_health / abs(amt)
                if liq_px < 0:
                    liq_px = 0.0
                return {
                    "liq_px": float(liq_px),
                    "margin_mode": "cross",
                    "leverage": 0,  # Nado has no per-product leverage knob
                }
            except Exception as e:
                log.warning("position_liquidation %s parse: %s", coin, e)
                return None
        return None

    def user_state(self) -> dict:
        """HL-compat: returns marginSummary + assetPositions shape so trader's
        margin-cap check works."""
        s = self._summary()
        if s is None:
            return {"marginSummary": {}, "assetPositions": []}
        equity = self.account_value()
        # Approx total margin used: sum |v_quote| / avg leverage 20x
        total_notional = 0.0
        asset_positions = []
        for b in (s.perp_balances or []):
            try:
                amt = _from_x18(b.balance.amount)
                if abs(amt) < 1e-12:
                    continue
                v_quote = _from_x18(b.balance.v_quote_balance)
                total_notional += abs(v_quote)
                pid = b.product_id
                sym = self._pid_to_symbol.get(pid, f"PID_{pid}")
                asset_positions.append({"position": {
                    "coin": sym,
                    "szi": str(amt),
                    "entryPx": str(abs(v_quote / amt)) if abs(amt) > 0 else "0",
                }})
            except Exception:
                continue
        # Conservative: assume 20x leverage average → margin = notional / 20
        margin_used = total_notional / 20.0
        ms = {
            "accountValue": str(equity),
            "totalMarginUsed": str(margin_used),
            "totalNtlPos": str(total_notional),
        }
        return {
            "marginSummary": ms,
            "crossMarginSummary": ms,
            "withdrawable": str(equity - margin_used),
            "assetPositions": asset_positions,
        }

    def spot_user_state(self) -> dict:
        s = self._summary()
        if s is None:
            return {"balances": []}
        out = []
        for b in (s.spot_balances or []):
            try:
                amount = _from_x18(b.balance.amount)
                if abs(amount) < 1e-12:
                    continue
                pid = b.product_id
                sym = self._pid_to_symbol.get(pid, "USDT0" if pid == 0 else f"PID_{pid}")
                out.append({"coin": sym, "total": str(amount), "hold": "0"})
            except Exception:
                continue
        return {"balances": out}

    def fetch_open_orders_ccxt_shape(self) -> list:
        """ccxt-shape list of OPEN ORDERS (incl. SL trigger orders).
        Used by watchdog_kraken to detect SL coverage per position.
        Watchdog cares about: symbol, type ('stop'/'trigger'), reduceOnly.

        Implementation: query Nado trigger service, filter to ACTIVE statuses only.
        Cancelled/triggered/error are excluded — they're not "open" SLs anymore.
        """
        out = []
        try:
            from nado_protocol.trigger_client.types.query import (
                ListTriggerOrdersParams, ListTriggerOrdersTx,
            )
            tx = ListTriggerOrdersTx(
                sender=self.subaccount_hex,
                recvTime=int(time.time() * 1000) + 60_000,
            )
            # Only request "active" statuses — Nado returns these as enum/string
            params = ListTriggerOrdersParams(
                tx=tx, limit=100,
                status_types=["waiting_price", "waiting_dependency", "triggering"],
            )
            resp = self._sdk.context.trigger_client.list_trigger_orders(params)
            data = resp.data if hasattr(resp, "data") else resp
            triggers = data.orders if hasattr(data, "orders") else []
            for t in triggers:
                try:
                    # Defensive: even with status filter, double-check status
                    status = t.status
                    status_str = str(status).lower() if not isinstance(status, str) else status.lower()
                    if any(bad in status_str for bad in ("cancelled", "triggered", "error", "executing", "completed")):
                        continue
                    od = t.order
                    pid = od.product_id
                    sym = self._pid_to_symbol.get(pid, f"PID_{pid}")
                    amount = int(od.order.amount) if hasattr(od.order, "amount") else 0
                    out.append({
                        "symbol": sym,
                        "coin": sym,
                        "type": "trigger",
                        "reduceOnly": True,
                        "side": "buy" if amount > 0 else "sell",
                        "amount": abs(amount) / X18,
                        "info": {"product_id": pid, "digest": getattr(od, "digest", None), "status": status_str},
                    })
                except Exception as e:
                    log.warning("trigger order parse: %s", e)
        except Exception as e:
            log.warning("fetch_open_orders trigger query failed: %s", e)
        return out

    def open_orders(self) -> list:
        """HL-shape open orders list (used by watchdog to find SL trigger orders)."""
        try:
            orders_per_product = self._sdk.context.engine_client.get_subaccount_multi_products_open_orders(
                subaccount=self.subaccount_hex,
                product_ids=list(self._pid_to_symbol.keys()),
            )
        except Exception as e:
            log.warning("get_subaccount_multi_products_open_orders failed: %s", e)
            return []
        out = []
        # Response shape: list of {product_id, orders: [{...}]}
        try:
            entries = orders_per_product.product_orders if hasattr(orders_per_product, "product_orders") else []
            for entry in entries:
                pid = entry.product_id
                sym = self._pid_to_symbol.get(pid, f"PID_{pid}")
                for o in (entry.orders or []):
                    out.append({
                        "coin": sym,
                        "oid": getattr(o, "digest", None),
                        "limitPx": str(_from_x18(o.priceX18)) if hasattr(o, "priceX18") else "0",
                        "sz": str(abs(_from_x18(o.amount))) if hasattr(o, "amount") else "0",
                        "side": "B" if hasattr(o, "amount") and int(o.amount) > 0 else "A",
                        "isTrigger": False,  # plain open orders, not SL
                        "reduceOnly": False,
                        "product_id": pid,
                    })
        except Exception as e:
            log.warning("open_orders parse: %s", e)
        # Trigger orders (SL) live in a separate service — placeholder for future fetch.
        return out

    # ------------ Mark / mid ------------

    def mark_price(self, coin: str, ttl: float = 5.0) -> float:
        now = time.time()
        if self._mark_cache and now - self._mark_cache[0] < ttl:
            cached = self._mark_cache[1].get(coin)
            if cached:
                return cached
        try:
            pid = self._pid(coin)
            mp = self._sdk.context.engine_client.get_market_price(pid)
            mid = (_from_x18(mp.bid_x18) + _from_x18(mp.ask_x18)) / 2
            cache = self._mark_cache[1] if self._mark_cache else {}
            cache[coin] = mid
            self._mark_cache = (now, cache)
            return mid
        except Exception as e:
            log.warning("mark_price(%s) failed: %s", coin, e)
            return 0.0

    # ------------ Slip estimate (per side) ------------
    # Used by trader.py LIQUIDITY DRAG CHECK. Nado/Vertex perps на Ink L2 имеют
    # тонкий orderbook на не-major coins → conservative defaults. Real-world:
    # 2026-05-06 наблюдали -1.22% slip на FARTCOIN-PERP (тонкий стакан).
    # Без историч. fills per-coin (limited Nado bot trading history) тут
    # tier-defaults; в будущем может быть `data/nado_slippage.json`.
    _fee_rt_estimate = 0.001  # Nado taker ≈ 0.05% per side, 0.10% RT

    def slip_per_side(self, coin: str, notional_usd: float) -> float:
        """Conservative slippage estimate per side (fraction of notional)."""
        del notional_usd
        majors = {"BTC-PERP", "ETH-PERP", "SOL-PERP", "XRP-PERP", "DOGE-PERP",
                  "BNB-PERP", "LTC-PERP", "BCH-PERP", "ADA-PERP", "AVAX-PERP"}
        mids = {"LINK-PERP", "ARB-PERP", "OP-PERP", "MATIC-PERP", "ATOM-PERP",
                "NEAR-PERP", "AAVE-PERP", "TRX-PERP", "XMR-PERP", "UNI-PERP",
                "ZEC-PERP", "ETC-PERP", "ENA-PERP", "kPEPE-PERP", "ONDO-PERP",
                "HYPE-PERP"}
        if coin in majors:
            return 0.002
        if coin in mids:
            return 0.004
        return 0.008  # thin/exotic: 0.8% per side

    # ------------ Price rounding ------------

    def round_price(self, coin: str, px: float) -> float:
        """Round to product's tick_size."""
        try:
            tick = self.asset(coin).tick_size
        except KeyError:
            return px
        if tick <= 0:
            return px
        return round(round(px / tick) * tick, 12)

    # ------------ Trading ------------

    def update_leverage(self, coin: str, leverage: int, is_cross: bool = True) -> dict:
        """Nado uses cross-margin by default per subaccount. There's no per-product
        leverage knob on the standard subaccount (isolated subaccounts are separate).
        Return a dummy-success dict to keep trader.py happy."""
        return {"status": "ok", "response": {"type": "noop", "data": {}}}

    def market_open(self, coin: str, is_buy: bool, sz: float) -> dict:
        """Place a market order. `sz` in base asset units (e.g. 10 LINK).

        VERIFIED LIVE 03-05-2026: Nado `amount` is base × 1e18 wei. Must be exact
        multiple of `size_increment` (in wei, NOT base) — float math will introduce
        rounding error, so we snap on the integer grid using floor-division.

        REJECT if intended size < min_size — silently bumping up would breach
        risk_per_trade limit (Nado has high mins relative to small-cap equity).
        """
        try:
            meta = self.asset(coin)
            pid = meta.product_id
            inc_x18 = meta.size_increment_x18 or int(round(self._size_increment_for(coin) * X18))
            min_x18 = int(round(meta.min_size * X18))
            target_x18 = int(round(sz * X18))
            if target_x18 < min_x18:
                msg = (f"size {sz} < Nado min {meta.min_size} for {coin}. "
                       f"Risk per trade too small for this market — skip")
                log.warning("Nado REJECT %s: %s", coin, msg)
                return _error_resp(f"below_min_size: {msg}")
            amount_x18 = (target_x18 // inc_x18) * inc_x18
            amount_signed = amount_x18 if is_buy else -amount_x18
            params = PlaceMarketOrderParams(
                product_id=pid,
                market_order=MarketOrderParams(
                    sender=self.subaccount_hex,
                    amount=amount_signed,
                ),
                slippage=self.settings.slippage,
                reduce_only=False,
            )
            sz_actual = amount_x18 / X18
            log.info(
                "Nado market_open: %s sz=%s → snapped %s (amount_x18=%d, inc_x18=%d, is_buy=%s)",
                coin, sz, sz_actual, amount_signed, inc_x18, is_buy,
            )
            resp = self._sdk.market.place_market_order(params)
            self.invalidate_positions_cache()
            return self._wrap_open_resp(resp, coin, is_buy, sz_actual)
        except Exception as e:
            log.exception("Nado market_open(%s) failed: %s", coin, e)
            return _error_resp(str(e))

    def _size_increment_for(self, coin: str) -> float:
        """size_increment in base units (e.g. 0.1 for LINK, 0.001 for ETH)."""
        meta = self.asset(coin)
        if meta.size_increment_x18 > 0:
            return meta.size_increment_x18 / X18
        return 10 ** (-meta.sz_decimals) if meta.sz_decimals >= 0 else 1.0

    def market_close(self, coin: str) -> dict:
        try:
            pid = self._pid(coin)
            resp = self._sdk.market.close_position(self.subaccount_hex, pid)
            self.invalidate_positions_cache()
            return {"status": "ok", "response": {"type": "order", "data": {
                "statuses": [{"filled": {"avgPx": str(self.mark_price(coin)), "totalSz": "0"}}]
            }}, "_raw": str(resp)}
        except Exception as e:
            log.exception("Nado market_close(%s) failed: %s", coin, e)
            return _error_resp(str(e))

    def trigger_sl(self, coin: str, is_buy: bool, sz: float, trigger_px: float) -> dict:
        """Place a stop-loss trigger order (reduceOnly).
        is_buy=True  = buy-to-close (closes SHORT) → trigger when price RISES → oracle_price_above
        is_buy=False = sell-to-close (closes LONG)  → trigger when price FALLS → oracle_price_below
        """
        try:
            meta = self.asset(coin)
            pid = meta.product_id
            inc_x18 = meta.size_increment_x18 or int(round(self._size_increment_for(coin) * X18))
            target_x18 = int(round(sz * X18))
            amount_abs_x18 = (target_x18 // inc_x18) * inc_x18
            amount_signed_x18 = amount_abs_x18 if is_buy else -amount_abs_x18
            # Limit price worse-than-trigger so order fills as market when triggered
            slip = 0.005
            limit_px = trigger_px * (1 + slip) if is_buy else trigger_px * (1 - slip)
            # Bug fix 2026-05-05: float * 1e18 даёт float-error → x18 не кратен tick_x18
            # → exchange отвергает с "not divisible by price_increment_x18", позиция
            # остаётся без SL → forced-close с убытком. Округляем через integer math.
            tick = meta.tick_size or 0.0001
            tick_x18 = int(round(tick * X18))
            if tick_x18 <= 0:
                tick_x18 = 1
            limit_px_x18 = (int(round(limit_px * X18)) // tick_x18) * tick_x18
            trigger_px_x18 = (int(round(trigger_px * X18)) // tick_x18) * tick_x18
            trigger_type = "oracle_price_above" if is_buy else "oracle_price_below"
            log.info(
                "Nado trigger_sl: %s amount=%d trigger_x18=%d limit_x18=%d (tick_x18=%d) type=%s",
                coin, amount_signed_x18, trigger_px_x18, limit_px_x18, tick_x18, trigger_type,
            )
            resp = self._sdk.market.place_price_trigger_order(
                product_id=pid,
                price_x18=str(limit_px_x18),
                amount_x18=str(amount_signed_x18),
                trigger_price_x18=str(trigger_px_x18),
                trigger_type=trigger_type,
                subaccount_owner=self.main_wallet,
                subaccount_name=self.subaccount_name,
                reduce_only=True,
            )
            return self._wrap_sl_resp(resp, coin)
        except Exception as e:
            log.exception("Nado trigger_sl(%s) failed: %s", coin, e)
            return _error_resp(str(e))

    def cancel_order(self, coin: str, oid) -> dict:
        """Cancel REGULAR order (limit/market). Для trigger orders (SL/TP) — use cancel_sl_order!

        Bug fix 2026-05-06: Nado имеет 2 separate API endpoints:
        - cancel_orders (regular limit/market orders)
        - cancel_trigger_orders (SL / take_profit / stop trigger orders)
        Раньше cancel_order применяли к SL → API возвращало success но
        cancelled_orders=[] (silent fail). За 12 часов накопились 4-6 SL на
        позицию. Юзер обнаружил утром 06-05.
        """
        try:
            pid = self._pid(coin)
            params = CancelOrdersParams(
                sender=self.subaccount_hex,
                productIds=[pid],
                digests=[oid],
            )
            return {"raw": str(self._sdk.market.cancel_orders(params))}
        except Exception as e:
            if _is_order_not_found_2020(e):
                log.info("cancel(%s, %s): order already gone (Nado 2020)", coin, oid)
                return {"already_gone": True}
            log.warning("cancel(%s, %s) failed: %s", coin, oid, e)
            return {}

    def cancel_sl_order(self, coin: str, oid) -> dict:
        """Cancel a STOP-LOSS / TRIGGER order. Uses Nado's cancel_trigger_orders API.

        Bug fix 2026-05-06: SL orders на Nado — это TRIGGER orders. cancel_orders
        к ним не применяется (silent fail). Right method = cancel_trigger_orders.

        Log-noise fix 2026-05-06: error_code 2020 ("order not found") demoted
        to INFO. Stale SL oid'ы from previous session = expected on boot —
        bot replaces with fresh SL. Other errors (signature, network, 5xx)
        stay at WARNING.
        """
        try:
            pid = self._pid(coin)
            params = CancelTriggerOrdersParams(
                sender=self.subaccount_hex,
                productIds=[pid],
                digests=[oid],
            )
            return {"raw": str(self._sdk.market.cancel_trigger_orders(params))}
        except Exception as e:
            if _is_order_not_found_2020(e):
                log.info("cancel_sl(%s, %s): order already gone (Nado 2020)", coin, oid)
                return {"already_gone": True}
            log.warning("cancel_sl(%s, %s) failed: %s", coin, oid, e)
            return {}

    def list_open_sl_orders(self, coin: str) -> list[str]:
        """List все active stop/trigger orders на coin.

        Bug fix 2026-05-05 (вечер 4): метод отсутствовал на Nado → trader.py
        orphan SL cleanup всегда no-op на Nado. Теперь корректно использует
        existing fetch_open_orders shim.
        """
        try:
            orders = self.exchange.fetch_open_orders()
        except Exception:
            return []
        out = []
        for o in orders:
            if o.get("symbol") != coin and o.get("coin") != coin:
                continue
            t = str(o.get("type", "")).lower()
            info_t = str((o.get("info") or {}).get("orderType", "")).lower()
            info_status = str((o.get("info") or {}).get("status", "")).lower()
            if "trigger" in t or "stop" in t or "trigger" in info_t or "stop" in info_t or "waiting" in info_status:
                # Nado returns digest in info — это order ID
                oid = (o.get("info") or {}).get("digest") or o.get("id")
                if oid: out.append(oid)
        return out

    # ------------ User fills (for compute_realized_pnl) ------------

    def user_fills(self, ttl: float = 60.0) -> list[dict]:
        """Returns empty list — Nado's IndexerMatch doesn't expose product_id directly.
        compute_realized_pnl below queries matches per-product instead.
        Returned for HL-interface compat (trader.py passes this to compute_realized_pnl)."""
        return []

    def _fetch_matches_for_product(self, pid: int, limit: int = 100) -> list[dict]:
        """Fetch fills for a specific product, returns ccxt-like dicts.

        Bug fix 2026-05-06: m.timestamp всегда None в Nado SDK
        (`nado_protocol.indexer_client`). Используем `submission_idx` как
        proxy for time ordering — это monotonically increasing index.
        Fills возвращаются в submission_idx descending order (newest first).
        """
        try:
            data = self._sdk.context.indexer_client.get_matches(
                IndexerMatchesParams(
                    subaccounts=[self.subaccount_hex],
                    product_ids=[pid],
                    limit=limit,
                )
            )
            matches = data.matches if hasattr(data, "matches") else []
        except Exception as e:
            log.warning("get_matches(pid=%d) failed: %s", pid, e)
            return []
        sym = self._pid_to_symbol.get(pid, f"PID_{pid}")
        out = []
        for m in matches:
            try:
                amount_x18 = int(m.order.amount)
                base_filled = _from_x18(m.base_filled)  # actual size filled
                quote_filled = _from_x18(m.quote_filled)
                price = abs(quote_filled / base_filled) if abs(base_filled) > 1e-12 else 0.0
                fee = _from_x18(getattr(m, "fee", "0") or "0")
                # submission_idx as time proxy (timestamp is None in Nado SDK)
                try:
                    sub_idx = int(getattr(m, "submission_idx", 0) or 0)
                except (TypeError, ValueError):
                    sub_idx = 0
                out.append({
                    "symbol": sym,
                    "coin": sym,
                    "side": "buy" if amount_x18 > 0 else "sell",
                    "amount": abs(base_filled),
                    "price": price,
                    "timestamp": 0,  # real ts not available
                    "submission_idx": sub_idx,
                    "fee": {"cost": fee},
                    "info": {"product_id": pid},
                })
            except Exception as e:
                log.warning("match parse (pid=%d): %s", pid, e)
        return out

    def compute_realized_pnl(
        self, fills, coin: str, direction: str, size: float, trade_open_iso=None,
    ):
        """Compute realized PnL from open+close fill pairs. Nado's IndexerMatch
        doesn't have product_id, so we ignore the `fills` arg and re-fetch per
        product. The fills arg stays for HL-interface compat.

        Bug fix 2026-05-06: m.timestamp всегда None в Nado SDK → старая
        фильтрация по `t_ms >= open_ms` всегда была False → closes=[]/opens=[]
        → возвращали None для ВСЕХ closed_vstop trades на Nado.

        Fix: fills возвращаются в submission_idx descending order (newest first).
        Берём first batch closing-side fills (cumulative ≥ size) — это close
        текущей trade. Затем next batch opening-side fills (cumulative ≥ size)
        — это open текущей trade. Submission_idx гарантирует ordering.

        Edge case: если на coin'е была другая trade закрытая ранее (и её
        close+open находятся в fills tail), они не запутают первую пару — мы
        останавливаемся когда заполнили size в каждом batch.
        """
        try:
            pid = self._pid(coin)
        except KeyError:
            return None, None
        fills = self._fetch_matches_for_product(pid, limit=200)
        if not fills:
            return None, None

        # Sort by submission_idx descending (most recent first) for safety.
        # API obviously returns this way but be defensive.
        fills_sorted = sorted(
            (f for f in fills if (f.get("symbol") == coin or f.get("coin") == coin)),
            key=lambda f: int(f.get("submission_idx", 0) or 0),
            reverse=True,
        )
        if not fills_sorted:
            return None, None

        opening_side = "buy" if direction == "long" else "sell"
        closing_side = "sell" if direction == "long" else "buy"

        # Phase 1: collect close fills until cumulative amount ≥ size × 0.95
        target = max(size * 0.95, 0.0001)  # min target to handle float fuzz
        closes: list[dict] = []
        cum_close = 0.0
        i = 0
        n = len(fills_sorted)
        while i < n and cum_close < target:
            f = fills_sorted[i]
            i += 1
            if f.get("side") != closing_side:
                continue
            amt = float(f.get("amount", 0) or 0)
            if amt <= 0:
                continue
            closes.append(f)
            cum_close += amt
        if not closes:
            return None, None

        # Phase 2: continue from where Phase 1 left off, collect opens until target
        # (skipping any further close fills which belong to OLDER trades)
        opens: list[dict] = []
        cum_open = 0.0
        while i < n and cum_open < target:
            f = fills_sorted[i]
            i += 1
            if f.get("side") != opening_side:
                continue
            amt = float(f.get("amount", 0) or 0)
            if amt <= 0:
                continue
            opens.append(f)
            cum_open += amt

        def _wavg(lst):
            tot = sum(float(x.get("amount", 0) or 0) for x in lst)
            if tot <= 0:
                return None, 0.0
            wsum = sum(
                float(x.get("price", 0) or 0) * float(x.get("amount", 0) or 0)
                for x in lst
            )
            return wsum / tot, tot

        avg_close, close_sz = _wavg(closes)
        avg_open, open_sz = _wavg(opens) if opens else (None, 0.0)

        if avg_open is None or open_sz <= 0:
            # Fallback: opens not found (older trade?) — return only avg_close
            return None, avg_close

        matched = min(close_sz, open_sz)
        if direction == "long":
            gross = (avg_close - avg_open) * matched
        else:
            gross = (avg_open - avg_close) * matched
        notional = (avg_open + avg_close) * matched / 2
        fees = notional * 0.001  # ~5bps taker × 2 sides ≈ 0.10%
        return gross - fees, avg_close

    # ------------ Response wrappers (HL-shape for trader.py) ------------

    def _wrap_open_resp(self, sdk_resp, coin: str, is_buy: bool, sz: float) -> dict:
        """Translate Nado SDK response to HL-shape so trader's parser doesn't change."""
        try:
            # Successful place_market_order returns a response with status & req hash.
            # Fill price comes back via subaccount state on next cycle, not in resp.
            # For trader.py we synthesise filled.avgPx from current mark.
            mark = self.mark_price(coin)
            return {
                "status": "ok",
                "response": {"type": "order", "data": {
                    "statuses": [{"filled": {
                        "avgPx": str(mark) if mark else str(0),
                        "totalSz": str(sz),
                    }}]
                }},
                "_raw": str(sdk_resp)[:300],
            }
        except Exception as e:
            return _error_resp(f"wrap_open: {e}")

    def _wrap_sl_resp(self, sdk_resp, coin: str) -> dict:
        """Translate trigger order response. Use the order digest as oid."""
        try:
            digest = None
            # Try common shapes for the digest field
            for attr in ("digest", "tx_digest"):
                if hasattr(sdk_resp, attr):
                    digest = getattr(sdk_resp, attr)
                    break
            if digest is None:
                # parse JSON response
                d = sdk_resp.dict() if hasattr(sdk_resp, "dict") else {}
                digest = d.get("data", {}).get("digest") or d.get("digest")
            if not digest:
                digest = "nado_sl_" + str(int(time.time() * 1000))
            return {"status": "ok", "response": {"type": "order", "data": {
                "statuses": [{"resting": {"oid": digest}}]
            }}, "_raw": str(sdk_resp)[:300]}
        except Exception as e:
            return _error_resp(f"wrap_sl: {e}")


def _error_resp(msg: str) -> dict:
    """HL-shape error response."""
    return {"status": "ok", "response": {"type": "order", "data": {
        "statuses": [{"error": msg[:200]}]
    }}}


# Public alias for factory
NadoClient = NadoClient_
