"""WS probe — независимая верификация step 2 WS migration.

Запускается на VPS параллельно с прод-ботом (своё подключение, отдельный
HLClient instance, не мешает прод-боту). За N секунд подписывается на mix
из активных coin'ов (BTC, ETH, SOL), тонких (RSR, AERO), HIP-3 (xyz:NVDA),
открытых на боте позиций. Печатает:
  - WS msg rate per (coin, interval)
  - Cache freshness latency (мс между push и cache write)
  - HIP-3 push behaviour (есть/нет)
  - Top-of-bar transition handling

Usage: HL_WS_CANDLES=true /root/hyperliquid_bot/venv/bin/python3 scripts/ws_probe.py [duration_sec]
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, "/root/hyperliquid_bot")
os.environ["HL_WS_CANDLES"] = "true"

from bot.config import Settings
from bot.exchange import HLClient

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 90
PROBE_COINS = [
    # active majors (high volume — много pushes)
    ("BTC", "4h"), ("BTC", "1d"),
    ("ETH", "4h"), ("ETH", "1d"),
    ("SOL", "4h"), ("SOL", "1d"),
    # mid-active
    ("AVAX", "4h"), ("DOGE", "4h"),
    # thin alts (низкий volume, проверим silent-coin поведение)
    ("RSR", "4h"), ("AERO", "4h"),
    # HIP-3 (проверим если HL пушит candle data для xyz dex)
    ("xyz:NVDA", "4h"), ("xyz:TSLA", "4h"), ("xyz:GOLD", "4h"),
]


def main():
    settings = Settings.from_env()
    client = HLClient(settings)
    print(f"[probe] init done, duration={DURATION}s, ws_enabled={client._ws_enabled}")

    # Bootstrap REST + lazy subscribe
    print("\n[probe] bootstrapping via REST (populates cache + subscribes WS)...")
    t0 = time.time()
    for coin, interval in PROBE_COINS:
        try:
            df = client.candles(coin, interval, limit=200)
            print(f"  {coin:14} {interval}: {len(df)} bars (last close=${float(df['Close'].iloc[-1]):.4f})"
                  if not df.empty else f"  {coin:14} {interval}: EMPTY")
        except Exception as e:
            print(f"  {coin:14} {interval}: BOOTSTRAP FAIL: {e}")
    print(f"[probe] bootstrap took {time.time() - t0:.1f}s, "
          f"{len(client._ws_subscriptions)} WS subs active")

    # Wait + sample
    print(f"\n[probe] sampling WS msgs for {DURATION}s...")
    msg_counts: dict[tuple[str, str], list[int]] = defaultdict(list)
    sample_interval = 15
    elapsed = 0
    prev_total = client._ws_msg_total
    snapshot_prev = dict(client._ws_last_msg_at)
    while elapsed < DURATION:
        time.sleep(sample_interval)
        elapsed += sample_interval
        with client._ws_lock:
            cur_total = client._ws_msg_total
            cur_snapshot = dict(client._ws_last_msg_at)
        delta_total = cur_total - prev_total
        rate = delta_total / sample_interval
        # per-coin update count
        n_active = sum(
            1 for k, t in cur_snapshot.items()
            if (time.time() - t) < 60
        )
        print(f"  +{elapsed:3}s: {delta_total:5} new msgs ({rate:.1f}/s), "
              f"{n_active}/{len(cur_snapshot)} active <60s")
        prev_total = cur_total
        snapshot_prev = cur_snapshot

    # Per-coin breakdown
    print("\n[probe] per-coin WS push rate (last 60s active = pushed in last 60s):")
    print(f"  {'coin':14} {'tf':3}  push_age  cached_bar_t        cache_age")
    print("  " + "-" * 70)
    now = time.time()
    with client._ws_lock:
        last_msgs = dict(client._ws_last_msg_at)
        cache_snap = dict(client._candles_cache)
    for coin, interval in PROBE_COINS:
        key = (coin, interval)
        last = last_msgs.get(key, 0.0)
        push_age = now - last if last > 0 else float("inf")
        push_age_str = f"{push_age:6.1f}s" if push_age < 1e6 else "  never"
        cached = cache_snap.get(key)
        if cached:
            bar_t_ms, df, fetched_at = cached
            from datetime import datetime, timezone
            bar_t_iso = datetime.fromtimestamp(bar_t_ms / 1000, tz=timezone.utc).isoformat()[:19]
            cache_age = now - fetched_at
            cache_age_str = f"{cache_age:6.1f}s"
        else:
            bar_t_iso = "—"
            cache_age_str = "—"
        print(f"  {coin:14} {interval:3}  {push_age_str}  {bar_t_iso}  {cache_age_str}")

    # Verify cache freshness vs wall-clock
    print("\n[probe] HIP-3 push status (xyz:* coins):")
    hip3_keys = [k for k in last_msgs if k[0].startswith("xyz:")]
    if not hip3_keys:
        print("  (no xyz: coins in probe set)")
    else:
        any_active = False
        for key in hip3_keys:
            last = last_msgs[key]
            if last > 0 and (now - last) < 600:
                any_active = True
                age = now - last
                print(f"  ✅ {key[0]:14} {key[1]:3}: pushed {age:.1f}s ago")
            else:
                print(f"  ❌ {key[0]:14} {key[1]:3}: NO PUSH (REST fallback active)")
        if not any_active:
            print("  → HIP-3 WS candle channel НЕ работает; xyz:* остаётся на REST polling")

    # Stop watchdog and disconnect cleanly
    client._ws_stop.set()
    try:
        client.info.disconnect_websocket()
    except Exception:
        pass
    print(f"\n[probe] done. total msgs received: {client._ws_msg_total}")


if __name__ == "__main__":
    main()
