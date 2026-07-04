"""Размер позиции, фильтры (R/R, EMA200 старший ТФ, funding), circuit breaker."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import pandas as pd

from bot.config import EMA_LENGTH, Settings
from bot.detector import PatternSignal
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from bot.exchange import AssetMeta

log = logging.getLogger(__name__)


# ---------- EMA200 фильтр старшего ТФ ----------
def higher_tf_trend(df_higher: pd.DataFrame) -> str:
    """'long' если Close > EMA(200) на старшем ТФ, иначе 'short'. 'unknown' если данных мало."""
    if df_higher is None or df_higher.empty or len(df_higher) < EMA_LENGTH:
        return "unknown"
    ema = df_higher["Close"].ewm(span=EMA_LENGTH, adjust=False).mean()
    last_close = float(df_higher["Close"].iloc[-1])
    last_ema = float(ema.iloc[-1])
    return "long" if last_close > last_ema else "short"


# ---------- Funding фильтр ----------
def is_funding_blocked(funding_hourly: float, direction: str, threshold: float) -> bool:
    """Блокируем вход в направлении funding, если |funding * 8| > threshold."""
    if abs(funding_hourly * 8) <= threshold:
        return False
    if funding_hourly > 0 and direction == "long":
        return True
    if funding_hourly < 0 and direction == "short":
        return True
    return False


# ---------- R/R и контртренд ----------
def is_countertrend(direction: str, higher_trend: str) -> bool:
    return higher_trend != "unknown" and direction != higher_trend


def required_rr(direction: str, higher_trend: str, settings: Settings) -> float:
    return settings.min_rr_countertrend if is_countertrend(direction, higher_trend) else settings.min_rr


# ---------- BTC market-regime filter for shorts ----------
# Backtest evidence (KF 4y, 3095 trades, NET fees+slip):
#   bull-market flag_short: avgR -0.195R (n=141)
#   bull-market 123_short:  avgR -0.046R (n=197)
#   bull-market triangle_short: avgR +0.414R (n=111)  ← keep
#   Selective drop of flag/123 short in BTC bull (4h EMA200) on crypto only:
#     +36R over 4y, +19% avgR per slot, -11% trade slots used.
#   HIP-3 assets (xyz:*) — stocks/forex/commodities/indices — independent of BTC,
#   exempt from this filter (юзер 2026-05-06).

_BTC_BULL_BLOCKED_PATTERNS: frozenset[str] = frozenset({"flag_short", "123_short"})


def is_btc_regime_blocked(pattern: str, coin: str, btc_4h_trend: str) -> tuple[bool, str | None]:
    """Block short patterns that have no edge in BTC bull market for crypto coins.

    Returns (blocked, reason). HIP-3 (xyz:*) is exempt.
    """
    if pattern not in _BTC_BULL_BLOCKED_PATTERNS:
        return False, None
    if coin.startswith("xyz:"):
        return False, None  # HIP-3 stocks/forex/commodities — independent of BTC cycle
    if btc_4h_trend != "long":
        return False, None  # BTC bear or unknown — short is fine
    return True, f"BTC bull regime + crypto {pattern} → blocked (avgR≈0 в backtest 4y)"


# ---------- Размер позиции ----------
@dataclass
class SizeResult:
    size: float
    risk_dollars: float
    leverage: int
    notional: float


def compute_size(
    signal: PatternSignal,
    asset: AssetMeta,
    account_value: float,
    settings: Settings,
    risk_multiplier: float = 1.0,
    liquidity_cap_notional: float | None = None,
) -> SizeResult | None:
    """Args:
        risk_multiplier: множитель к base risk_per_trade.
            1.0 = standard. >1 для boosted setups (liquidity tier, pattern boost, etc).
        liquidity_cap_notional: max position $ value based on pair's liquidity.
            None = no cap. Иначе size scaled down so notional <= cap.
            Adaptive sizing: liquid pairs (BTC) get full risk-based size,
            thin pairs get smaller size (proportional to volume).
    """
    risk_per_unit = abs(signal.entry - signal.stop_loss)
    if risk_per_unit <= 0 or signal.entry <= 0:
        return None

    risk_dollars = account_value * settings.risk_per_trade * risk_multiplier
    leverage = asset.max_leverage if settings.leverage_mode == "max" else 1

    size_by_risk = risk_dollars / risk_per_unit
    size_by_leverage = (account_value * leverage) / signal.entry

    size = min(size_by_risk, size_by_leverage)

    # Liquidity cap: shrink to fit pair's available volume
    if liquidity_cap_notional is not None and liquidity_cap_notional > 0:
        size_by_liquidity = liquidity_cap_notional / signal.entry
        size = min(size, size_by_liquidity)

    size = _round_size(size, asset.sz_decimals)

    if size <= 0:
        return None

    notional = size * signal.entry
    # Recompute actual risk_dollars (may be smaller if liquidity-capped)
    actual_risk = size * risk_per_unit
    return SizeResult(size=size, risk_dollars=actual_risk, leverage=leverage, notional=notional)


def _round_size(size: float, sz_decimals: int) -> float:
    if size <= 0:
        return 0.0
    factor = 10**sz_decimals
    return math.floor(size * factor) / factor


# ---------- Liquidation vs Stop-Loss check ----------
def check_liq_vs_sl(
    direction: str,
    sl_px: float,
    liq_px: float | None,
    margin_mode: str,
    entry_px: float,
    buffer_pct: float = 0.3,
) -> tuple[str, str]:
    """Проверяет что SL триггерится раньше liquidation, с запасом.

    Корректно работает и для initial SL (под entry для long), и для trailed
    SL (выше entry для long): сравниваем дистанцию SL→liq (в направлении
    против позы) с дистанцией entry→SL (величина риска по сделке).

    Правило: |SL - liq| ≥ buffer_pct × |entry - SL|.
    buffer_pct=0.3 даёт 30% запас — partial-liq на тонком стакане может
    выстрелить чуть раньше номинального liq_px, нужен margin сверху.

    Returns (severity, message):
        'critical' = isolated и SL уже за liq (SL не сработает раньше liq).
        'warn'     = SL за liq но buffer < threshold (для cross), или
                     любая проблема для isolated.
        'ok'       = SL раньше liq с буфером ≥ threshold.
    """
    if liq_px is None or liq_px <= 0:
        return "ok", "liq_px=None (HL отдаёт только когда близко)"
    sl_dist = abs(entry_px - sl_px)
    if sl_dist <= 0:
        return "ok", "SL дистанция 0"
    # Direction-aware: для long SL должен быть ВЫШЕ liq (SL триггерится при
    # падении первым); для short — НИЖЕ liq.
    if direction == "long":
        sl_minus_liq = sl_px - liq_px  # >0 когда SL safely над liq
    else:
        sl_minus_liq = liq_px - sl_px  # >0 когда SL safely под liq
    is_isolated = margin_mode.lower() == "isolated"
    if sl_minus_liq <= 0:
        sev = "critical" if is_isolated else "warn"
        return sev, (
            f"{margin_mode} {direction}: SL={sl_px:.6f} НЕ перед liq={liq_px:.6f} "
            f"(SL не сработает раньше liq)"
        )
    buffer_ratio = sl_minus_liq / sl_dist
    if buffer_ratio < buffer_pct:
        sev = "critical" if is_isolated else "warn"
        return sev, (
            f"{margin_mode} {direction}: SL→liq buffer {buffer_ratio*100:.0f}% < "
            f"{buffer_pct*100:.0f}% от SL_dist (SL={sl_px:.6f} liq={liq_px:.6f} "
            f"entry={entry_px:.6f})"
        )
    return "ok", f"SL→liq buffer {buffer_ratio*100:.0f}% ≥ {buffer_pct*100:.0f}%"


