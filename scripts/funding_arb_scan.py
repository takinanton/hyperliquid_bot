#!/usr/bin/env python3
"""Cross-venue funding-rate arbitrage scanner (BTC/ETH/SOL и любые другие монеты).

Задача: найти монету/пару бирж, где funding-спред большой И стабильный —
т.е. carry, который платят каждые 1/4/8 часов независимо от направления цены.

Режимы:

  venues    — какие биржи вообще умеют perp+funding (авто-дискавери из ccxt).
  snapshot  — текущий funding на всех venue → лучшая пара (short дорогой /
              long дешёвый) по APR-спреду на каждую монету.
  history   — funding history за N дней → mean/median APR, % времени спред > 0,
              худшее 7-дневное окно, net APR после комиссий. Единственный режим,
              который отвечает на вопрос «стабильный ли он».
  one-leg   — одноногий carry по всем монетам одной биржи: сколько funding
              реально собрал бы шорт (или лонг) за N дней и с какой consistency.
  hl-multi  — быстрый кросс-venue снимок по всем монетам HL одним запросом
              (HL / Binance / Bybit через HL `predictedFundings`).

Venue registry строится автоматически из ccxt (все биржи с swap + funding) —
ничего не хардкодится списком «топ-N», сканируются все, кто ответил.

Примеры:
    python scripts/funding_arb_scan.py venues
    python scripts/funding_arb_scan.py snapshot --coins BTC,ETH,SOL --min-apr 10
    python scripts/funding_arb_scan.py history --days 90 --coins BTC,ETH,SOL
    python scripts/funding_arb_scan.py history --days 30 --venues hyperliquid,pacifica,binance
    python scripts/funding_arb_scan.py one-leg --venue hyperliquid --coins all --days 90
    python scripts/funding_arb_scan.py hl-multi --coins all --top-pairs 25

ВАЖНО: скрипт сетевой. На VPS (hl-bot / kraken-bot) сеть открыта — там полный
скан 50 бирж. В песочнице Claude egress-policy режет всё кроме hyperliquid,
скан честно отрапортует BLOCKED по каждому venue.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import ccxt
except ImportError:  # pragma: no cover
    sys.exit("ccxt не установлен: pip install ccxt")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "data" / "funding_arb"

HOURS_PER_YEAR = 24 * 365  # 8760

# Дубликаты/регион-клоны — те же данные, только шум в таблице.
DENY_VENUES = {
    "binancecoinm",   # inverse, funding тот же что binanceusdm
    "binanceusdm",    # покрывается 'binance'
    "okxus",          # клон okx
    "kucoineu",       # клон kucoin
    "kucoin",         # спот-роутер, фьючи в kucoinfutures
    "fmfwio",         # клон hitbtc
    "coinbaseexchange",
}

# Дефолтный funding-интервал, если venue не отдаёт его в ответе.
DEFAULT_INTERVAL_H = {
    "hyperliquid": 1,
    "pacifica": 1,
    "lighter": 1,
    "nado": 1,
    "extended": 1,
    "paradex": 8,
    "aster": 1,
    "backpack": 1,
    "hibachi": 1,
    "dydx": 1,
    "apex": 8,
    "bitmex": 8,
    "deribit": 8,
}

# Round-trip taker-комиссия (открыть+закрыть, одна нога), доля. Используется
# только если ccxt не отдал market['taker']. Значения из docs/rules.md.
DEFAULT_FEE_RT = {
    "hyperliquid": 0.0009,
    "kraken": 0.0010,
    "krakenfutures": 0.0010,
    "nado": 0.0010,
    "pacifica": 0.0010,
    "extended": 0.0010,
}
FALLBACK_FEE_RT = 0.0010  # 0.05% taker × 2

QUOTE_PREFERENCE = ("USDT", "USDC", "USD")


# ─────────────────────────────────────────────────────────────────────────────
# Модель данных
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Quote:
    """Один funding-замер на venue."""
    venue: str
    coin: str
    symbol: str
    rate: float          # ставка за интервал (доля, не %)
    interval_h: float
    ts_ms: int | None = None
    mark: float | None = None

    @property
    def apr(self) -> float:
        """Годовая ставка в % (без компаундинга — так считают все биржи)."""
        return self.rate * (HOURS_PER_YEAR / self.interval_h) * 100.0


@dataclass
class VenueResult:
    venue: str
    ok: bool
    error: str = ""
    quotes: dict[str, Quote] = field(default_factory=dict)          # coin -> Quote
    series: dict[str, list[Quote]] = field(default_factory=dict)    # coin -> history
    fee_rt: dict[str, float] = field(default_factory=dict)          # coin -> round-trip fee


# ─────────────────────────────────────────────────────────────────────────────
# Venue discovery
# ─────────────────────────────────────────────────────────────────────────────

def discover_venues(include_deny: bool = False) -> list[str]:
    """Все ccxt-биржи с perp-рынками и funding (snapshot или history)."""
    out: list[str] = []
    for eid in ccxt.exchanges:
        if not include_deny and eid in DENY_VENUES:
            continue
        try:
            ex = getattr(ccxt, eid)()
        except Exception:
            continue
        has = ex.has or {}
        if not has.get("swap"):
            continue
        if has.get("fetchFundingRate") or has.get("fetchFundingRates") or has.get("fetchFundingRateHistory"):
            out.append(eid)
    return sorted(out)


def make_client(eid: str, timeout_ms: int) -> Any:
    ex = getattr(ccxt, eid)({"enableRateLimit": True, "timeout": timeout_ms})
    # ccxt ставит session.trust_env=False → requests игнорит HTTPS_PROXY и
    # REQUESTS_CA_BUNDLE. На VPS это no-op (proxy не задан), в песочнице —
    # единственный способ вообще выйти в сеть.
    session = getattr(ex, "session", None)
    if session is not None:
        session.trust_env = True
    return ex


def perp_symbol(ex: Any, coin: str) -> str | None:
    """Находит linear-perp символ для монеты. Возвращает None если нет рынка."""
    coin = coin.upper()
    for quote in QUOTE_PREFERENCE:
        cand = f"{coin}/{quote}:{quote}"
        if cand in ex.markets:
            return cand
    best: tuple[int, str] | None = None
    for sym, m in ex.markets.items():
        if not m.get("swap") or not m.get("active", True):
            continue
        if (m.get("base") or "").upper() != coin:
            continue
        if m.get("inverse"):
            continue
        settle = (m.get("settle") or "").upper()
        rank = QUOTE_PREFERENCE.index(settle) if settle in QUOTE_PREFERENCE else len(QUOTE_PREFERENCE)
        if best is None or rank < best[0]:
            best = (rank, sym)
    return best[1] if best else None


def fee_round_trip(ex: Any, eid: str, symbol: str) -> float:
    """Round-trip taker-комиссия одной ноги (открыть + закрыть)."""
    try:
        taker = ex.markets[symbol].get("taker")
        if taker is not None and float(taker) > 0:
            return float(taker) * 2.0
    except Exception:
        pass
    return DEFAULT_FEE_RT.get(eid, FALLBACK_FEE_RT)


# ─────────────────────────────────────────────────────────────────────────────
# Нормализация интервала
# ─────────────────────────────────────────────────────────────────────────────

def _interval_from_payload(eid: str, payload: dict[str, Any]) -> float:
    """Достаёт длину funding-интервала в часах из ccxt-ответа."""
    raw = payload.get("interval")
    if isinstance(raw, str) and raw.endswith("h"):
        try:
            return float(raw[:-1])
        except ValueError:
            pass
    info = payload.get("info") or {}
    for key in ("fundingIntervalHours", "funding_interval_hours", "fundingInterval", "interval_hours"):
        val = info.get(key)
        if val is not None:
            try:
                v = float(val)
                # некоторые биржи отдают миллисекунды/минуты
                if v > 1000:
                    return v / 3_600_000.0
                if v > 24:
                    return v / 60.0
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
    prev_t, next_t = payload.get("fundingTimestamp"), payload.get("nextFundingTimestamp")
    if prev_t and next_t and next_t > prev_t:
        gap = (next_t - prev_t) / 3_600_000.0
        if 0.5 <= gap <= 24:
            return round(gap, 3)
    return float(DEFAULT_INTERVAL_H.get(eid, 8))


def _interval_from_series(eid: str, stamps: list[int]) -> float:
    """Медианный шаг между funding-событиями (надёжнее, чем поле в ответе)."""
    gaps = [
        (b - a) / 3_600_000.0
        for a, b in zip(stamps, stamps[1:])
        if b > a and (b - a) / 3_600_000.0 <= 24
    ]
    if not gaps:
        return float(DEFAULT_INTERVAL_H.get(eid, 8))
    return round(statistics.median(gaps), 3)


# ─────────────────────────────────────────────────────────────────────────────
# Fetchers
# ─────────────────────────────────────────────────────────────────────────────

def fetch_snapshot(eid: str, coins: list[str], timeout_ms: int) -> VenueResult:
    res = VenueResult(venue=eid, ok=False)
    try:
        ex = make_client(eid, timeout_ms)
        ex.load_markets()
    except Exception as exc:
        res.error = f"markets: {_short(exc)}"
        return res

    symbols: dict[str, str] = {}
    for coin in coins:
        sym = perp_symbol(ex, coin)
        if sym:
            symbols[coin] = sym
            res.fee_rt[coin] = fee_round_trip(ex, eid, sym)
    if not symbols:
        res.error = "нет perp-рынков по запрошенным монетам"
        return res

    payloads: dict[str, dict[str, Any]] = {}
    has = ex.has or {}
    if has.get("fetchFundingRates"):
        try:
            batch = ex.fetch_funding_rates(list(symbols.values()))
            for coin, sym in symbols.items():
                if sym in batch:
                    payloads[coin] = batch[sym]
        except Exception as exc:
            res.error = f"batch: {_short(exc)}"
    if has.get("fetchFundingRate"):
        for coin, sym in symbols.items():
            if coin in payloads:
                continue
            try:
                payloads[coin] = ex.fetch_funding_rate(sym)
            except Exception as exc:
                res.error = res.error or f"single: {_short(exc)}"
    if not payloads and has.get("fetchFundingRateHistory"):
        # venue без snapshot-эндпоинта (extended/paradex/dydx/apex) — берём
        # последнюю запись истории.
        for coin, sym in symbols.items():
            try:
                hist = ex.fetch_funding_rate_history(sym, limit=10)
                if hist:
                    last = hist[-1]
                    payloads[coin] = {
                        "fundingRate": last.get("fundingRate"),
                        "fundingTimestamp": last.get("timestamp"),
                        "info": last.get("info") or {},
                        "interval": None,
                    }
                    if len(hist) >= 3:
                        stamps = [h["timestamp"] for h in hist if h.get("timestamp")]
                        payloads[coin]["_interval_h"] = _interval_from_series(eid, stamps)
            except Exception as exc:
                res.error = res.error or f"hist: {_short(exc)}"

    for coin, payload in payloads.items():
        rate = payload.get("fundingRate")
        if rate is None:
            continue
        interval = payload.get("_interval_h") or _interval_from_payload(eid, payload)
        res.quotes[coin] = Quote(
            venue=eid,
            coin=coin,
            symbol=symbols[coin],
            rate=float(rate),
            interval_h=float(interval),
            ts_ms=payload.get("fundingTimestamp") or payload.get("timestamp"),
            mark=_maybe_float(payload.get("markPrice")),
        )
    res.ok = bool(res.quotes)
    if res.ok:
        res.error = ""
    elif not res.error:
        res.error = "funding не отдан"
    return res


def fetch_coin_history(ex: Any, eid: str, coin: str, days: int) -> list[Quote]:
    """Funding history одной монеты на одном venue, с пагинацией назад."""
    sym = perp_symbol(ex, coin)
    if not sym:
        return []
    since_ms = int((time.time() - days * 86400) * 1000)
    rows: list[dict[str, Any]] = []
    cursor = since_ms
    seen: set[int] = set()
    for _ in range(400):
        chunk = ex.fetch_funding_rate_history(sym, since=cursor, limit=1000)
        if not chunk:
            break
        fresh = [c for c in chunk if c.get("timestamp") and c["timestamp"] not in seen]
        if not fresh:
            break
        for c in fresh:
            seen.add(c["timestamp"])
        rows.extend(fresh)
        last_ts = max(c["timestamp"] for c in fresh)
        if last_ts <= cursor:
            break
        cursor = last_ts + 1
        if cursor >= int(time.time() * 1000):
            break
    if not rows:
        return []
    rows.sort(key=lambda r: r["timestamp"])
    interval = _interval_from_series(eid, [r["timestamp"] for r in rows])
    return [
        Quote(venue=eid, coin=coin, symbol=sym, rate=float(r["fundingRate"]),
              interval_h=interval, ts_ms=r["timestamp"])
        for r in rows if r.get("fundingRate") is not None
    ]


def fetch_history(eid: str, coins: list[str], days: int, timeout_ms: int) -> VenueResult:
    res = VenueResult(venue=eid, ok=False)
    try:
        ex = make_client(eid, timeout_ms)
        ex.load_markets()
    except Exception as exc:
        res.error = f"markets: {_short(exc)}"
        return res
    if not (ex.has or {}).get("fetchFundingRateHistory"):
        res.error = "нет fetchFundingRateHistory"
        return res

    for coin in coins:
        sym = perp_symbol(ex, coin)
        if not sym:
            continue
        res.fee_rt[coin] = fee_round_trip(ex, eid, sym)
        try:
            series = fetch_coin_history(ex, eid, coin, days)
        except Exception as exc:
            res.error = res.error or f"{coin}: {_short(exc)}"
            continue
        if series:
            res.series[coin] = series
            res.quotes[coin] = series[-1]
    res.ok = bool(res.series)
    if res.ok:
        res.error = ""
    elif not res.error:
        res.error = "история пуста"
    return res


# ─────────────────────────────────────────────────────────────────────────────
# Аналитика
# ─────────────────────────────────────────────────────────────────────────────

def hourly_grid(series: list[Quote], start_ms: int, end_ms: int) -> dict[int, float]:
    """APR-серия, разложенная по часам. Ключ — час от epoch.

    Funding-событие в момент t со ставкой r и интервалом h означает: этот APR
    действовал на протяжении h часов ДО t. Раскладываем назад, чтобы 1h-venue
    (HL) и 8h-venue (Binance) сравнивались на общей сетке.
    """
    out: dict[int, float] = {}
    for q in series:
        if q.ts_ms is None:
            continue
        end_h = int(q.ts_ms // 3_600_000)
        span = max(1, int(round(q.interval_h)))
        for k in range(span):
            hour = end_h - k
            if start_ms // 3_600_000 <= hour <= end_ms // 3_600_000:
                out[hour] = q.apr
    return out


def leg_stats(series: list[Quote]) -> dict[str, float]:
    aprs = [q.apr for q in series]
    if not aprs:
        return {}
    return {
        "n": len(aprs),
        "mean_apr": statistics.fmean(aprs),
        "median_apr": statistics.median(aprs),
        "stdev_apr": statistics.pstdev(aprs) if len(aprs) > 1 else 0.0,
        "pct_positive": 100.0 * sum(1 for a in aprs if a > 0) / len(aprs),
        "min_apr": min(aprs),
        "max_apr": max(aprs),
    }


def spread_stats(short_grid: dict[int, float], long_grid: dict[int, float]) -> dict[str, float]:
    """Стата по спреду. short_grid — где мы шортим (платят нам при funding>0)."""
    common = sorted(set(short_grid) & set(long_grid))
    if len(common) < 24:
        return {}
    spread = [short_grid[h] - long_grid[h] for h in common]
    worst_7d = _worst_window_mean(spread, window_h=24 * 7)
    return {
        "hours": len(common),
        "mean_apr": statistics.fmean(spread),
        "median_apr": statistics.median(spread),
        "pct_positive": 100.0 * sum(1 for s in spread if s > 0) / len(spread),
        "worst_7d_apr": worst_7d,
        "min_apr": min(spread),
        "max_apr": max(spread),
    }


def _worst_window_mean(values: list[float], window_h: int) -> float:
    """Худшее скользящее окно — проверка «а не держится ли спред на одном всплеске»."""
    if len(values) < window_h:
        return statistics.fmean(values)
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    worst = math.inf
    for i in range(0, len(values) - window_h + 1):
        worst = min(worst, (prefix[i + window_h] - prefix[i]) / window_h)
    return worst


def net_apr(gross_apr: float, fee_rt_total: float, hold_days: float) -> float:
    """Спред минус комиссии, размазанные по сроку удержания."""
    if hold_days <= 0:
        return gross_apr
    return gross_apr - (fee_rt_total * 100.0) * (365.0 / hold_days)


# ─────────────────────────────────────────────────────────────────────────────
# Вывод
# ─────────────────────────────────────────────────────────────────────────────

def print_connectivity(results: list[VenueResult]) -> None:
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    print(f"\n=== Venue connectivity: {len(ok)}/{len(results)} OK ===")
    print("  OK:  " + ", ".join(r.venue for r in ok) if ok else "  OK:  (ни один venue не ответил)")
    if bad:
        print("  FAIL:")
        for r in sorted(bad, key=lambda x: x.venue):
            print(f"    {r.venue:<18} {r.error[:90]}")


def print_snapshot(coin: str, quotes: list[Quote], min_apr: float, hold_days: float,
                   fees: dict[str, float]) -> dict[str, Any]:
    quotes = sorted(quotes, key=lambda q: q.apr, reverse=True)
    print(f"\n=== {coin}: funding сейчас ({len(quotes)} venue) ===")
    print(f"{'venue':<18} {'rate/интервал':>14} {'интервал':>9} {'APR %':>9}")
    print("-" * 54)
    for q in quotes:
        print(f"{q.venue:<18} {q.rate*100:>13.5f}% {q.interval_h:>8.0f}h {q.apr:>9.2f}")

    out: dict[str, Any] = {"coin": coin, "venues": [
        {"venue": q.venue, "symbol": q.symbol, "rate": q.rate,
         "interval_h": q.interval_h, "apr": q.apr} for q in quotes]}

    if len(quotes) >= 2:
        hi, lo = quotes[0], quotes[-1]
        gross = hi.apr - lo.apr
        fee_total = fees.get(hi.venue, FALLBACK_FEE_RT) + fees.get(lo.venue, FALLBACK_FEE_RT)
        net = net_apr(gross, fee_total, hold_days)
        print(f"\n  Лучшая пара: SHORT {hi.venue} / LONG {lo.venue}")
        print(f"    gross spread: {gross:>7.2f}% APR")
        print(f"    комиссии:     {fee_total*100:.3f}% RT обе ноги → {(fee_total*100)*(365/hold_days):.2f}% APR при hold {hold_days:.0f}d")
        print(f"    net:          {net:>7.2f}% APR" + ("" if net >= min_apr else f"   ← ниже порога {min_apr:.1f}%"))
        out["best_pair"] = {"short": hi.venue, "long": lo.venue, "gross_apr": gross,
                            "fee_rt_total": fee_total, "net_apr": net, "hold_days": hold_days}
        one_leg = max(quotes, key=lambda q: abs(q.apr))
        side = "SHORT" if one_leg.apr > 0 else "LONG"
        print(f"  Одна нога:   {side} {one_leg.venue} → {abs(one_leg.apr):.2f}% APR carry "
              f"(без хеджа: полный ценовой риск)")
        out["one_leg"] = {"venue": one_leg.venue, "side": side, "apr": one_leg.apr}
    return out


def print_history(coin: str, per_venue: dict[str, list[Quote]], days: int, hold_days: float,
                  fees: dict[str, float], top_pairs: int) -> dict[str, Any]:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    print(f"\n=== {coin}: funding за {days}d ({len(per_venue)} venue) ===")
    print(f"{'venue':<18} {'n':>6} {'mean APR':>10} {'median':>9} {'stdev':>9} {'% > 0':>7} {'min':>9} {'max':>9}")
    print("-" * 82)
    stats: dict[str, dict[str, float]] = {}
    for venue, series in sorted(per_venue.items(), key=lambda kv: -statistics.fmean([q.apr for q in kv[1]] or [0])):
        st = leg_stats(series)
        if not st:
            continue
        stats[venue] = st
        print(f"{venue:<18} {st['n']:>6.0f} {st['mean_apr']:>10.2f} {st['median_apr']:>9.2f} "
              f"{st['stdev_apr']:>9.2f} {st['pct_positive']:>6.1f}% {st['min_apr']:>9.2f} {st['max_apr']:>9.2f}")

    grids = {v: hourly_grid(s, start_ms, end_ms) for v, s in per_venue.items()}
    pairs: list[dict[str, Any]] = []
    venues = list(grids)
    for i, a in enumerate(venues):
        for b in venues[i + 1:]:
            for short_v, long_v in ((a, b), (b, a)):
                sp = spread_stats(grids[short_v], grids[long_v])
                if not sp or sp["mean_apr"] <= 0:
                    continue
                fee_total = fees.get(short_v, FALLBACK_FEE_RT) + fees.get(long_v, FALLBACK_FEE_RT)
                pairs.append({
                    "short": short_v, "long": long_v,
                    "gross_apr": sp["mean_apr"], "median_apr": sp["median_apr"],
                    "pct_positive": sp["pct_positive"], "worst_7d_apr": sp["worst_7d_apr"],
                    "hours": sp["hours"], "fee_rt_total": fee_total,
                    "net_apr": net_apr(sp["mean_apr"], fee_total, hold_days),
                })
    pairs.sort(key=lambda p: -p["net_apr"])

    if pairs:
        print(f"\n  Топ пар по net APR (hold {hold_days:.0f}d, комиссии обеих ног учтены):")
        print(f"  {'short':<16} {'long':<16} {'gross':>8} {'net':>8} {'% врем>0':>9} {'худш 7d':>9} {'часов':>7}")
        print("  " + "-" * 78)
        for p in pairs[:top_pairs]:
            print(f"  {p['short']:<16} {p['long']:<16} {p['gross_apr']:>8.2f} {p['net_apr']:>8.2f} "
                  f"{p['pct_positive']:>8.1f}% {p['worst_7d_apr']:>9.2f} {p['hours']:>7.0f}")
    else:
        print("\n  Пар с положительным средним спредом нет.")

    if stats:
        one = max(stats.items(), key=lambda kv: abs(kv[1]["mean_apr"]))
        side = "SHORT" if one[1]["mean_apr"] > 0 else "LONG"
        print(f"\n  Одна нога (без хеджа): {side} {one[0]} → {abs(one[1]['mean_apr']):.2f}% APR средний carry, "
              f"funding был {'положительным' if one[1]['mean_apr']>0 else 'отрицательным'} "
              f"{one[1]['pct_positive'] if one[1]['mean_apr']>0 else 100-one[1]['pct_positive']:.1f}% времени")
    return {"coin": coin, "days": days, "per_venue": stats, "pairs": pairs[:top_pairs]}


# ─────────────────────────────────────────────────────────────────────────────
# Одноногий carry и быстрый кросс-venue снимок
# ─────────────────────────────────────────────────────────────────────────────

def scan_one_leg(eid: str, coins: list[str], days: int, timeout_ms: int,
                 workers: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Одноногий carry: сколько funding реально собрал бы шорт (или лонг) по
    каждой монете на одном venue за N дней.

    Без хеджа — значит доход = funding, риск = полная цена монеты. Метрика,
    которая тут важна, не средний APR, а consistency: доля интервалов, где
    ставка была того же знака. 100% APR при 55% consistency — это не carry,
    это один всплеск на сквизе.
    """
    import threading

    base = make_client(eid, timeout_ms)
    base.load_markets()
    markets = base.markets
    tls = threading.local()

    def client() -> Any:
        if not hasattr(tls, "ex"):
            ex = make_client(eid, timeout_ms)
            ex.set_markets(markets)
            tls.ex = ex
        return tls.ex

    def work(coin: str) -> tuple[str, list[Quote] | str]:
        try:
            return coin, fetch_coin_history(client(), eid, coin, days)
        except Exception as exc:
            return coin, _short(exc)

    rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(work, c) for c in coins]):
            coin, out = fut.result()
            if isinstance(out, str):
                errors[coin] = out
                continue
            if not out:
                errors[coin] = "истории нет"
                continue
            aprs = [q.apr for q in out]
            mean = statistics.fmean(aprs)
            side = "SHORT" if mean > 0 else "LONG"
            consistency = 100.0 * (sum(1 for a in aprs if a > 0) if mean > 0
                                   else sum(1 for a in aprs if a < 0)) / len(aprs)
            signed = aprs if mean > 0 else [-a for a in aprs]
            # собранное в направлении сделки: шорт живёт на положительном
            # funding, лонг — на отрицательном, обе цифры положительные
            cum_pct = sum(q.rate for q in out) * 100.0 * (1 if mean > 0 else -1)
            rows.append({
                "coin": coin, "side": side, "n": len(aprs),
                "mean_apr": mean, "median_apr": statistics.median(aprs),
                "consistency_pct": consistency,
                "collected_pct": cum_pct,
                "collected_apr": cum_pct * 365.0 / days,
                "worst_7d_apr": _worst_window_mean(signed, window_h=24 * 7),
                "stdev_apr": statistics.pstdev(aprs) if len(aprs) > 1 else 0.0,
            })
    rows.sort(key=lambda r: -r["collected_apr"])
    return rows, errors


