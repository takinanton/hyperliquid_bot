"""Подбор пар по реальной ликвидности (OI + recent volume).

Старый подход (top по dayNtlVlm) обманчив:
- Pair может иметь $50M за 24h volume но 99% объёма было час назад,
  сейчас pump прошёл, ликвидность для свежей сделки = низкая.
- Open Interest (OI) показывает РЕАЛЬНУЮ глубину рынка — сколько
  суммарно открытых позиций. Высокий OI = много участников = легко
  войти/выйти без slippage.

Новый подход:
1. Стартовый фильтр пары (на старте бота) — по OI и 24h volume.
2. Pre-trade check (в момент signal) — текущий 1h volume должен покрывать
   нашу позицию × N (по умолчанию 20x).
"""
from __future__ import annotations

import logging
import requests

log = logging.getLogger(__name__)


def fetch_liquidity_metrics(hip3_whitelist: set[str] | None = None) -> list[dict]:
    """Возвращает [{name, day_vol_usd, oi_usd, mark_px, max_leverage}, ...]
    для main perps + whitelisted HIP-3 dexes."""
    if hip3_whitelist is None:
        hip3_whitelist = {"xyz"}

    url = "https://api.hyperliquid.xyz/info"
    out: list[dict] = []

    # Main universe
    try:
        r = requests.post(url, json={"type": "metaAndAssetCtxs"}, timeout=15)
        ctxs = r.json()
        if isinstance(ctxs, list) and len(ctxs) >= 2:
            universe = ctxs[0].get("universe", [])
            asset_ctxs = ctxs[1]
            for i, asset in enumerate(universe):
                if i >= len(asset_ctxs):
                    continue
                ctx = asset_ctxs[i]
                try:
                    mark_px = float(ctx.get("markPx", 0) or 0)
                    oi_units = float(ctx.get("openInterest", 0) or 0)
                    day_vol = float(ctx.get("dayNtlVlm", 0) or 0)
                except (TypeError, ValueError):
                    continue
                out.append({
                    "name": asset["name"],
                    "day_vol_usd": day_vol,
                    "oi_usd": oi_units * mark_px,
                    "mark_px": mark_px,
                    "max_leverage": int(asset.get("maxLeverage", 1)),
                })
    except Exception as e:
        log.warning("main metaAndAssetCtxs failed: %s", e)

    # HIP-3 whitelisted dexes
    try:
        dexs = requests.post(url, json={"type": "perpDexs"}, timeout=15).json()
        if isinstance(dexs, list):
            for d in dexs:
                if d is None or not isinstance(d, dict):
                    continue
                dex_name = d.get("name")
                if not dex_name or dex_name not in hip3_whitelist:
                    continue
                try:
                    cts = requests.post(
                        url,
                        json={"type": "metaAndAssetCtxs", "dex": dex_name},
                        timeout=15,
                    ).json()
                    if isinstance(cts, list) and len(cts) >= 2:
                        for i, asset in enumerate(cts[0].get("universe", [])):
                            if i >= len(cts[1]):
                                continue
                            ctx = cts[1][i]
                            try:
                                mark_px = float(ctx.get("markPx", 0) or 0)
                                oi_units = float(ctx.get("openInterest", 0) or 0)
                                day_vol = float(ctx.get("dayNtlVlm", 0) or 0)
                            except (TypeError, ValueError):
                                continue
                            out.append({
                                "name": asset["name"],
                                "day_vol_usd": day_vol,
                                "oi_usd": oi_units * mark_px,
                                "mark_px": mark_px,
                                "max_leverage": int(asset.get("maxLeverage", 1)),
                            })
                except Exception:
                    continue
    except Exception as e:
        log.warning("HIP-3 dex fetch failed: %s", e)

    return out


def select_all_pairs(hip3_whitelist: set[str] | None = None) -> list[str]:
    """Возвращает ВСЕ доступные пары (main + whitelisted HIP-3) без фильтра.
    Используется когда COIN_FILTER=none — фильтр работает только в момент
    pre-trade check (более точно, реагирует на live ликвидность)."""
    metrics = fetch_liquidity_metrics(hip3_whitelist=hip3_whitelist)
    if not metrics:
        return []
    # Сортируем по OI desc для предсказуемого порядка обхода
    metrics.sort(key=lambda m: -m["oi_usd"])
    return [m["name"] for m in metrics]


def select_liquid_coins(
    n: int = 100,
    min_oi_usd: float = 200_000,
    min_vol_24h_usd: float = 500_000,
    hip3_whitelist: set[str] | None = None,
    bypass_filter_prefixes: tuple[str, ...] = ("xyz:",),
) -> list[str]:
    """Выбирает top-N пар по OI с минимумами на OI и volume.

    Сортировка по oi_usd descending. Это лучше чем по volume, т.к. OI
    показывает текущую глубину, а volume может быть из прошлого pump.

    bypass_filter_prefixes: пары с этими префиксами всегда включаются
    минуя фильтр (например xyz: — HIP-3 stocks/commodities, тонкие на
    weekend, но в будни торгуются нормально).
    """
    metrics = fetch_liquidity_metrics(hip3_whitelist=hip3_whitelist)
    if not metrics:
        log.warning("select_liquid_coins: empty metrics")
        return []

    # Bypass для определённых префиксов (например xyz: HIP-3)
    bypassed = [
        m for m in metrics
        if any(m["name"].startswith(p) for p in bypass_filter_prefixes)
    ]
    bypassed_names = {m["name"] for m in bypassed}

    # Применяем минимумы к остальным
    filtered = [
        m for m in metrics
        if m["name"] not in bypassed_names
        and m["oi_usd"] >= min_oi_usd
        and m["day_vol_usd"] >= min_vol_24h_usd
    ]

    log.info(
        "Liquidity filter: %d total → %d after OI≥$%.0fk/vol≥$%.0fk + %d bypassed (%s)",
        len(metrics), len(filtered), min_oi_usd / 1000, min_vol_24h_usd / 1000,
        len(bypassed), ",".join(bypass_filter_prefixes),
    )

    # Сортируем по OI: bypassed (xyz) и main отдельно
    filtered.sort(key=lambda m: -m["oi_usd"])
    bypassed.sort(key=lambda m: -m["oi_usd"])

    # n применяется ТОЛЬКО к main pairs (bypassed = bonus all-in).
    # Это значит при n=100 + 69 xyz = всего 169 пар в pool. Юзер так хочет.
    selected = bypassed + filtered[:n]

    if log.isEnabledFor(logging.DEBUG):
        for m in selected[:10]:
            log.debug(
                "  %s: OI $%.1fM, vol24h $%.1fM, mark $%g",
                m["name"], m["oi_usd"] / 1e6, m["day_vol_usd"] / 1e6, m["mark_px"],
            )

    return [m["name"] for m in selected]


def has_sufficient_1h_liquidity(
    last_1h_volume_usd: float,
    position_notional: float,
    min_ratio: float = 20.0,
) -> bool:
    """Pre-trade check: за последний 1h объём пары должен быть ≥ min_ratio
    × notional нашей позиции.

    min_ratio=20 → если открываемся на $1000 notional, последний 1h должен
    был оторговать ≥ $20k. Это даёт ~5% slippage cushion в worst case.

    Сюда же можно добавить depth-of-book check (info.l2_book) для precision —
    но 1h volume как proxy достаточно для большинства случаев.
    """
    if position_notional <= 0:
        return True
    return last_1h_volume_usd >= position_notional * min_ratio
