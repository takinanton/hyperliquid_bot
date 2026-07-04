"""Daily config drift check — сравнивает .env каждого бота с canonical PRODUCTION CONFIG.
Если drift → лог в /var/log/config_drift.log + (опц.) Telegram alert.

Запускается cron daily 00:05 UTC.

Canonical PRODUCTION CONFIG — encoded ниже. Источник истины: CLAUDE.md секция
"PRODUCTION CONFIG" + per-bot overrides table.
"""
import os, sys, re
from pathlib import Path
from datetime import datetime, timezone

# Canonical production config (sync'd from CLAUDE.md as of 2026-05-04)
PROD = {
    'common': {
        'RISK_PER_TRADE': '0.01',
        'MIN_RR': '0.9',
        'MIN_RR_COUNTERTREND': '1.1',
        'LOW_LEV_MIN_RR': '1.5',
        'MID_LEV_MIN_RR': '1.1',
        'LOW_LEV_THRESHOLD': '3',
        'MID_LEV_THRESHOLD': '5',
        'PATTERN_MIN_RR': 'flag_short:1.5',
        'MAX_OPENS_PER_DAY': '999',
        'MAX_OPENS_PER_CYCLE': '999',
        'MAX_MARGIN_USED_PCT': '0.50',
        'MAX_CONSECUTIVE_LOSSES': '10',
        'EXIT_MODE': 'vstop_struct',
        'STRUCT_BUFFER_PCT': '0.003',
        'VSTOP_ATR_MULT': '2.5',
        'ATR_REGIME_THRESHOLD': '0.7',
    },
    # Per-bot allowed overrides (anything else = DRIFT)
    'overrides': {
        'kraken-futures': {'MAX_MARGIN_USED_PCT': '0.90'},  # isolated ≡ cross 0.5
        'nado': {},
    }
}

BOTS = {
    'kraken-futures': '/root/hyperliquid_bot/.env',
    'nado': '/root/nado_bot/.env',
}

def parse_env(path):
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        out[k.strip()] = v.strip().split('#')[0].strip()
    return out

def main():
    LOG = Path('/var/log/config_drift.log')
    LOG.parent.mkdir(exist_ok=True)
    drift_found = False
    report = [f'=== Config drift check {datetime.now(timezone.utc).isoformat()} ===']
    for bot, env_path in BOTS.items():
        actual = parse_env(env_path)
        expected = dict(PROD['common'])
        expected.update(PROD['overrides'].get(bot, {}))
        report.append(f'\n--- {bot} ({env_path}) ---')
        bot_drift = False
        for k, want in expected.items():
            got = actual.get(k)
            if got is None:
                report.append(f'  ⚠ MISSING: {k} (expected {want})')
                bot_drift = True
            elif got != want:
                report.append(f'  ❌ DRIFT: {k}={got} (expected {want})')
                bot_drift = True
        if not bot_drift:
            report.append(f'  ✅ OK ({len(expected)} params verified)')
        drift_found = drift_found or bot_drift
    txt = '\n'.join(report) + '\n'
    print(txt)
    LOG.write_text((LOG.read_text() if LOG.exists() else '') + txt)
    sys.exit(1 if drift_found else 0)

if __name__ == '__main__':
    main()
