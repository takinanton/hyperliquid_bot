#!/usr/bin/env python3
"""Funding harvester scout — ранкинг коинов по УСТОЙЧИВОЙ net-of-costs funding yield.

Отвечает "почему фармить не по топу funding APR": топ по снапшоту почти всегда
(a) fail по ликвидности, (b) wrong-side hedge, или (c) СПАЙК — funding-история
показывает, что среднее держится около нуля/floor, а высокая цифра транзиентна.

СВЕРЕНО С КОДЕКСОМ (те же источники/пороги, что и бот):
  - Данные: public POST /info metaAndAssetCtxs  (bot/exchange.py:548, coin_filter.py:79)
  - Funding часовой (bot/exchange.py:531) -> APR = funding_hr * 24 * 365
  - История: public POST /info fundingHistory  (тот же info-эндпоинт)
  - Liquidity: OI>=$2M, vol>=$1M, spread<=0.1%, depth+-0.5%>=$5k (coin_filter.py:27-31)
  - Cost: taker 0.045%/side => 0.09% RT (CLAUDE.md), slip 0.1%/side (exchange.py:881)

Cross-venue (Nado/Extended/Pacifica) ТУТ НЕ ТЯНЕТСЯ — org egress-policy режет их
хосты (403 CONNECT), а bot-клиенты требуют SDK+ключи. Запускать на VPS. Причём
в самом боте Nado.funding_rate — заглушка return 0.0 (exchange_nado.py:325),
Extended идёт через x10 SDK, Pacifica-клиент отсутствует => cross-venue funding
надо сперва реализовать. См. вывод скрипта.

НИЧЕГО НЕ ТОРГУЕТ. Только read-only POST к публичному info-эндпоинту.

Usage:
  python3 scripts/funding_scan.py                       # снапшот, OI/Vol гейт
  python3 scripts/funding_scan.py --deep                # + реальный spread/depth
  python3 scripts/funding_scan.py --deep --history 7    # + 7д стабильность funding
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("requests not installed (pip install requests / venv бота)")

HL_INFO = "https://api.hyperliquid.xyz/info"

# --- пороги/косты 1:1 с ботом ---
MIN_OI_USD = 2_000_000       # coin_filter.py:27
MIN_VOL_24H_USD = 1_000_000  # coin_filter.py:28
MAX_SPREAD_PCT = 0.1         # coin_filter.py:29
MIN_DEPTH_05_USD = 5_000     # coin_filter.py:30
HL_TAKER_FEE = 0.00045       # 0.045%/side => 0.09% RT (CLAUDE.md)
HL_SLIP_DEFAULT = 0.001      # 0.1%/side fallback (exchange.py:881)
HL_FLOOR_APR = 11.0          # наблюдаемый HL baseline funding (interest component)


def _post(payload: dict, timeout: float = 15.0) -> object:
    r = requests.post(HL_INFO, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_universe() -> list[dict]:
    data = _post({"type": "metaAndAssetCtxs"})
    out: list[dict] = []
    if not (isinstance(data, list) and len(data) >= 2):
        return out
    for i, asset in enumerate(data[0].get("universe", [])):
        if i >= len(data[1]):
            continue
        a = data[1][i]
        mark = float(a.get("markPx", 0) or 0)
        if mark <= 0:
            continue
        out.append({
            "name": asset["name"],
            "funding_hr": float(a.get("funding", 0) or 0),
            "mark": mark,
            "oi_usd": float(a.get("openInterest", 0) or 0) * mark,
            "vol_usd": float(a.get("dayNtlVlm", 0) or 0),
            "spread_pct": None, "depth_05": None, "hist": None,
        })
    return out


def add_book_metrics(coin: dict) -> None:
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


def add_history(coin: dict, days: float) -> None:
    """Стабильность funding за окно: avg signed APR, same-side %, n часов."""
    try:
        start = int((time.time() - days * 86400) * 1000)
        h = _post({"type": "fundingHistory", "coin": coin["name"], "startTime": start}, timeout=12)
        if not isinstance(h, list) or not h:
            return
        aprs = [float(x.get("fundingRate", 0)) * 24 * 365 * 100 for x in h]
        n = len(aprs)
        now_sign = 1 if coin["funding_hr"] >= 0 else -1
        avg_signed = sum(aprs) / n
        same_side = sum(1 for a in aprs if (a >= 0) == (now_sign >= 0)) / n * 100
        expected_hrs = days * 24
        coin["hist"] = {
            "avg_signed": avg_signed,
            "same_side_pct": same_side,
            "n": n,
            "new": n < expected_hrs * 0.5,   # мало истории => новый листинг
        }
    except Exception:
        return


def per_side_cost(coin: dict) -> float:
    if coin.get("spread_pct") is not None:
        slip = max(HL_TAKER_FEE, (coin["spread_pct"] / 100) / 2)
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


def verdict(coin: dict) -> str:
    """STABLE / FLIP / SPIKE / NEW / — на основе funding-истории."""
    h = coin.get("hist")
    if not h:
        return "—"
    if h["new"]:
        return "NEW"          # <50% ожидаемой истории — новый листинг, риск
    if h["same_side_pct"] < 70:
        return "FLIP"         # funding часто меняет знак — нельзя статично харвестить
    snap = abs(coin["apr"])
    avg = abs(h["avg_signed"])
    if snap > max(2 * avg, HL_FLOOR_APR + 15):
        return "SPIKE"        # снапшот сильно выше среднего — транзиент
    return "STABLE"


def harvest_apr(coin: dict) -> float:
    """Ожидаемая доходность харвеста: если есть история — среднее (signed magnitude),
    иначе снапшот. Это то, что реально соберёшь за холд, а не пиковая цифра."""
    h = coin.get("hist")
    if h and not h["new"]:
        return abs(h["avg_signed"])
    return abs(coin["apr"])


def enrich(coin: dict, hold_days: float) -> dict:
    coin["apr"] = abs(coin["funding_hr"]) * 24 * 365 * 100  # снапшот APR, %
    coin["side"] = "SHORT-perp" if coin["funding_hr"] > 0 else "LONG-perp"
    coin["liq"] = liquidity_pass(coin)
    coin["verdict"] = verdict(coin)
    hv = harvest_apr(coin)                       # %
    psc = per_side_cost(coin)
    rt_neutral = 4 * psc * 100                    # %  (2 ноги x 2 side)
    daily = hv / 365
    coin["rt_cost_pct"] = rt_neutral
    coin["breakeven_days"] = (rt_neutral / daily) if daily > 0 else float("inf")
    coin["net_apr"] = ((hv * hold_days / 365) - rt_neutral) * 365 / hold_days
    coin["hv"] = hv
    return coin


def fmt(coin: dict) -> str:
    be = coin["breakeven_days"]
    be_s = f"{be:5.1f}" if be != float("inf") else "  inf"
    sp = f"{coin['spread_pct']:.3f}" if coin.get("spread_pct") is not None else "  -"
    avg = f"{coin['hist']['avg_signed']:6.1f}" if coin.get("hist") else "     -"
    return (f"{coin['name']:<11} {coin['apr']:6.0f}% {avg:>7} {coin['verdict']:<6} "
            f"{coin['side']:<10} {coin['vol_usd']/1e6:7.1f}M {sp:>6} "
            f"{be_s} {coin['net_apr']:6.0f}%  {'PASS' if coin['liq'] else 'fail'}")


HEADER = (f"{'coin':<11} {'snapAPR':>7} {'avg7d':>7} {'stab':<6} {'harvest':<10} "
          f"{'vol24h':>8} {'spr%':>6} {'be_d':>5} {'netAPR':>6}  liq")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=22)
    ap.add_argument("--hold", type=float, default=30, help="reference hold (дни)")
    ap.add_argument("--deep", action="store_true", help="реальный spread/depth из l2Book")
    ap.add_argument("--history", type=float, default=0, help="дней funding-истории для стабильности")
    ap.add_argument("--watch", nargs="*", default=["HYPE", "FARTCOIN"])
    args = ap.parse_args()

    print(f"# Funding scout @ {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())} "
          f"| HL metaAndAssetCtxs (public) | hold={args.hold}d hist={args.history}d")
    coins = fetch_universe()
    if not coins:
        print("[!] metaAndAssetCtxs пусто — нет egress к HL?", file=sys.stderr)
        return 2
    print(f"# universe: {len(coins)} perps")
    coins.sort(key=lambda c: abs(c["funding_hr"]), reverse=True)

    watch_set = set(args.watch)
    focus = coins[:args.top] + [c for c in coins[args.top:] if c["name"] in watch_set]
    if args.deep:
        print(f"# --deep: l2Book для {len(focus)} coins...")
        for c in focus:
            add_book_metrics(c); time.sleep(0.12)
    if args.history:
        print(f"# --history: {args.history}d funding для {len(focus)} coins...")
        for c in focus:
            add_history(c, args.history); time.sleep(0.12)

    for c in coins:
        enrich(c, args.hold)

    print("\n== TOP by RAW снапшот funding (наивный выбор) ==")
    print(HEADER)
    for c in coins[:args.top]:
        print(fmt(c))

    liq = [c for c in coins if c["liq"]]
    # если есть история — ранкуем по реальной harvest-доходности, иначе по снапшоту
    key = (lambda c: (c["verdict"] in ("STABLE", "OK", "—"), c["net_apr"])) if args.history \
        else (lambda c: c["net_apr"])
    liq.sort(key=key, reverse=True)
    tag = "по NET (стабильные сверху)" if args.history else "по NET снапшота"
    print(f"\n== TOP среди liquidity-PASS, {tag} ({len(liq)} прошли гейт) ==")
    print(HEADER)
    for c in liq[:args.top]:
        print(fmt(c))

    print("\n== WATCH (текущий портфель) ==")
    print(HEADER)
    by_name = {c["name"]: c for c in coins}
    for w in args.watch:
        c = by_name.get(w)
        print(fmt(c) if c else f"{w:<11} — нет в universe")

    # вывод
    stable_liq = [c for c in liq if c.get("verdict") == "STABLE"] if args.history else liq
    print(f"\n# raw-снапшот топ = {coins[0]['name']} ({coins[0]['apr']:.0f}%, {coins[0]['verdict']})")
    if args.history and stable_liq:
        best = max(stable_liq, key=lambda c: c["net_apr"])
        print(f"# лучший СТАБИЛЬНЫЙ liquidity-PASS = {best['name']}: "
              f"harvest~{best['hv']:.0f}% net~{best['net_apr']:.0f}%")
        print("# => высокий снапшот-funding != доход: смотри avg7d + stab, а не пик.")
    print("# cross-venue (Nado/Extended/Pacifica) недостижим отсюда (egress policy) — на VPS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
