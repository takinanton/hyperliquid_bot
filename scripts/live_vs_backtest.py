"""Live performance tracker — compares live R-per-trade with backtest baseline.

Запускается раз в день (cron 23:00 UTC). Берёт closed trades за последние 24h,
вычисляет live avgR per trade и сравнивает с backtest baseline +0.93R.

Шлёт в Telegram:
- Live avgR vs backtest gap %
- Trade count, WR
- Top winners/losers
- Alert если gap > 50% (overfit warning)

Также log-ает в data/live_vs_backtest.log для quarterly review.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.journal import _conn
from bot.notifier import Notifier

# Backtest baselines (from CLAUDE.md, validated 03-05-2026)
BASELINE_AVG_R = 0.93  # avg R per trade with current config
BASELINE_WR = 0.46     # 46% win rate

# Threshold for "live looks healthy" — within 50% of backtest
HEALTH_THRESHOLD_PCT = 50.0  # if live < 50% of backtest, flag overfit

LOG_FILE = Path(__file__).resolve().parent.parent / "data" / "live_vs_backtest.log"
LOG_FILE.parent.mkdir(exist_ok=True)


def fetch_closed_24h() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cutoff_iso = cutoff.isoformat()
    with _conn() as con:
        rows = con.execute(
            """SELECT id, coin, pattern, direction, entry, stop_loss, size,
                      risk_dollars, pnl_dollars, pnl_pct, status, notes,
                      detected_at, created_at, closed_at
               FROM trades
               WHERE status IN ('closed_vstop', 'closed_no_sl', 'closed_manual')
                 AND COALESCE(closed_at, created_at) >= ?
               ORDER BY COALESCE(closed_at, created_at) DESC""",
            (cutoff_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def compute_R(trade: dict) -> float | None:
    """P&L as multiple of planned risk (1R)."""
    risk_dollars = trade.get("risk_dollars")
    pnl = trade.get("pnl_dollars")
    if risk_dollars is None or pnl is None or risk_dollars == 0:
        return None
    try:
        return float(pnl) / float(risk_dollars)
    except (TypeError, ValueError):
        return None


def main():
    trades = fetch_closed_24h()
    n = len(trades)

    if n == 0:
        msg = "📊 Daily live tracker: 0 closed trades за 24h."
        print(msg)
        return 0

    # Compute R for each
    R_values = []
    pnl_dollars_total = 0.0
    wins = 0
    by_coin = {}
    for t in trades:
        R = compute_R(t)
        if R is None:
            continue
        R_values.append(R)
        pnl = float(t.get("pnl_dollars") or 0)
        pnl_dollars_total += pnl
        if pnl > 0:
            wins += 1
        coin = t["coin"]
        by_coin[coin] = by_coin.get(coin, 0) + pnl

    if not R_values:
        msg = f"📊 Daily live tracker: {n} trades но нет risk_dollars/pnl. Проверь journaling."
        print(msg)
        return 0

    n_with_R = len(R_values)
    live_avg_R = sum(R_values) / n_with_R
    live_wr = wins / n_with_R * 100

    # Compare to baseline
    gap_R = live_avg_R - BASELINE_AVG_R
    gap_pct = (live_avg_R / BASELINE_AVG_R * 100) if BASELINE_AVG_R else 0
    health = "✅" if gap_pct >= HEALTH_THRESHOLD_PCT else "⚠️"

    # Top movers
    sorted_coins = sorted(by_coin.items(), key=lambda x: -x[1])
    top_3 = sorted_coins[:3]
    bot_3 = sorted_coins[-3:] if len(sorted_coins) > 3 else []

    body = f"""📊 Live vs Backtest Tracker (24h)

Trades closed:     {n_with_R}
Live avg R:        {live_avg_R:+.3f}
Backtest baseline: +{BASELINE_AVG_R:.2f}
Gap:               {gap_R:+.3f}R ({gap_pct:.0f}% of backtest) {health}

Live WR:           {live_wr:.0f}%
Backtest WR:       {BASELINE_WR*100:.0f}%

Realized P&L:      ${pnl_dollars_total:+.2f}

Top winners:
{chr(10).join(f'  {c}: ${p:+.0f}' for c, p in top_3)}

Top losers:
{chr(10).join(f'  {c}: ${p:+.0f}' for c, p in bot_3)}

Status: {'HEALTHY — live ≈ backtest' if gap_pct >= 80 else 'WATCH — gap noticeable' if gap_pct >= HEALTH_THRESHOLD_PCT else 'ALERT — possible overfit / regime change'}
"""

    print(body)

    # Log to file
    with open(LOG_FILE, "a") as f:
        ts = datetime.now(timezone.utc).isoformat()
        f.write(f"{ts}\tn={n_with_R}\tavgR={live_avg_R:+.3f}\tgap_pct={gap_pct:.0f}\tWR={live_wr:.0f}\tpnl=${pnl_dollars_total:+.2f}\n")

    # Send to Telegram
    try:
        n = Notifier()
        if n.enabled:
            ok = n.daily_summary(body)
            print(f"Telegram sent: {ok}")
    except Exception as e:
        print(f"Telegram failed: {e}")


if __name__ == "__main__":
    main()
