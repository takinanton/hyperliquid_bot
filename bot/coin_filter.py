"""Daily coin whitelist refresh — фильтр по OI/Vol/orderbook depth.

Юзер 2026-05-06: "массовый фильтр чтобы не пинговать пары которые нам не подходят".

Threshold (default):
  - OI ≥ $2M
  - Vol_24h ≥ $1M
  - spread ≤ 0.1%
  - depth at ±0.5% ≥ $5k (both sides)
  - levels in ±0.5% band ≥ 3

Whitelist saved to JSON file. Bot's main loop reads it and skips non-listed coins.
Stale whitelist (>24h) → fallback to full universe для safety.
"""
from __future__ import annotations
import json
import logging
import os
import time
from typing import Optional, Set

log = logging.getLogger(__name__)

# Defaults (override via env)
WHITELIST_FILE = os.getenv("WHITELIST_FILE",
                           "/root/hyperliquid_bot/data/coin_whitelist.json")
MIN_OI_USD = float(os.getenv("FILTER_MIN_OI_USD", "2000000"))
MIN_VOL_24H_USD = float(os.getenv("FILTER_MIN_VOL_24H_USD", "1000000"))
MAX_SPREAD_PCT = float(os.getenv("FILTER_MAX_SPREAD_PCT", "0.1"))
MIN_DEPTH_05_USD = float(os.getenv("FILTER_MIN_DEPTH_05_USD", "5000"))
MIN_LEVELS_05 = int(os.getenv("FILTER_MIN_LEVELS_05", "3"))
WHITELIST_TTL_HOURS = float(os.getenv("WHITELIST_TTL_HOURS", "24"))


def _book_metrics(client, coin: str) -> Optional[dict]:
    """Returns {spread_pct, min_depth_05, min_levels_05} or None if fetch fails."""
    try:
        book = client.info.l2_snapshot(coin)
        levels = book.get("levels", [])
        if len(levels) < 2: return None
        bids, asks = levels[0], levels[1]
        if not bids or not asks: return None
        best_bid = float(bids[0]["px"]); best_ask = float(asks[0]["px"])
        if best_ask <= best_bid: return None
        mid = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid * 100

        def cum(side, pct):
            total = 0.0; count = 0
            for lev in side:
                try: px = float(lev["px"]); sz = float(lev["sz"])
                except: continue
                if abs(px - mid) / mid <= pct / 100:
                    total += px * sz
                    count += 1
            return total, count

        bid_d, bid_l = cum(bids, 0.5)
        ask_d, ask_l = cum(asks, 0.5)
        return {
            "spread_pct": spread_pct,
            "min_depth_05": min(bid_d, ask_d),
            "min_levels_05": min(bid_l, ask_l),
        }
    except Exception as e:
        log.debug("book_metrics(%s) failed: %s", coin, e)
        return None