def print_one_leg(eid: str, rows: list[dict[str, Any]], errors: dict[str, str],
                  days: int, top: int, min_consistency: float) -> dict[str, Any]:
    fee_rt = DEFAULT_FEE_RT.get(eid, FALLBACK_FEE_RT) * 100.0
    print(f"\n=== {eid}: одноногий funding-carry за {days}d "
          f"({len(rows)} монет, {len(errors)} без данных) ===")
    print(f"{'coin':<12} {'side':<6} {'собрано %':>10} {'= APR %':>9} {'consist':>8} "
          f"{'худш 7d APR':>12} {'stdev':>9} {'n':>6}")
    print("-" * 80)
    stable = [r for r in rows if r["consistency_pct"] >= min_consistency]
    for r in stable[:top]:
        print(f"{r['coin']:<12} {r['side']:<6} {r['collected_pct']:>10.2f} {r['collected_apr']:>9.2f} "
              f"{r['consistency_pct']:>7.1f}% {r['worst_7d_apr']:>12.2f} {r['stdev_apr']:>9.1f} {r['n']:>6}")
    print(f"\n  Фильтр consistency ≥ {min_consistency:.0f}%: {len(stable)} из {len(rows)} монет")
    print(f"  Комиссия входа+выхода на {eid}: {fee_rt:.3f}% — вычесть из «собрано %» один раз")
    print("  ⚠ Без хеджа carry не арбитраж: цена монеты может съесть годовой funding за день.")
    return {"venue": eid, "days": days, "rows": rows, "errors": errors}


