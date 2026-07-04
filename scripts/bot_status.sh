#!/bin/bash
# Quick health check для hl-bot. Запускать на VPS или через ssh hl-bot.
# Usage:
#   bash scripts/bot_status.sh

set -uo pipefail
cd "$(dirname "$0")/.."
source venv/bin/activate

echo "=== systemd ==="
systemctl is-active hyperliquid-bot
systemctl is-enabled hyperliquid-bot

echo
echo "=== Last journalctl 5 lines ==="
journalctl -u hyperliquid-bot -n 5 --no-pager 2>/dev/null | tail -5

echo
echo "=== Open positions vs SL on exchange ==="
python3 check_sl.py 2>/dev/null || echo "(check_sl.py missing or python errored)"

echo
echo "=== Cron entries ==="
crontab -l 2>/dev/null

echo
echo "=== Watchdog last 3 lines ==="
tail -3 /var/log/hl-bot-watchdog.log 2>/dev/null || echo "(no watchdog log)"

echo
echo "=== Trade DB summary ==="
sqlite3 data/trades.db "
SELECT status, COUNT(*) as n, ROUND(SUM(pnl_dollars), 2) as total_pnl
FROM trades GROUP BY status;
"
echo
echo "=== Recent closed trades (last 5) ==="
sqlite3 data/trades.db "
SELECT id, coin, direction, ROUND(pnl_dollars, 2) as pnl, datetime(closed_at) as closed
FROM trades WHERE status LIKE 'closed%' ORDER BY closed_at DESC LIMIT 5;
"
