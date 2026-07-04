"""Per-sec_type data depth audit. Runs on komp where the cache is real."""
import json, sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from bot.data_source import to_yf_ticker

CACHE = ROOT / "data" / "yf_cache_ib"
UNIV = ROOT / "data" / "ib_universe_top1000.json"
FULL = ROOT / "data" / "ib_universe.json"

sec_by_sym = {}
top = json.loads(UNIV.read_text())
for s in top["symbols"]:
    sec_by_sym[s["sym"]] = s["type"]
full = json.loads(FULL.read_text())
for s in full["symbols"]:
    if s["type"] == "future" and s["sym"] not in sec_by_sym:
        sec_by_sym[s["sym"]] = "future"

def span(p):
    if not p.exists(): return None, None, 0
    try:
        df = pd.read_parquet(p)
        if "time" not in df.columns or df.empty: return None, None, 0
        return pd.Timestamp(df["time"].iloc[0]), pd.Timestamp(df["time"].iloc[-1]), len(df)
    except Exception:
        return None, None, 0

# Aggregate per sec_type
agg = {"stock": [], "fx": [], "future": []}
for sym, sec in sec_by_sym.items():
    if sec not in agg: continue
    tk = to_yf_ticker(sym)
    first, last, n = span(CACHE / "1h" / f"{tk}.parquet")
    if first is not None and last is not None:
        agg[sec].append((sym, first, last, (last - first).days, n))

print(f"\n=== Per sec_type 1h cache depth ===\n")
print(f"{'sec_type':<8} {'n_syms':>6} {'min_d':>6} {'p25':>5} {'median':>6} {'p75':>5} {'max_d':>6} {'1h_bars_med':>11}")
print("-" * 70)
for sec, items in agg.items():
    if not items:
        print(f"{sec:<8} (no files found)")
        continue
    days = sorted(x[3] for x in items)
    bars = sorted(x[4] for x in items)
    def p(arr, q):
        idx = max(0, min(len(arr)-1, int(len(arr)*q)))
        return arr[idx]
    print(f"{sec:<8} {len(items):>6} {min(days):>6} {p(days,0.25):>5} {p(days,0.5):>6} {p(days,0.75):>5} {max(days):>6} {p(bars,0.5):>11}")

# Show samples
print("\n=== Sample first/last (5 per sec) ===")
for sec, items in agg.items():
    print(f"\n{sec}:")
    for sym, first, last, days, n in items[:5]:
        print(f"  {sym:<10}  {str(first)[:19]} -> {str(last)[:19]}  ({days}d, {n} bars)")
