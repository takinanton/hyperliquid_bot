"""Hourly heartbeat — alert ONLY if any bot down / disk full / mem critical.
Silent-when-OK design (юзер просил минимум noise per hour).

Once daily at 8:00 UTC sends positive ping для подтверждения что cron работает.
"""
import os, sys, requests, subprocess, json, time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
CHAT = os.getenv("TG_CHAT_ID", "").strip()
if not TOKEN or not CHAT:
    print("no TG creds"); sys.exit(0)

# Service list — auto-skip ones not present on this VPS via service_exists().
# Fleet 2026-05-16: 10 production bots across 5 VPS.
KNOWN_BOTS = (
    "hyperliquid-bot",      # hl-bot main HL
    "lsr-bot",              # hl-bot HL sub-account
    "kraken-bot",           # kraken-bot main KF
    "nado-bot",             # kraken-bot Vertex
    "lsr-kf-bot",           # kraken-bot KF crypto LSR sub
    "pacifica-bot",         # pacifica-bot main
    "pacifica-15m-bot",     # pacifica-bot 15m sub
    "extended-bot",         # extended-bot main
    "extended-15m-bot",     # extended-bot 15m sub
    "ib-bot",               # ib-bot
)

def is_active(svc):
    r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
    return r.stdout.strip() == "active"

def service_exists(svc):
    r = subprocess.run(
        ["systemctl", "list-unit-files", "--no-pager", "--no-legend", svc + ".service"],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())

bots_status = []
issues = []
for svc in KNOWN_BOTS:
    if not service_exists(svc):
        continue
    active = is_active(svc)
    bots_status.append((svc, active))
    if not active:
        issues.append(f"❌ {svc} INACTIVE")

# Disk
df = subprocess.check_output(["df", "-h", "/"]).decode().splitlines()[1]
disk_used_pct = int(df.split()[4].rstrip('%'))
disk_used = df.split()[4]
if disk_used_pct > 85:
    issues.append(f"❌ Disk {disk_used} used")

# Memory
mem = subprocess.check_output(["free"]).decode().splitlines()[1]
mem_total = int(mem.split()[1])
mem_used = int(mem.split()[2])
mem_pct = 100 * mem_used / mem_total if mem_total else 0
if mem_pct > 90:
    issues.append(f"❌ Mem {mem_pct:.0f}%")

now = datetime.now(timezone.utc)
now_str = now.strftime("%Y-%m-%d %H:%M UTC")
hostname = subprocess.check_output(["hostname"]).decode().strip()

# Send if: issues exist OR daily ping window (8:00-8:01 UTC)
send_daily_ping = (now.hour == 8 and now.minute < 5)

if issues:
    msg = (f"🚨 ALERT [{hostname}] {now_str}\n\n"
           + "\n".join(issues)
           + "\n\nServices status:\n"
           + "\n".join(f"{'✅' if a else '❌'} {s}" for s, a in bots_status))
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": msg}, timeout=10)
    print(f"ALERT sent: {r.json().get('ok', False)}")
elif send_daily_ping:
    msg = (f"❤️ Daily heartbeat [{hostname}] {now_str}\n\n"
           + "\n".join(f"✅ {s}" for s, _ in bots_status)
           + f"\n\nDisk: {disk_used} used\n"
           + f"Mem: {mem_pct:.0f}%")
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": msg}, timeout=10)
    print(f"Daily ping sent: {r.json().get('ok', False)}")
else:
    # Silent — log status only
    status_summary = ", ".join(f"{s}={'OK' if a else 'DOWN'}" for s, a in bots_status)
    print(f"OK [{hostname}] {now_str} | {status_summary} | disk {disk_used} mem {mem_pct:.0f}%")
