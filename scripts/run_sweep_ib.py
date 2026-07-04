"""IB strategy sweep — runs honest_replay_ib.py for each combo of
ATR_REGIME × VIX_mode × STRUCT_BUFFER × MIN_RR, captures summary,
prints top combos by net_R AND combos with avg MM ∈ [30, 40].

Each child run shells out so env overrides take effect cleanly.
JSON summaries land in data/honest_replay/sweep_ib/<idx>_<combo>.json.

Run on komp:
  cd C:\\Users\\user\\Desktop\\HL\\hyperliquid_bot
  python scripts\\run_sweep_ib.py

Env:
  IB_WORKERS (per-replay multiproc, default 8)
  SMOKE_COINS (if set, smoke mode — small grid + few coins)
  BT_RISK (default 0.01), ACCOUNT_USD (default 240000), MAX_MARGIN_USED_PCT (default 0.50)
  IB_START_TS / IB_END_TS — window for sweep aggregation (e.g. 2025-05-12 / 2026-05-12 = 1y)
"""
from __future__ import annotations
import os, sys, json, time, subprocess, itertools
from pathlib import Path
# v4 adds MM_CAP as a swept axis — strictness alone proven insufficient
# (v1/v2 all sat at 44-45% MM_avg). Mechanical cap path verified separately.

HL_ROOT = Path(os.environ.get("HL_ROOT", str(Path(__file__).parent.parent.parent)))
PY = sys.executable
REPLAY = HL_ROOT / "hyperliquid_bot" / "scripts" / "honest_replay_ib.py"
OUT_DIR = HL_ROOT / "hyperliquid_bot" / "data" / "honest_replay" / "sweep_ib"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- GRID ----
SMOKE = bool(os.environ.get("SMOKE_COINS"))
if SMOKE:
    GRID_ATR = ["0.6", "0.7", "0.8"]
    GRID_VIX = [
        ("off", "false", "25", "14", "0.5", "1.5"),
        ("default", "true", "25", "14", "0.5", "1.5"),
    ]
    GRID_BUF = ["0.003", "0.005"]
    GRID_RR = ["1.3"]
else:
    # GRID v9 — fine grid in sweet spot. v8 showed:
    #   baseline (ATR=0.7, RR=1.3) -> MM 44.9, avgR 0.502
    #   v8 c0   (ATR=1.5, RR=2.5) -> MM  6.2, avgR 0.839
    #   v8 c4   (ATR=2.0, RR=3.0) -> 0 trades
    #   VIX off == VIX default (VIX 14-25 all period, never triggered)
    # Target MM=35 lies between baseline and v8 c0 — fine-scan that zone.
    GRID_ATR_RR = [
        ("0.8", "1.5"), ("0.9", "1.7"), ("1.0", "2.0"),
        ("1.1", "2.2"), ("1.3", "2.5"),
    ]
    GRID_VIX = [
        ("off", "false", "25", "14", "1.0", "1.0"),
    ]
    GRID_BUF = ["0.005"]
    GRID_MM_CAP = ["0.50"]
    GRID_MIN_SL_DIST = ["0.005"]

