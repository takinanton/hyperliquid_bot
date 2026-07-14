#!/usr/bin/env python3
"""Funding harvester scout — read-only ranking of coins by NET-of-costs funding yield.

Отвечает на вопрос "почему фармить не по топу funding APR, а по net": сортирует
рынок по чистой доходности после liquidity-гейта и costs, а не по голому funding.

СВЕРЕНО С КОДЕКСОМ (использует те же источники/пороги, что и бот):
  - Данные: публичный POST /info {"type":"metaAndAssetCtxs"}  — ровно как
    bot/exchange.py:548 (_get_funding_map) и bot/coin_filter.py:79-94.
    Ключи НЕ нужны (read-only, public endpoint).
  - Funding часовой (bot/exchange.py:531 "часовой") → APR = funding_hr * 24 * 365.
  - Liquidity-гейт: MIN_OI_USD=$2M, MIN_VOL_24H_USD=$1M, spread<=0.1%,
    depth+-0.5%>=$5k  (bot/coin_filter.py:27-31).
  - Cost: HL taker 0.045%/side => 0.09% RT (CLAUDE.md "HL 0.09%"),
    slip default 0.1%/side (bot/exchange.py:881 fallback).

НИЧЕГО НЕ ТОРГУЕТ. Только GET-подобные POST-запросы к публичному info-эндпоинту.

Usage:
  python3 scripts/funding_scan.py                 # main perp dex, OI/Vol gate
  python3 scripts/funding_scan.py --deep          # + реальный spread/depth из l2Book
  python3 scripts/funding_scan.py --top 40 --hold 30 --notional 2000
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("requests not installed (pip install requests, либо запусти в venv бота)")

HL_INFO = "https://api.hyperliquid.xyz/info"

# --- пороги/косты 1:1 с ботом ---
MIN_OI_USD = 2_000_000       # coin_filter.py:27
MIN_VOL_24H_USD = 1_000_000  # coin_filter.py:28
MAX_SPREAD_PCT = 0.1         # coin_filter.py:29
MIN_DEPTH_05_USD = 5_000     # coin_filter.py:30
HL_TAKER_FEE = 0.00045       # 0.045%/side => 0.09% RT (CLAUDE.md)
HL_SLIP_DEFAULT = 0.001      # 0.1%/side fallback (exchange.py:881)


def _post(payload: dict, timeout: float = 15.0) -> object:
    r = requests.post(HL_INFO, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_universe() -> list[dict]:
    """Возвращает [{name, funding_hr, mark, oi_usd, vol_usd}] для main perp dex."""
    data = _post({"type": "metaAndAssetCtxs"})
    out: list[dict] = []
    if not (isinstance(data, list) and len(data) >= 2):
        return out
    universe = data[0].get("universe", [])
    ctxs = data[1]
    for i, asset in enumerate(universe):
        if i >= len(ctxs):
            continue
        a = ctxs[i]
        mark = float(a.get("markPx", 0) or 0)
        funding_hr = float(a.get("funding", 0) or 0)
        oi_usd = float(a.get("openInterest", 0) or 0) * mark
        vol_usd = float(a.get("dayNtlVlm", 0) or 0)
        if mark <= 0:
            continue
        out.append({
            "name": asset["name"],
            "funding_hr": funding_hr,
            "mark": mark,
            "oi_usd": oi_usd,
            "vol_usd": vol_usd,
            "spread_pct": None,
            "depth_05": None,
        })
    return out


def add_book_metrics(coin: dict) -> None:
    """Заполняет spread_pct + depth_05 из публичного l2Book (как coin_filter._book_metrics)."""
    try:
        book = _post({"type": "l2Book", "coin": coin["name"]}, timeout=10)
        levels = book.get("levels", []) if isinstance(book, dict) else []
        if len(levels) < 2 or not levels[0] or not levels[1]:
            return
        bids, asks = levels[0], levels[1]
        best_bid = float(bids[0]["px"]); best_ask = float(asks[0]["px"])
        if best_ask <= best_bid:
            return
        mid = (best_bid + best_ask) / 2
        coin["spread_pct"] = (best_ask - best_bid) / mid * 100

        def cum(side):
            tot = 0.0
            for lev in side:
                try:
                    px = float(lev["px"]); sz = float(lev["sz"])
                except Exception:
                    continue
                if abs(px - mid) / mid <= 0.005:
                    tot += px * sz
            return tot
        coin["depth_05"] = min(cum(bids), cum(asks))
    except Exception:
        return


def per_side_cost(coin: dict) -> float:
    """Fee + slip оценка на одну сторону (доля notional)."""
    if coin.get("spread_pct") is not None:
        slip = max(HL_TAKER_FEE, (coin["spread_pct"] / 100) / 2)  # half-spread as clip proxy
    else:
        slip = HL_SLIP_DEFAULT
    return HL_TAKER_FEE + slip


def liquidity_pass(coin: dict) -> bool:
    if coin["oi_usd"] < MIN_OI_USD or coin["vol_usd"] < MIN_VOL_24H_USD:
        return False
    if coin.get("spread_pct") is not None and coin["spread_pct"] > MAX_SPREAD_PCT:
        return False
    if coin.get("depth_05") is not None and coin["depth_05"] < MIN_DEPTH_05_USD:
        return False
    return True


def enrich(coin: dict, hold_days: float) -> dict:
    apr = abs(coin["funding_hr"]) * 24 * 365          # harvestable: берём receiving-сторону
    psc = per_side_cost(coin)
    rt_neutral = 4 * psc                               # 2 ноги x 2 side (delta-neutral open+close)
    daily = apr / 365
    breakeven = (rt_neutral / daily) if daily > 0 else float("inf")
    net_apr = ((apr * hold_days / 365) - rt_neutral) * 365 / hold_days
    coin.update({
        "apr": apr,
        "side": "SHORT-perp" if coin["funding_hr"] > 0 else "LONG-perp",
        "rt_cost_pct": rt_neutral * 100,
        "breakeven_days": breakeven,
        "net_apr": net_apr,
        "liq": liquidity_pass(coin),
    })
    return coin


def fmt(coin: dict) -> str:
    be = coin["breakeven_days"]
    be_s = f"{be:6.1f}" if be != float("inf") else "   inf"
    sp = f"{coin['spread_pct']:.3f}" if coin.get("spread_pct") is not None else "  -"
    return (f"{coin['name']:<12} {coin['apr']*100:7.1f}% {coin['side']:<10} "
            f"{coin['vol_usd']/1e6:8.1f}M {coin['oi_usd']/1e6:7.1f}M {sp:>6} "
            f"{coin['rt_cost_pct']:5.2f}% {be_s} {coin['net_apr']*100:7.1f}%  "
            f"{'PASS' if coin['liq'] else 'fail'}")


HEADER = (f"{'coin':<12} {'fundAPR':>8} {'harvest':<10} "
          f"{'vol24h':>9} {'OI':>8} {'spr%':>6} {'rtCost':>6} {'be_d':>7} {'netAPR30':>8}  liq")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30, help="сколько строк показать")
    ap.add_argument("--hold", type=float, default=30, help="reference hold в днях для net")
    ap.add_argument("--notional", type=float, default=2000, help="target notional/leg (для будущего size-aware slip)")
    ap.add_argument("--deep", action="store_true", help="тянуть реальный spread/depth из l2Book (top-N by funding)")
    ap.add_argument("--watch", nargs="*", default=["HYPE", "FARTCOIN"], help="коины портфеля для сравнения")
    args = ap.parse_args()

    print(f"# Funding scout @ {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())} "
          f"| source: HL metaAndAssetCtxs (public) | hold={args.hold}d")
    t0 = time.time()
    coins = fetch_universe()
    if not coins:
        return _diag("metaAndAssetCtxs вернул пусто/не список")
    print(f"# universe: {len(coins)} perps | fetch {time.time()-t0:.1f}s")

    coins.sort(key=lambda c: abs(c["funding_hr"]), reverse=True)

    if args.deep:
        n = min(args.top, len(coins))
        watch_set = set(args.watch)
        deep_targets = coins[:n] + [c for c in coins[n:] if c["name"] in watch_set]
        print(f"# --deep: тяну l2Book для top-{n} by funding + watch ({len(deep_targets)} coins)...")
        for c in deep_targets:
            add_book_metrics(c)
            time.sleep(0.12)  # rate-limit safe (coin_filter: 0.15s)

    for c in coins:
        enrich(c, args.hold)

    # --- Раздел 1: топ по ГОЛОМУ funding (то, что соблазняет) ---
    print("\n== TOP by RAW funding APR (наивный выбор) ==")
    print(HEADER)
    for c in coins[:args.top]:
        print(fmt(c))

    # --- Раздел 2: топ по NET среди прошедших liquidity-гейт ---
    liq = [c for c in coins if c["liq"]]
    liq.sort(key=lambda c: c["net_apr"], reverse=True)
    print(f"\n== TOP by NET APR@{int(args.hold)}d, только liquidity-PASS ({len(liq)} passed gate) ==")
    print(HEADER)
    for c in liq[:args.top]:
        print(fmt(c))

    # --- Раздел 3: портфельные коины ---
    print("\n== WATCH (текущий портфель) ==")
    print(HEADER)
    by_name = {c["name"]: c for c in coins}
    for w in args.watch:
        c = by_name.get(w)
        print(fmt(c) if c else f"{w:<12} — нет в universe")

    # --- вывод ---
    if liq:
        raw_top = coins[0]["name"]
        net_top = liq[0]["name"]
        print(f"\n# raw-funding топ = {raw_top} | net-топ (liq-pass) = {net_top}")
        print("# если raw-топ != net-топ — это и есть ответ 'почему не по топу funding'.")
    return 0


def _diag(msg: str) -> int:
    print(f"\n[!] {msg}", file=sys.stderr)
    print("[!] Возможно нет egress к api.hyperliquid.xyz из этой среды.", file=sys.stderr)
    print("[!] Тогда запусти на VPS: python3 scripts/funding_scan.py --deep", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
