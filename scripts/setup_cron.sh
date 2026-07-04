#!/bin/bash
# Установить crontab на VPS под root.
# Запускать: sudo bash scripts/setup_cron.sh
#
# Crontab включает:
#   */5 * * * *   watchdog (проверка SL и восстановление)
#   0 23 * * *    daily summary в Telegram (Cat C digest)

set -uo pipefail
cd "$(dirname "$0")/.."

# Создать wrapper-скрипты если их нет
cat > scripts/run_watchdog.sh <<'EOF'
#!/bin/bash
cd /root/hyperliquid_bot
source venv/bin/activate
python3 scripts/watchdog.py >> /var/log/hl-bot-watchdog.log 2>&1
EOF
chmod +x scripts/run_watchdog.sh

cat > scripts/run_daily.sh <<'EOF'
#!/bin/bash
cd /root/hyperliquid_bot
source venv/bin/activate
python3 scripts/daily_summary.py >> /var/log/hl-bot-daily.log 2>&1
EOF
chmod +x scripts/run_daily.sh

# Установить crontab
cat > /tmp/cron_hlbot <<'EOF'
*/5 * * * * /root/hyperliquid_bot/scripts/run_watchdog.sh
0 22 * * * /root/hyperliquid_bot/scripts/run_daily.sh
EOF
crontab /tmp/cron_hlbot
echo "=== installed crontab ==="
crontab -l
rm -f /tmp/cron_hlbot

# Подготовить лог-файлы
touch /var/log/hl-bot-watchdog.log /var/log/hl-bot-daily.log
chmod 644 /var/log/hl-bot-watchdog.log /var/log/hl-bot-daily.log

echo "=== Setup done ==="
echo "  Watchdog cron: каждые 5 мин"
echo "  Daily summary: 22:00 UTC = 06:00 WITA (Bali) — утром юзеру"
echo "  Logs: /var/log/hl-bot-{watchdog,daily}.log"