# Carry through shared env (account, risk, mm cap, window)
BASE_ENV = {
    "ACCOUNT_USD": os.environ.get("ACCOUNT_USD", "240000"),
    "BT_RISK": os.environ.get("BT_RISK", "0.01"),
    "MAX_MARGIN_USED_PCT": os.environ.get("MAX_MARGIN_USED_PCT", "0.50"),
    "IB_WORKERS": os.environ.get("IB_WORKERS", "8"),
    "MIN_SL_DIST_PCT": os.environ.get("MIN_SL_DIST_PCT", "0.005"),
    "IB_ENV_FILE": os.environ.get(
        "IB_ENV_FILE",
        str(HL_ROOT / "hyperliquid_bot" / "data" / "ib_live.env"),
    ),
    # IB env loading: we still call live_env first but override above keys
    "IB_DATA_DIR": os.environ.get(
        "IB_DATA_DIR", str(HL_ROOT / "hyperliquid_bot" / "data" / "yf_cache_ib")
    ),
    # Live bot/ source: prefer LIVE_BOT_LOCAL_DIR (no ssh on komp); harness in
    # honest_replay_ib.py calls activate_live_bot('ib') which reads this env.
    "LIVE_BOT_LOCAL_DIR": os.environ.get(
        "LIVE_BOT_LOCAL_DIR", str(HL_ROOT / "hyperliquid_bot")
    ),
    # Disable liquidity-tier risk boost: user wants FLAT risk
    "LIQUIDITY_RISK_MULT_HIGH": "1.0",
    "LIQUIDITY_RISK_MULT_MID": "1.0",
    "KF_COLD_COMBO_MIN_N": "0",
    # Encoding hint for Windows console (unicode chars otherwise crash cp1252)
    "PYTHONIOENCODING": "utf-8",
}
if SMOKE:
    BASE_ENV["SMOKE_COINS"] = os.environ["SMOKE_COINS"]
for k in ("IB_START_TS", "IB_END_TS"):
    if os.environ.get(k):
        BASE_ENV[k] = os.environ[k]


def run_one(idx: int, atr: str, vix: tuple, buf: str, rr: str, mm_cap: str = "0.50",
            min_sl_dist: str = "0.005") -> dict:
    name = f"atr{atr}_rr{rr}_sl{min_sl_dist}_vix-{vix[0]}_mm{mm_cap}"
    out_path = OUT_DIR / f"{idx:03d}_{name}.json"
    env = os.environ.copy()
    env.update(BASE_ENV)
    env["ATR_REGIME_THRESHOLD"] = atr
    env["VIX_SIZING_ENABLED"] = vix[1]
    env["VIX_HIGH_THRESHOLD"] = vix[2]
    env["VIX_LOW_THRESHOLD"] = vix[3]
    env["VIX_SIZE_HIGH"] = vix[4]
    env["VIX_SIZE_LOW"] = vix[5]
    env["STRUCT_BUFFER_PCT"] = buf
    env["MIN_RR"] = rr
    env["MAX_MARGIN_USED_PCT"] = mm_cap
    env["MIN_SL_DIST_PCT"] = min_sl_dist
    env["IB_OUT_PATH"] = str(out_path)

    t0 = time.time()
    print(f"[{idx:03d}] {name}  starting...", flush=True)
    proc = subprocess.run(
        [PY, str(REPLAY)],
        env=env, capture_output=True, text=True, timeout=3600,
    )
    elapsed = time.time() - t0
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-400:]
        print(f"[{idx:03d}] FAILED rc={proc.returncode} elapsed={elapsed:.0f}s  tail: {tail}", flush=True)
        return {"idx": idx, "combo": name, "status": "fail", "rc": proc.returncode,
                "elapsed_sec": elapsed, "err_tail": tail}
    if not out_path.exists():
        return {"idx": idx, "combo": name, "status": "no_output", "elapsed_sec": elapsed}
    try:
        data = json.loads(out_path.read_text())
    except Exception as e:
        return {"idx": idx, "combo": name, "status": "bad_json", "err": str(e)}
    summ = data.get("summary", {})
    print(f"[{idx:03d}] {name}  trades={summ.get('trades',0)}  "
          f"net_R={summ.get('net_R',0):+.1f}  avgR={(summ.get('net_R',0)/max(summ.get('trades',1),1)):+.3f}  "
          f"MM_avg={summ.get('mm_avg_pct',0):.1f}%  MM_max={summ.get('mm_max_pct',0):.1f}%  "
          f"DD={summ.get('max_dd_pct',0):.1f}%  win={summ.get('win_rate',0):.0f}%  "
          f"({elapsed:.0f}s)", flush=True)
    return {
        "idx": idx, "combo": name, "status": "ok",
        "atr_regime": atr, "vix_mode": vix[0], "struct_buffer": buf, "min_rr": rr,
        "elapsed_sec": round(elapsed, 1),
        **summ,
        "out_path": str(out_path),
    }


