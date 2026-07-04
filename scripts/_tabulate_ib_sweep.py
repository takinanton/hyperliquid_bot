"""Tabulate IB sweep results into a one-screen table.

Reads:
  data/honest_replay/ib_baseline.json
  data/honest_replay/sweep_ib/*.json
  data/honest_replay/ib_smoke.json (skipped if not relevant)

Prints two tables:
  1. ALL runs (baseline + all combos) sorted by start order, with: combo, n_trades,
     avgR, net_R, MM_avg, MM_max, DD%, win%, sec_type breakdown
  2. RANKINGS — top by net_R, top in MM ∈ [30,40] band
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path

HL_ROOT = Path(os.environ.get("HL_ROOT", str(Path(__file__).parent.parent.parent)))
DR = HL_ROOT / "hyperliquid_bot" / "data" / "honest_replay"
SWEEP_DIR = DR / "sweep_ib"

def load_run(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text())
    except Exception as e:
        print(f"# skip {p.name}: {e}", file=sys.stderr)
        return None
    s = d.get("summary", {})
    cfg = d.get("config_resolved", {})
    caps = d.get("caps_applied", {})
    per_sec = {x["sec_type"]: x for x in d.get("per_sec_type", []) if x.get("n")}
    return {
        "file": p.name,
        "atr": cfg.get("atr_regime_threshold"),
        "vix": "ON" if cfg.get("vix_sizing_enabled") else "off",
        "vix_high": cfg.get("vix_high_threshold"),
        "vix_sh": cfg.get("vix_size_high"),
        "buf": cfg.get("struct_buffer_pct"),
        "rr": cfg.get("min_rr"),
        "raw": caps.get("raw_signals", 0),
        "addons": caps.get("pyramid_addons", 0),
        "rej_mm": caps.get("rejected_mm_cap", 0),
        "n": s.get("trades", 0),
        "net_R": s.get("net_R", 0),
        "avgR": s.get("net_R", 0) / max(s.get("trades", 1), 1),
        "win": s.get("win_rate", 0),
        "dd": s.get("max_dd_pct", 0),
        "mm_avg": s.get("mm_avg_pct", 0),
        "mm_max": s.get("mm_max_pct", 0),
        "cagr": s.get("cagr_pct", 0),
        "equity_x": s.get("final_equity_usd", 0) / cfg.get("account_starting_usd", 1),
        "stk": per_sec.get("stock", {}),
        "fut": per_sec.get("future", {}),
        "fx":  per_sec.get("fx", {}),
        "window_days": d.get("history_window", {}).get("days", 0),
    }

runs = []
b = DR / "ib_baseline.json"
if b.exists():
    r = load_run(b); r["label"] = "BASELINE (prod env)"; runs.append(r)

for p in sorted(SWEEP_DIR.glob("*.json")):
    if p.name.startswith("_"):
        continue
    r = load_run(p)
    if r:
        r["label"] = p.stem
        runs.append(r)

print(f"\n{len(runs)} runs loaded\n")

# Wide table
hdr = ("LABEL", "atr", "buf", "rr", "vix", "raw", "n", "avgR", "net_R",
       "MM_avg", "MM_max", "DD%", "win%", "stkN/avgR", "futN/avgR", "fxN/avgR", "equity×")
def fmt(r):
    def secstr(x):
        if not x: return "0/-"
        return f"{x.get('n',0)}/{x.get('avg_R',0):+.2f}"
    return (
        r["label"][:36],
        f"{r['atr']:.2f}" if r['atr'] is not None else "?",
        f"{r['buf']:.3f}" if r['buf'] is not None else "?",
        f"{r['rr']:.2f}" if r['rr'] is not None else "?",
        f"{r['vix']}"+(f":{r.get('vix_sh','?')}" if r['vix']=='ON' else ""),
        str(r["raw"]),
        str(r["n"]),
        f"{r['avgR']:+.3f}",
        f"{r['net_R']:+.1f}",
        f"{r['mm_avg']:.1f}",
        f"{r['mm_max']:.1f}",
        f"{r['dd']:.1f}",
        f"{r['win']:.0f}",
        secstr(r["stk"]),
        secstr(r["fut"]),
        secstr(r["fx"]),
        f"{r['equity_x']:.2f}",
    )

rows = [hdr] + [fmt(r) for r in runs]
widths = [max(len(str(row[i])) for row in rows) for i in range(len(hdr))]
def line(row): return "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row))
print(line(rows[0]))
print("  ".join("-" * w for w in widths))
for row in rows[1:]:
    print(line(row))

# Rankings
ok = [r for r in runs if r["n"] > 0]
print("\n=== TOP-5 by net_R ===")
top = sorted(ok, key=lambda r: -r["net_R"])[:5]
for r in top:
    print(f"  {r['label'][:50]:50s}  n={r['n']:4d}  avgR={r['avgR']:+.3f}  netR={r['net_R']:+.1f}  MM={r['mm_avg']:.1f}/{r['mm_max']:.1f}%  DD={r['dd']:.1f}%")

print("\n=== Combos with MM_avg in [30,40] ===")
band = [r for r in ok if 30 <= r["mm_avg"] <= 40]
band.sort(key=lambda r: -r["net_R"])
if not band:
    closest = sorted(ok, key=lambda r: abs(r["mm_avg"] - 35))[:3]
    print(f"  none yet — 3 closest to 35% (target window centre):")
    for r in closest:
        print(f"    {r['label'][:50]:50s}  MM_avg={r['mm_avg']:.1f}  avgR={r['avgR']:+.3f}  netR={r['net_R']:+.1f}")
else:
    for r in band:
        print(f"  {r['label'][:50]:50s}  n={r['n']:4d}  avgR={r['avgR']:+.3f}  netR={r['net_R']:+.1f}  MM={r['mm_avg']:.1f}/{r['mm_max']:.1f}%")
