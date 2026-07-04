"""MM% monitor — alert если MM% >= 45.0%, cooldown 48h на бота.

Юзер 2026-05-10: «если ММ подходит к 45% я хочу сигнал, но не чаще
чем раз в 48 часов на каждого бота».

Запускается cron каждый час. State в /tmp/last_mm_alert_<exchange>.json
(переопределяется через env MM_ALERT_STATE).
"""
import os, sys, json, time
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / '.env')
# Bypass HL universe-fetch at import (mm_monitor doesn't need COINS list).
# Без этого 429 на info.metaAndAssetCtxs валит mm_monitor → нет MM% алерта.
os.environ.setdefault('COINS', 'BTC')

import requests
from bot.exchange_factory import get_exchange_client
from bot.config import Settings

TOKEN = os.getenv('TG_BOT_TOKEN', '').strip()
CHAT = os.getenv('TG_CHAT_ID', '').strip()
MM_THRESHOLD_PCT = 45.0
ALERT_COOLDOWN_SEC = 48 * 3600

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
# State file под bot_dir/data чтобы non-root боты (pacifica ubuntu) могли писать.
# Override через env MM_ALERT_STATE если нужно явно.
LAST_ALERT = Path(os.getenv(
    'MM_ALERT_STATE',
    str(PROJECT_ROOT / 'data' / f'last_mm_alert_{s.exchange.lower()}.json'),
))
LAST_ALERT.parent.mkdir(parents=True, exist_ok=True)


def get_mm_state():
    """Returns (equity_usd, mm_used_usd). Per-exchange path.

    Все ветки используют c.account_value() как unified denominator
    (HL: portfolio.day[-1] = main+spot+HIP-3+staking+vault).
    Client init lazy — для HL не строим client'а, чтобы init-time 429 на
    info.metaAndAssetCtxs не валил mm_monitor.
    """
    name = s.exchange.lower()

    if name == 'hyperliquid':
        import urllib.request, json as _json
        addr = os.environ['HYPERLIQUID_ACCOUNT_ADDRESS']

        def _info(body):
            req = urllib.request.Request(
                'https://api.hyperliquid.xyz/info',
                data=_json.dumps(body).encode(),
                headers={'Content-Type': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return _json.loads(r.read())

        # MM: main + xyz HIP-3 (single clearinghouseState calls — cheap, no portfolio).
        mm = 0.0
        eq_legacy = 0.0
        try:
            cs = _info({'type': 'clearinghouseState', 'user': addr})
            mm += float(cs.get('crossMaintenanceMarginUsed') or 0)
            eq_legacy += float((cs.get('marginSummary') or {}).get('accountValue') or 0)
        except Exception:
            pass
        try:
            xyz = _info({'type': 'clearinghouseState', 'user': addr, 'dex': 'xyz'})
            mm += float(xyz.get('crossMaintenanceMarginUsed') or 0)
            eq_legacy += float((xyz.get('marginSummary') or {}).get('accountValue') or 0)
        except Exception:
            pass

        # Denominator: try unified account_value() first (includes spot+staking+vault).
        # При 429 на portfolio fallback к perp-only sum выше — MM% inflated но не падаем.
        try:
            c = get_exchange_client(s)
            eq = float(c.account_value())
        except Exception as e:
            print(f'WARN: account_value 429/fail, fallback to perp-only eq: {type(e).__name__}')
            eq = eq_legacy
        return eq, mm

    c = get_exchange_client(s)

    if name == 'kraken':
        st = c.user_state()
        eq = float((st.get('marginSummary') or {}).get('accountValue') or 0)
        mm = float(st.get('crossMaintenanceMarginUsed') or 0)
        return eq, mm

    if name == 'nado':
        sm = c._summary()

        def _x18(v):
            return int(v) / 1e18

        healths = list(sm.healths) if sm and sm.healths else []
        if len(healths) >= 3:
            eq = _x18(healths[2].health)
            mm = max(0.0, eq - _x18(healths[1].health))
        elif len(healths) >= 2:
            eq = _x18(healths[0].health)
            mm = max(0.0, eq - _x18(healths[1].health))
        else:
            eq = mm = 0.0
        return eq, mm

    if name == 'pacifica':
        resp = c._signed_request('GET', '/account', 'get_account', {})
        if not resp.get('success'):
            raise RuntimeError(f"/account failed: {resp.get('error') or resp}")
        d = resp.get('data') or {}
        eq = float(d.get('account_equity') or d.get('cross_account_equity') or 0)
        mm = float(d.get('cross_mmr') or 0)  # USD value (verified 2026-05-16)
        return eq, mm

    if name == 'extended':
        # x10 SDK margin_ratio = MM_used / equity (verified vs equity/IM math).
        bal = c._bridge.run(c._client.account.get_balance(), timeout=10).data
        eq = float(bal.equity)
        mm = float(bal.margin_ratio) * eq
        return eq, mm

    if name == 'ib':
        # exchange_ib.user_state() shim → crossMaintenanceMarginUsed (USD).
        st = c.user_state()
        eq = float(c.account_value())  # NetLiquidation
        mm = float(st.get('crossMaintenanceMarginUsed') or 0)
        return eq, mm

    raise RuntimeError(f"MM not implemented for exchange={name}")


try:
    eq, mm = get_mm_state()
except Exception as e:
    print(f'mm_state fail: {type(e).__name__}: {e}'); sys.exit(0)

pct = (mm / eq * 100.0) if eq > 0 else 0.0
now = time.time()
print(f'{label} MM {pct:.2f}% (used ${mm:.0f} / acct ${eq:.0f})')

if pct < MM_THRESHOLD_PCT:
    sys.exit(0)

last_t = 0.0
if LAST_ALERT.exists():
    try:
        last_t = float(json.loads(LAST_ALERT.read_text()).get('t', 0))
    except Exception:
        last_t = 0.0

if now - last_t < ALERT_COOLDOWN_SEC:
    left_h = (ALERT_COOLDOWN_SEC - (now - last_t)) / 3600
    print(f'COOLDOWN active ({left_h:.1f}h left), skip MM alert (pct {pct:.2f}%)')
    sys.exit(0)

if not (TOKEN and CHAT):
    print('TG creds not set, skip')
    sys.exit(0)

msg = f'[{label}] MM {pct:.1f}% — порог {MM_THRESHOLD_PCT:.0f}% (used ${mm:.0f} / acct ${eq:.0f})'
try:
    requests.post(
        f'https://api.telegram.org/bot{TOKEN}/sendMessage',
        data={'chat_id': CHAT, 'text': msg}, timeout=10,
    )
    LAST_ALERT.write_text(json.dumps({'t': now, 'pct': pct}))
    print('MM ALERT sent:', msg)
except Exception as e:
    print(f'TG send fail: {type(e).__name__}: {e}')
