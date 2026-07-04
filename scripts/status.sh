#!/bin/bash
# Quick all-bots status dashboard. Run from Mac:
#   bash scripts/status.sh

echo "=================================================================="
echo "  Bot Status Dashboard — $(date -u +'%Y-%m-%d %H:%M UTC')"
echo "=================================================================="
echo ""

# HL
echo "─── HL (hl-bot Tokyo, $(ssh -o ConnectTimeout=5 hl-bot 'hostname' 2>/dev/null)) ───"
hl_status=$(ssh -o ConnectTimeout=5 hl-bot "sudo systemctl is-active hyperliquid-bot" 2>/dev/null)
echo "  Service: $hl_status"
ssh -o ConnectTimeout=5 hl-bot "sudo /root/hyperliquid_bot/venv/bin/python3 /tmp/hl_pos_summary.py 2>&1 | grep -vE 'pkg_resources|UserWarning|Warning' | head -20" 2>/dev/null
echo ""

# Snel — 3 bots
echo "─── Snel VPS (<HOST>) ───"
ssh -o ConnectTimeout=5 root@<HOST> "
for s in kraken-bot nado-bot; do
  status=\$(systemctl is-active \$s)
  echo \"  \$s: \$status\"
done
echo ''
echo '  Equity per bot:'
for botdir in hyperliquid_bot nado_bot; do
  bot_label=\$(echo \$botdir | tr '_' '-')
  cd /root/\$botdir
  /root/\$botdir/venv/bin/python3 -c '
import warnings; warnings.filterwarnings(\"ignore\")
import sys; sys.path.insert(0, \".\")
from dotenv import load_dotenv; load_dotenv(\".env\")
from bot.config import Settings
from bot.exchange_factory import get_exchange_client
try:
    c = get_exchange_client(Settings.from_env())
    av = c.account_value()
    pos = c.open_positions()
    print(f\"    \$bot_label: \${av:.2f} ({len(pos)} positions)\")
except Exception as e:
    print(f\"    \$bot_label: ERR {e}\")
' 2>/dev/null
done
echo ''
echo '  fail2ban status:'
fail2ban-client status sshd 2>/dev/null | grep -E 'Currently banned|Total banned' | sed 's/^/    /'"

echo ""
echo "─── Recent git commits ───"
cd /Users/ak/Desktop/HL/hyperliquid_bot
git log --oneline -5

echo ""
echo "─── Telegram heartbeat ───"
echo "  Last sent at top of each hour (silent-when-OK design)"
echo "  Daily ping at 8:00 UTC"
echo ""
