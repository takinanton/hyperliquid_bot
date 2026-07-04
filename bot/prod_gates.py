"""Canonical registry of every gate / filter / guard that prod applies.

WHY:
    Backtest harnesses (honest_replay_hl.py, honest_replay_kf.py, scripts/*sweep*.py,
    bot/backtest.py) repeatedly diverge from prod by silently dropping a gate.
    The recurring incident pattern: "backtest looked great → задеплоили → prod хуже →
    оказалось NOT applied X gate". Single source of truth fixes that.

CONTRACT:
    1. Every new prod gate gets a Gate(...) entry in PROD_GATES.
    2. Every backtest harness declares its applied gate ids and calls
       assert_backtest_parity(applied) BEFORE producing results.
    3. Removing a gate from prod means removing it from this registry
       (after grepping that no backtest still references the id).

Fast invocations:
    python -m bot.prod_gates              # markdown dump for docs
    python -m bot.prod_gates --check      # drift check: every env in registry
                                          # must actually be read in bot/*.py
    python -m bot.prod_gates --diff hl    # which gates honest_replay_hl misses
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

Stage = Literal["pre_entry", "entry", "management"]

ALL_STAGES: tuple[Stage, ...] = ("pre_entry", "entry", "management")
SIGNAL_STAGES: tuple[Stage, ...] = ALL_STAGES


@dataclass(frozen=True)
class Gate:
    id: str
    description: str
    stage: Stage
    location: str                      # "bot/trader.py" — file path, no line (rots)
    config_keys: tuple[str, ...] = ()  # env vars / Settings fields that drive it
    notes: str = ""                    # edge cases worth knowing in backtests


# -------------------------------------------------------------------------
# REGISTRY — keep alphabetised within each stage for easy diffing.
# -------------------------------------------------------------------------
PROD_GATES: tuple[Gate, ...] = (
    # ---------- pre_entry ------------------------------------------------
    Gate(
        id="adaptive_rr_by_leverage",
        description="R/R floor adapts to LOW/MID/HIGH leverage tier.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("LOW_LEV_THRESHOLD", "MID_LEV_THRESHOLD",
                     "min_rr", "min_rr_countertrend"),
        notes="Asset metadata (max_leverage) fetched from exchange — backtest must "
              "carry leverage per coin or use a fixed default.",
    ),
    Gate(
        id="already_traded_dedup",
        description="Skip if same (coin, pattern, tf, detected_at) already executed.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=(),
        notes="Backtest equivalent: single-coin lock.",
    ),
    Gate(
        id="btc_regime_filter",
        description="Block shorts when BTC 4h regime is bull (and inverse).",
        stage="pre_entry",
        location="bot/main.py",
        config_keys=("BTC_REGIME_TICKER",),
        notes="Empty BTC_REGIME_TICKER = filter disabled. Backtest must load BTC "
              "candles separately.",
    ),
    Gate(
        id="cascade_filter",
        description="Block new entries if N losses occurred within window.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("CASCADE_WINDOW_MIN", "CASCADE_THRESHOLD"),
        notes="Reads recent_losses_count from journal — backtest needs simulated "
              "trade-history index.",
    ),
    Gate(
        id="cold_combo_cap",
        description="Reduce risk for (pattern, coin) combos with <N backtest trades.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("KF_COLD_COMBO_MIN_N", "KF_COLD_COMBO_MULT"),
        notes="Loads data/combo_counts.json (per-exchange). Only meaningful if "
              "the backtest ALSO produces such a count file — otherwise fixed mult.",
    ),
    Gate(
        id="force_long_short_coins",
        description="Per-coin direction bias — drop opposite-direction signals.",
        stage="pre_entry",
        location="bot/main.py",
        config_keys=("FORCE_LONG_COINS", "FORCE_SHORT_COINS"),
    ),
    Gate(
        id="force_trend_override",
        description="Override pattern direction by FORCE_TREND.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("FORCE_TREND",),
        notes="Manual operator override; usually empty in normal backtests.",
    ),
    Gate(
        id="funding_blocked",
        description="Block trade if predicted funding rate is too adverse.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("funding_block_threshold",),
        notes="Backtest must carry hourly funding series per coin — most don't.",
    ),
    Gate(
        id="hip3_market_hours",
        description="HIP-3 (xyz:*) instruments only trade during US market hours.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("HIP3_MARKET_HOURS_ONLY",),
        notes="Off by default. Critical when scanning xyz:TSLA / xyz:AAPL etc.",
    ),
    Gate(
        id="liquidity_tier_mult",
        description="Risk multiplier by 1h liquidity ratio (BTC/ETH 2×, thin 0.5×).",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("MIN_1H_LIQUIDITY_RATIO", "MIN_1H_LIQUIDITY_RATIO_HIP3"),
        notes="Requires a per-coin liquidity probe — backtest needs precomputed "
              "liquidity table or stub.",
    ),
    Gate(
        id="liquidity_cap_notional",
        description="Adaptive sizing cap: notional <= 1h_vol_est / MIN_1H_LIQUIDITY_RATIO. "
                    "Trade size shrinks (and actual risk_$ with it) on thin pairs.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("MIN_1H_LIQUIDITY_RATIO", "MIN_1H_LIQUIDITY_RATIO_HIP3"),
        notes="Distinct from liquidity_tier_mult (which boosts risk on liquid pairs). "
              "Backtests must scale $-PnL / notional / margin by cap_ratio when cap binds; "
              "avgR / fee_R remain invariant (cap shrinks denominator and numerator proportionally).",
    ),
    Gate(
        id="max_opens_per_day_cycle",
        description="Cap how many new entries can open per day / per cycle.",
        stage="pre_entry",
        location="bot/main.py",
        config_keys=("MAX_OPENS_PER_DAY", "MAX_OPENS_PER_CYCLE"),
        notes="Both default to 999 (effectively off). Apply only if env tightens them.",
    ),
    Gate(
        id="min_rr",
        description="Base R/R floor (long/short, with countertrend variant).",
        stage="pre_entry",
        location="bot/risk.py",
        config_keys=("min_rr", "min_rr_countertrend"),
    ),
    Gate(
        id="open_trade_per_coin",
        description="Skip if coin already has an open position in DB.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("PYRAMID_ENABLED",),
        notes="Single-coin lock = backtest's open_until_idx. RELAXED when "
              "PYRAMID_ENABLED=true — replaced by pyramid_addon_trigger gate.",
    ),
    Gate(
        id="pyramid_addon_trigger",
        description="Allow same-(coin,tf,dir) add-on if unrealized_R >= "
                    "PYRAMID_TRIGGER_R, levels < MAX_PYRAMID_LEVELS, pattern "
                    "not in PYRAMID_EXCLUDED_PATTERNS. Active only with "
                    "PYRAMID_ENABLED=true.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("PYRAMID_ENABLED", "PYRAMID_TRIGGER_R",
                     "MAX_PYRAMID_LEVELS", "PYRAMID_EXCLUDED_PATTERNS"),
        notes="Backtest 2026-05-09 (HL+KF): +20% net_R / +50-65% max_DD relative. "
              "Per-level flat 1% risk; each level keeps own SL via vstop trail.",
    ),
    Gate(
        id="dd_halt",
        description="Block NEW entries when equity_dd >= MAX_DRAWDOWN_PCT_HALT. "
                    "Trail SL of open positions still runs.",
        stage="pre_entry",
        location="bot/main.py",
        config_keys=("MAX_DRAWDOWN_PCT_HALT",),
        notes="Disabled by default (halt_pct=0). Re-introduced 2026-05-09 as "
              "safety brake under PYRAMID_ENABLED — backtest showed DD 1.5-1.65× wider.",
    ),
    Gate(
        id="pattern_boost_mult",
        description="Per-pattern manual risk multiplier (env-driven).",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("PATTERN_BOOST_MULT",),
        notes="Empty by default; only fires when operator pins specific patterns.",
    ),
    Gate(
        id="pattern_min_rr",
        description="Per-pattern R/R floor override (PATTERN_MIN_RR map).",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("PATTERN_MIN_RR",),
    ),
    Gate(
        id="signal_drift_freshness",
        description="Reject if current mark drifted >X% from signal.entry (stale).",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("SIGNAL_MAX_DRIFT_PCT",),
        notes="Default 1.0%. Catches outage/restart-gap scenarios where the "
              "candle close that triggered the signal is no longer the mark. "
              "Trivial in backtest if entry == close at firing bar.",
    ),
    Gate(
        id="hip3_min_sl_distance",
        description="HIP-3 only: skip if SL distance < HIP3_MIN_SL_PCT.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("HIP3_MIN_SL_PCT",),
        notes="Default 1.5%. Added 2026-05-06 — instant-SL incidents on xyz:* "
              "after thin-hours swings produced 0.3–0.9% SLs.",
    ),
    Gate(
        id="liquidity_drag_check",
        description="Skip if effective_RR (rr − drag_R from fee+slip) < min_rr.",
        stage="pre_entry",
        location="bot/trader.py",
        config_keys=("min_rr",),
        notes="No dedicated env — derived from per-coin slip + fee_rt against "
              "stop_pct. Backtest must compute drag the same way to match.",
    ),
    Gate(
        id="whitelist_coins",
        description="Universe = COINS env (resolved via coin_filter).",
        stage="pre_entry",
        location="bot/main.py",
        config_keys=("COINS", "COIN_FILTER"),
    ),
    # ---------- entry ----------------------------------------------------
    Gate(
        id="max_margin_used_pct",
        description="Pre-trade maintenance-margin cap.",
        stage="entry",
        location="bot/trader.py",
        config_keys=("MAX_MM_PCT", "MAX_MARGIN_USED_PCT"),
        notes="WARNING: name-drift. main.py reads MAX_MM_PCT, trader.py:396 still "
              "reads MAX_MARGIN_USED_PCT. Per commit b37b8a5 the .env was renamed "
              "to MAX_MM_PCT — trader.py callsite is now stale and falls back to "
              "default 0.50 unless both keys are present. Track in trade audit.",
    ),
    Gate(
        id="min_notional",
        description="Exchange minimum order size (per coin).",
        stage="entry",
        location="bot/trader.py",
        config_keys=(),
        notes="Backtest can ignore for liquid coins; matters for HIP-3 / Nado micros.",
    ),
    Gate(
        id="slippage_and_fees",
        description="Real-fee + per-coin slippage applied per side.",
        stage="entry",
        location="bot/exchange.py",
        config_keys=("slippage",),
        notes="Backtest reads data/{exchange}_slippage.json; MARGIN ≥ 1.5× per "
              "user policy (memory: feedback_fees_slippage_in_backtests).",
    ),
    # ---------- management -----------------------------------------------
    Gate(
        id="liq_vs_sl_buffer",
        description="Auto-switch isolated→cross if liq distance < K×SL distance.",
        stage="management",
        location="bot/trader.py",
        config_keys=("LIQ_SL_BUFFER",),
        notes="Active on HL+Nado, NOT on Kraken (memory: project_liq_vs_sl_guard). "
              "Never auto-closes.",
    ),
    Gate(
        id="vstop_trailing",
        description="Volatility-based trailing stop (struct or ATR).",
        stage="management",
        location="bot/trader.py",
        config_keys=("exit_mode", "struct_buffer_pct", "VSTOP_ATR_MULT"),
        notes="VSTOP_ATR_MULT read via os.environ in trader.py (not a Settings field).",
    ),
)


# Build a fast lookup once.
_BY_ID: dict[str, Gate] = {g.id: g for g in PROD_GATES}


def gate(id: str) -> Gate:
    return _BY_ID[id]


def gate_ids(stages: Iterable[Stage] = ALL_STAGES) -> set[str]:
    s = set(stages)
    return {g.id for g in PROD_GATES if g.stage in s}


# -------------------------------------------------------------------------
# Parity assertion — entry point for backtest harnesses.
# -------------------------------------------------------------------------
class ParityError(AssertionError):
    """Raised when a backtest harness diverges from the prod gate registry."""


def assert_backtest_parity(
    applied: Iterable[str],
    *,
    stages: Iterable[Stage] = SIGNAL_STAGES,
    exempt: Iterable[str] = (),
    strict: bool = True,
) -> dict:
    """Verify the backtest applies every prod gate (for the given stages).

    Args:
        applied: gate ids the harness has applied. Must match PROD_GATES ids.
        stages: which stages to enforce. Default = all signal-relevant stages.
        exempt: ids the harness EXPLICITLY waives, with a reason logged elsewhere.
                Use sparingly — every exempt is a lie about "prod-fidelity".
        strict: if True, raise ParityError on any divergence. If False, return
                report and let caller decide (useful for early dev).

    Returns:
        dict with keys: missing, extra, exempt, applied, expected.
    """
    applied_set = set(applied)
    exempt_set = set(exempt)
    expected = gate_ids(stages)

    unknown_applied = applied_set - {g.id for g in PROD_GATES}
    unknown_exempt = exempt_set - {g.id for g in PROD_GATES}
    missing = expected - applied_set - exempt_set

    report = {
        "expected": sorted(expected),
        "applied": sorted(applied_set),
        "exempt": sorted(exempt_set),
        "missing": sorted(missing),
        "unknown_applied": sorted(unknown_applied),
        "unknown_exempt": sorted(unknown_exempt),
    }

    if not (missing or unknown_applied or unknown_exempt):
        return report

    lines = ["[prod_gates] PARITY DIVERGENCE"]
    if missing:
        lines.append("  MISSING from backtest (prod applies, you don't):")
        for gid in sorted(missing):
            g = _BY_ID[gid]
            keys = ", ".join(g.config_keys) or "—"
            lines.append(f"    - {gid:<35s} [{g.stage}]  {g.location}  ({keys})")
            lines.append(f"        {g.description}")
            if g.notes:
                lines.append(f"        note: {g.notes}")
    if unknown_applied:
        lines.append("  UNKNOWN applied ids (typo or unregistered gate):")
        for gid in sorted(unknown_applied):
            lines.append(f"    - {gid}")
    if unknown_exempt:
        lines.append("  UNKNOWN exempt ids:")
        for gid in sorted(unknown_exempt):
            lines.append(f"    - {gid}")

    msg = "\n".join(lines)
    if strict:
        raise ParityError(msg)
    import logging
    logging.getLogger(__name__).warning(msg)
    return report


# -------------------------------------------------------------------------
# Markdown dump (for docs/CLAUDE.md/README).
# -------------------------------------------------------------------------
def dump_markdown(stages: Iterable[Stage] = ALL_STAGES) -> str:
    lines = ["| ID | Stage | Description | Location | Config |",
             "|----|-------|-------------|----------|--------|"]
    for g in sorted(PROD_GATES, key=lambda x: (x.stage, x.id)):
        if g.stage not in set(stages):
            continue
        cfg = ", ".join(f"`{k}`" for k in g.config_keys) if g.config_keys else "—"
        lines.append(
            f"| `{g.id}` | {g.stage} | {g.description} | `{g.location}` | {cfg} |"
        )
    return "\n".join(lines)


# -------------------------------------------------------------------------
# Drift check — every config_key in the registry should actually be read
# somewhere under bot/*.py. Catches "I removed the env from code but forgot
# to delete it from .env" (memory: feedback_config_vs_code_drift).
# -------------------------------------------------------------------------
def find_drift(bot_dir: str = "bot") -> dict:
    """Return {gate_id: [unread keys]} for any config_key not grep-able under bot/."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent
    bot_root = root if root.name == "bot" else (root / bot_dir)
    src = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in bot_root.glob("*.py")
                    if p.name != "prod_gates.py")
    out: dict[str, list[str]] = {}
    for g in PROD_GATES:
        unread = []
        for key in g.config_keys:
            # Match either `os.getenv("KEY"`, `os.environ["KEY"]`, or attribute
            # `settings.key`/`KEY` as bare reference.
            patterns = [
                rf'os\.getenv\(\s*["\']{re.escape(key)}["\']',
                rf'os\.environ\[\s*["\']{re.escape(key)}["\']',
                rf'settings\.{re.escape(key)}\b',
                rf'\b{re.escape(key)}\b',
            ]
            if not any(re.search(p, src) for p in patterns):
                unread.append(key)
        if unread:
            out[g.id] = unread
    return out