def hl_predicted(timeout: int = 30) -> dict[str, dict[str, Quote]]:
    """HL-эндпоинт `predictedFundings`: funding по ВСЕМ монетам HL сразу для
    HlPerp / BinPerp / BybitPerp одним запросом.

    Зачем отдельно от ccxt: один HTTP-запрос вместо 3×N, работает даже когда
    api.binance.com / api.bybit.com недоступны напрямую (Binance-ставку отдаёт
    сам HL). Сравнение только по трём venue — для остальных нужен `snapshot`.
    """
    import requests  # локально: остальной скрипт живёт на одном ccxt

    name_map = {"HlPerp": "hyperliquid", "BinPerp": "binance", "BybitPerp": "bybit"}
    resp = requests.post("https://api.hyperliquid.xyz/info",
                         json={"type": "predictedFundings"}, timeout=timeout)
    resp.raise_for_status()
    out: dict[str, dict[str, Quote]] = {}
    for coin, venues in resp.json():
        row: dict[str, Quote] = {}
        for raw_name, payload in venues:
            if not payload or payload.get("fundingRate") is None:
                continue
            venue = name_map.get(raw_name, raw_name.lower())
            row[venue] = Quote(
                venue=venue, coin=coin, symbol=coin,
                rate=float(payload["fundingRate"]),
                interval_h=float(payload.get("fundingIntervalHours") or DEFAULT_INTERVAL_H.get(venue, 8)),
                ts_ms=payload.get("nextFundingTime"),
            )
        if row:
            out[coin] = row
    return out