def refresh_whitelist(client) -> list[str]:
    """Run full filter: OI/Vol pre-filter + orderbook check. Returns list of coin names.

    Side effect: saves to WHITELIST_FILE.
    """
    log.info("Coin filter refresh: starting full universe scan...")
    t0 = time.time()
    import requests
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "metaAndAssetCtxs"}, timeout=15).json()
    except Exception as e:
        log.error("metaAndAssetCtxs fetch failed: %s — keeping old whitelist", e)
        return load_whitelist() or []

    candidates: list[str] = []
    if isinstance(r, list) and len(r) >= 2:
        for i, asset in enumerate(r[0].get("universe", [])):
            if i >= len(r[1]): continue
            a = r[1][i]
            mark = float(a.get("markPx", 0) or 0)
            oi_usd = float(a.get("openInterest", 0) or 0) * mark
            vol_usd = float(a.get("dayNtlVlm", 0) or 0)
            if oi_usd >= MIN_OI_USD and vol_usd >= MIN_VOL_24H_USD:
                candidates.append(asset["name"])

    # HIP-3 xyz
    try:
        r2 = requests.post("https://api.hyperliquid.xyz/info",
                           json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=15).json()
        if isinstance(r2, list) and len(r2) >= 2:
            for i, asset in enumerate(r2[0].get("universe", [])):
                if i >= len(r2[1]): continue
                a = r2[1][i]
                mark = float(a.get("markPx", 0) or 0)
                oi_usd = float(a.get("openInterest", 0) or 0) * mark
                vol_usd = float(a.get("dayNtlVlm", 0) or 0)
                if oi_usd >= MIN_OI_USD and vol_usd >= MIN_VOL_24H_USD:
                    candidates.append(f"xyz:{asset['name']}")
    except Exception as e:
        log.warning("HIP-3 universe fetch failed: %s", e)

    log.info("After OI/Vol pre-filter: %d candidates from universe", len(candidates))

    # Orderbook check
    final: list[str] = []
    for i, coin in enumerate(candidates):
        m = _book_metrics(client, coin)
        if m is None:
            continue
        if (m["spread_pct"] <= MAX_SPREAD_PCT
                and m["min_depth_05"] >= MIN_DEPTH_05_USD
                and m["min_levels_05"] >= MIN_LEVELS_05):
            final.append(coin)
        time.sleep(0.15)  # rate limit safe
        if (i + 1) % 20 == 0:
            log.debug("Filter progress: %d/%d", i + 1, len(candidates))

    elapsed = time.time() - t0
    log.info(
        "Coin filter refresh complete: %d → %d coins passed (took %.0fs)",
        len(candidates), len(final), elapsed,
    )

    # Save
    payload = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "thresholds": {
            "min_oi_usd": MIN_OI_USD,
            "min_vol_24h_usd": MIN_VOL_24H_USD,
            "max_spread_pct": MAX_SPREAD_PCT,
            "min_depth_05_usd": MIN_DEPTH_05_USD,
            "min_levels_05": MIN_LEVELS_05,
        },
        "universe_size": len(candidates) + (len(r[0].get("universe", [])) - len(candidates) if isinstance(r, list) else 0),
        "passed": len(final),
        "coins": sorted(final),
    }
    try:
        os.makedirs(os.path.dirname(WHITELIST_FILE), exist_ok=True)
        with open(WHITELIST_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        log.info("Whitelist saved to %s", WHITELIST_FILE)
    except Exception as e:
        log.error("Whitelist save failed: %s", e)

    return final


def load_whitelist() -> Optional[Set[str]]:
    """Load whitelist if fresh (≤TTL hours). Returns None if stale or missing.

    Bot должен fall back на full universe если whitelist missing/stale —
    safer (полнота) чем no trades.
    """
    try:
        with open(WHITELIST_FILE) as f:
            data = json.load(f)
        ts = float(data.get("ts", 0))
        age_hours = (time.time() - ts) / 3600
        if age_hours > WHITELIST_TTL_HOURS:
            log.warning(
                "Whitelist stale (%.1fh > %.0fh TTL) — falling back to full universe",
                age_hours, WHITELIST_TTL_HOURS,
            )
            return None
        coins = set(data.get("coins", []))
        if not coins:
            log.warning("Whitelist empty — falling back to full universe")
            return None
        return coins
    except FileNotFoundError:
        log.info("Whitelist file not found — using full universe")
        return None
    except Exception as e:
        log.warning("Whitelist load failed: %s — using full universe", e)
        return None


if __name__ == "__main__":
    # Manual run from CLI: python -m bot.coin_filter
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from bot.config import Settings
    from bot.exchange_factory import get_exchange_client
    s = Settings.from_env()
    client = get_exchange_client(s)
    final = refresh_whitelist(client)
    print(f"\nWhitelist: {len(final)} coins")
    for c in final[:30]:
        print(f"  {c}")
    if len(final) > 30:
        print(f"  ... and {len(final) - 30} more")
