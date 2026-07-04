"""Equity drift monitor — alert ТОЛЬКО если equity упал >15% за 24ч.
Юзер 2026-05-04: \"оставь только в тг если полная жопа с депо — если его
просадка за день больше 15% от депо. все остальное удали\".
Запускается cron каждый час. История в /root/data/equity_history_<exchange>.json.
"""
import os, sys, json, requests
from datetime import datetime, timezone
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / '.env')

from bot.exchange_factory import get_exchange_client
from bot.config import Settings

TOKEN = os.getenv('TG_BOT_TOKEN', '').strip()
CHAT = os.getenv('TG_CHAT_ID', '').strip()
DROP_THRESHOLD_24H = 50.0  # ТОЛЬКО этот алерт юзеру
ALERT_COOLDOWN_SEC = 24 * 3600  # юзер 2026-05-10: не чаще раза в сутки

BOT_LABEL = {
    'hyperliquid': 'HL',
    'kraken': 'KF',
    'nado': 'Nado',
    'pacifica': 'Paci',
    'extended': 'Ext',
    'ib': 'IB',
}

s = Settings.from_env()
label = BOT_LABEL.get(s.exchange.lower(), s.exchange.upper())
HISTORY = Path(os.getenv('EQUITY_HISTORY', f'/root/data/equity_history_{s.exchange}.json'))
HISTORY.parent.mkdir(parents=True, exist_ok=True)
LAST_ALERT = HISTORY.with_name(f'last_catastrophe_alert_{s.exchange}.json')

c = get_exchange_client(s)
try:
    eq = c.account_value()
except Exception as e:
    print(f'account_value fail: {e}'); sys.exit(0)

now = datetime.now(timezone.utc).timestamp()
history = []
if HISTORY.exists():
    try: history = json.loads(HISTORY.read_text())
    except Exception: history = []
history.append({'t': now, 'eq': eq})
# Keep last 48h (in case multiple drops needed)
cutoff = now - 48 * 3600
history = [h for h in history if h['t'] >= cutoff]
HISTORY.write_text(json.dumps(history))

# Find equity ~24h ago
def find_eq_at(target_t, tolerance=3600):
    if not history: return None
    closest = min(history, key=lambda h: abs(h['t'] - target_t))
    if abs(closest['t'] - target_t) <= tolerance:
        return closest['eq']
    return None

eq_24h = find_eq_at(now - 24*3600, 3600)
if eq_24h and eq_24h > 0:
    drop = (eq_24h - eq) / eq_24h * 100
    if drop >= DROP_THRESHOLD_24H:
        last_t = 0
        if LAST_ALERT.exists():
            try: last_t = float(json.loads(LAST_ALERT.read_text()).get('t', 0))
            except Exception: last_t = 0
        if now - last_t < ALERT_COOLDOWN_SEC:
            left_h = (ALERT_COOLDOWN_SEC - (now - last_t)) / 3600
            print(f'COOLDOWN active ({left_h:.1f}h left), skip alert (drop {drop:.2f}%)')
        elif TOKEN and CHAT:
            msg = f'[{label}] 🚨 CATASTROPHE — -{drop:.1f}% за 24ч (${eq_24h:.0f} → ${eq:.0f})'
            requests.post(
                f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                data={'chat_id': CHAT, 'text': msg}, timeout=10,
            )
            LAST_ALERT.write_text(json.dumps({'t': now}))
            print('CATASTROPHE ALERT sent:', msg)
    else:
        print(f'OK eq=${eq:.2f} (24h ago ${eq_24h:.2f}, drop {drop:.2f}%)')
else:
    print(f'OK eq=${eq:.2f} (no 24h baseline yet, history={len(history)} pts)')