def print_hl_multi(rows: dict[str, dict[str, Quote]], min_apr: float, hold_days: float,
                   top: int, coins_filter: list[str] | None) -> dict[str, Any]:
    """Ранжирует все монеты HL по кросс-venue спреду funding."""
    ranked: list[dict[str, Any]] = []
    for coin, row in rows.items():
        if coins_filter and coin not in coins_filter:
            continue
        if len(row) < 2:
            continue
        hi = max(row.values(), key=lambda q: q.apr)
        lo = min(row.values(), key=lambda q: q.apr)
        gross = hi.apr - lo.apr
        fee_total = (DEFAULT_FEE_RT.get(hi.venue, FALLBACK_FEE_RT)
                     + DEFAULT_FEE_RT.get(lo.venue, FALLBACK_FEE_RT))
        ranked.append({
            "coin": coin, "short": hi.venue, "long": lo.venue,
            "gross_apr": gross, "net_apr": net_apr(gross, fee_total, hold_days),
            "aprs": {v: q.apr for v, q in row.items()},
        })
    ranked.sort(key=lambda r: -r["gross_apr"])

    print(f"\n=== Кросс-venue спред funding, {len(ranked)} монет (HL predictedFundings) ===")
    print(f"{'coin':<10} {'short':<12} {'long':<12} {'gross APR':>10} {'net APR':>9}   "
          f"{'HL':>8} {'Binance':>9} {'Bybit':>8}")
    print("-" * 88)
    for r in ranked[:top]:
        a = r["aprs"]
        print(f"{r['coin']:<10} {r['short']:<12} {r['long']:<12} {r['gross_apr']:>10.2f} {r['net_apr']:>9.2f}   "
              f"{a.get('hyperliquid', float('nan')):>8.2f} {a.get('binance', float('nan')):>9.2f} "
              f"{a.get('bybit', float('nan')):>8.2f}")
    above = [r for r in ranked if r["net_apr"] >= min_apr]
    print(f"\n  Монет с net ≥ {min_apr:.1f}% APR: {len(above)} / {len(ranked)} "
          f"(hold {hold_days:.0f}d, комиссии обеих ног учтены)")
    print("  ⚠ Это snapshot одного момента, не стабильность. Проверять режимом `history`.")
    return {"mode": "hl-multi", "ranked": ranked}


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _short(exc: Exception, limit: int = 110) -> str:
    msg = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
    return msg[:limit]