def main():
    # Build (atr, rr, vix, buf, mm_cap) tuples; smoke uses legacy GRID_ATR×GRID_RR,
    # full uses paired GRID_ATR_RR + GRID_MM_CAP.
    if SMOKE:
        grid = [
            (atr, rr, vix, buf, "0.50", "0.005")
            for atr in GRID_ATR for rr in GRID_RR
            for vix in GRID_VIX for buf in GRID_BUF
        ]
    else:
        grid = [
            (atr, rr, vix, buf, mm, sl)
            for (atr, rr) in GRID_ATR_RR
            for vix in GRID_VIX for buf in GRID_BUF
            for mm in GRID_MM_CAP
            for sl in GRID_MIN_SL_DIST
        ]
    print(f"=== IB SWEEP: {len(grid)} combos ===  (smoke={SMOKE})", flush=True)
    t0 = time.time()
    results: list[dict] = []
    for idx, (atr, rr, vix, buf, mm_cap, sl) in enumerate(grid):
        results.append(run_one(idx, atr, vix, buf, rr, mm_cap, sl))
        eta = (time.time() - t0) / max(idx + 1, 1) * (len(grid) - idx - 1)
        if idx % 5 == 4:
            print(f"  ... {idx+1}/{len(grid)} done  ETA {eta/60:.1f}min", flush=True)

    # Persist full sweep summary
    sweep_out = OUT_DIR / "_sweep_summary.json"
    sweep_out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nSweep done in {(time.time()-t0)/60:.1f}min  → {sweep_out}", flush=True)

    # Rank
    ok = [r for r in results if r.get("status") == "ok"]
    if not ok:
        print("No successful runs.")
        return
    # Top by net_R
    by_netR = sorted(ok, key=lambda r: -r.get("net_R", -1e9))
    print("\n=== TOP-10 by net_R ===")
    print(f"{'idx':>4}  {'combo':<48}  {'trades':>6}  {'net_R':>7}  {'avgR':>7}  {'MM_avg':>6}  {'MM_max':>6}  {'DD':>5}  {'win':>4}")
    for r in by_netR[:10]:
        trades = r.get('trades', 0)
        avgR = r.get('net_R', 0) / max(trades, 1)
        print(f"{r['idx']:>4}  {r['combo']:<48}  {trades:>6}  {r.get('net_R',0):+7.1f}  {avgR:+7.3f}  "
              f"{r.get('mm_avg_pct',0):>5.1f}%  {r.get('mm_max_pct',0):>5.1f}%  {r.get('max_dd_pct',0):>4.1f}%  {r.get('win_rate',0):>3.0f}%")

    # Filter to MM_avg ∈ [30, 40]
    band = [r for r in ok if 30.0 <= r.get("mm_avg_pct", -1) <= 40.0]
    print(f"\n=== Combos with MM_avg ∈ [30%, 40%]: {len(band)} ===")
    band.sort(key=lambda r: -r.get("net_R", -1e9))
    print(f"{'idx':>4}  {'combo':<48}  {'trades':>6}  {'net_R':>7}  {'avgR':>7}  {'MM_avg':>6}  {'MM_max':>6}  {'DD':>5}  {'win':>4}")
    for r in band[:15]:
        trades = r.get('trades', 0)
        avgR = r.get('net_R', 0) / max(trades, 1)
        print(f"{r['idx']:>4}  {r['combo']:<48}  {trades:>6}  {r.get('net_R',0):+7.1f}  {avgR:+7.3f}  "
              f"{r.get('mm_avg_pct',0):>5.1f}%  {r.get('mm_max_pct',0):>5.1f}%  {r.get('max_dd_pct',0):>4.1f}%  {r.get('win_rate',0):>3.0f}%")


if __name__ == "__main__":
    main()
