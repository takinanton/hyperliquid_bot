#!/bin/bash
# MM% monitor cron wrapper. Threshold 45%, cooldown 48h.
# state file derived inside python from s.exchange (per-bot isolation).
BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BOT_DIR" || exit 1
# shellcheck disable=SC1091
source venv/bin/activate
python3 scripts/mm_monitor.py >> "/tmp/mm_monitor_$(basename "$BOT_DIR").log" 2>&1