def _maybe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _resolve_venues(arg: str) -> list[str]:
    if arg in ("all", "auto", ""):
        return discover_venues()
    return [v.strip() for v in arg.split(",") if v.strip()]


def _dump(payload: dict[str, Any], path: Path | None, tag: str) -> None:
    if path is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = OUT_DIR / f"{tag}_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nJSON → {path}")


def _run_pool(fn, venues: Iterable[str], workers: int) -> list[VenueResult]:
    results: list[VenueResult] = []
    venues = list(venues)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fn, v): v for v in venues}
        for fut in as_completed(futs):
            v = futs[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append(VenueResult(venue=v, ok=False, error=_short(exc)))
    return sorted(results, key=lambda r: r.venue)


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["snapshot", "history", "one-leg", "hl-multi", "venues"])
    ap.add_argument("--coins", default="BTC,ETH,SOL",
                    help="'all' в режимах hl-multi / one-leg = все монеты venue")
    ap.add_argument("--venue", default="hyperliquid", help="venue для режима one-leg")
    ap.add_argument("--min-consistency", type=float, default=70.0,
                    help="one-leg: минимальная доля интервалов с одним знаком, %%")
    ap.add_argument("--venues", default="all", help="'all' (авто из ccxt) или список через запятую")
    ap.add_argument("--days", type=int, default=90, help="глубина истории (history)")
    ap.add_argument("--hold-days", type=float, default=30.0, help="срок удержания для амортизации комиссий")
    ap.add_argument("--min-apr", type=float, default=5.0, help="порог интереса, %% APR")
    ap.add_argument("--top-pairs", type=int, default=10)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=20000, help="таймаут HTTP, ms")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    venues = _resolve_venues(args.venues)

    if args.mode == "venues":
        print(f"ccxt {ccxt.__version__}: {len(venues)} venue с perp+funding")
        for v in venues:
            print(" ", v)
        return 0

    if args.mode == "one-leg":
        eid = args.venue
        if args.coins.strip().lower() == "all":
            probe = make_client(eid, args.timeout)
            probe.load_markets()
            coins = sorted({(m.get("base") or "").upper() for m in probe.markets.values()
                            if m.get("swap") and not m.get("inverse") and m.get("active", True)} - {""})
            print(f"{eid}: {len(coins)} perp-монет")
        rows, errors = scan_one_leg(eid, coins, args.days, args.timeout, args.workers)
        payload = print_one_leg(eid, rows, errors, args.days, args.top_pairs, args.min_consistency)
        payload["ts"] = datetime.now(timezone.utc).isoformat()
        _dump(payload, args.json, f"one_leg_{eid}")
        return 0

    if args.mode == "hl-multi":
        rows = hl_predicted(timeout=args.timeout // 1000)
        flt = None if args.coins.strip().lower() == "all" else coins
        payload = print_hl_multi(rows, args.min_apr, args.hold_days, args.top_pairs, flt)
        payload["ts"] = datetime.now(timezone.utc).isoformat()
        _dump(payload, args.json, "hl_multi")
        return 0

    print(f"ccxt {ccxt.__version__} | mode={args.mode} | coins={','.join(coins)} | venues={len(venues)}")

    if args.mode == "snapshot":
        results = _run_pool(lambda v: fetch_snapshot(v, coins, args.timeout), venues, args.workers)
        print_connectivity(results)
        payload: dict[str, Any] = {"mode": "snapshot", "ts": datetime.now(timezone.utc).isoformat(),
                                   "coins": {}, "ok": [r.venue for r in results if r.ok],
                                   "failed": {r.venue: r.error for r in results if not r.ok}}
        for coin in coins:
            quotes = [r.quotes[coin] for r in results if coin in r.quotes]
            fees = {r.venue: r.fee_rt.get(coin, FALLBACK_FEE_RT) for r in results if coin in r.quotes}
            if not quotes:
                print(f"\n=== {coin}: данных нет ===")
                continue
            payload["coins"][coin] = print_snapshot(coin, quotes, args.min_apr, args.hold_days, fees)
        _dump(payload, args.json, "snapshot")
        return 0

    results = _run_pool(lambda v: fetch_history(v, coins, args.days, args.timeout), venues, args.workers)
    print_connectivity(results)
    payload = {"mode": "history", "ts": datetime.now(timezone.utc).isoformat(), "days": args.days,
               "coins": {}, "ok": [r.venue for r in results if r.ok],
               "failed": {r.venue: r.error for r in results if not r.ok}}
    for coin in coins:
        per_venue = {r.venue: r.series[coin] for r in results if coin in r.series}
        fees = {r.venue: r.fee_rt.get(coin, FALLBACK_FEE_RT) for r in results if coin in r.series}
        if not per_venue:
            print(f"\n=== {coin}: истории нет ===")
            continue
        payload["coins"][coin] = print_history(coin, per_venue, args.days, args.hold_days, fees, args.top_pairs)
    _dump(payload, args.json, "history")
    return 0


if __name__ == "__main__":
    sys.exit(main())