def find_deprecated_loaders() -> dict:
    """Grep honest_replay scripts for load_4h imports / calls.

    History: 2026-05-06 load_4h() marked deprecated with stderr warning, but
    on 2026-05-07 honest_replay_hl/kf were written re-importing it. Warning
    wasn't enough — we now scan the harness sources at --check time so any
    third regression fails CI/manual run BEFORE wasting a 30-min replay.

    Returns {file_path: [line_no:line]} for every match in:
        scripts/honest_*.py            (root-level harnesses)
        hyperliquid_bot/scripts/honest_*.py
    Empty dict = clean.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]  # repo root
    candidates: list[pathlib.Path] = []
    for sub in ("scripts", "hyperliquid_bot/scripts"):
        d = root / sub
        if d.is_dir():
            candidates.extend(d.glob("honest_*.py"))

    pat = re.compile(r"\bload_4h\b")
    out: dict[str, list[str]] = {}
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        hits: list[str] = []
        for n, line in enumerate(text.splitlines(), start=1):
            if pat.search(line) and not line.lstrip().startswith("#"):
                hits.append(f"{n}: {line.rstrip()}")
        if hits:
            out[str(p.relative_to(root))] = hits
    return out


# -------------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------------
def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="Drift check: every config_key must be read somewhere in bot/.")
    ap.add_argument("--ids", action="store_true",
                    help="Print one gate id per line.")
    args = ap.parse_args()

    if args.ids:
        for g in sorted(PROD_GATES, key=lambda x: (x.stage, x.id)):
            print(g.id)
        return

    if args.check:
        problems = 0
        drift = find_drift()
        if drift:
            problems += 1
            print("[prod_gates] DRIFT — registered keys not read in bot/*.py:")
            for gid, keys in drift.items():
                print(f"  {gid}: {keys}")
        else:
            print("[prod_gates] OK — every registered config_key is read in bot/.")

        deprecated = find_deprecated_loaders()
        if deprecated:
            problems += 1
            print("[prod_gates] DEPRECATED LOADER — load_4h() referenced by:")
            for f, hits in deprecated.items():
                print(f"  {f}:")
                for h in hits:
                    print(f"    {h}")
            print("  Replace with load_prod() or load_at_tf(path, '4h', '1d').")
            print("  See memory: feedback_backtest_tf_must_match_prod.")
        else:
            print("[prod_gates] OK — no honest_replay_*.py imports load_4h.")

        if problems:
            raise SystemExit(2)
        return

    print(dump_markdown())


if __name__ == "__main__":
    _main()
