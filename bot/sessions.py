"""Trading-session awareness for IB bot.

Trades market orders → only act when target market is open. Pre-close cutoff
prevents entering positions <30min before close (no time to confirm SL placement
before overnight gap risk).

Sessions covered:
  • STK   — US Regular Trading Hours (RTH): Mon-Fri 9:30-16:00 ET
            • pre-close cutoff: skip new entries after 15:30 ET
  • FUT   — CME Globex: Sun 18:00 ET → Fri 17:00 ET, 1h daily break 17:00-18:00 ET

FX session handling удалено 2026-05-12 — forex/CFD/FX-futures полностью убраны
из IB-universe (см. data/manual_ops/2026-05-12_ib_rip_fx_shorts/).

Non-blocking for crypto bots: this module is imported lazily, returns (True, "n/a")
for unknown sec_types — won't change HL/KF/Nado/Paci behavior.
"""
from __future__ import annotations

import os
from datetime import datetime, time as _time, timezone

try:
    from zoneinfo import ZoneInfo  # py3.9+
    _ET = ZoneInfo("America/New_York")
except ImportError:  # fallback
    _ET = timezone.utc  # crude; logs will note this

# Pre-close cutoff in minutes (env-tunable)
_RTH_PRE_CLOSE_CUTOFF_MIN = int(os.getenv("RTH_PRE_CLOSE_CUTOFF_MIN", "30"))


def _now_et(now_utc: datetime | None = None) -> datetime:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(_ET)


def is_session_open(sec_type: str, now_utc: datetime | None = None) -> tuple[bool, str]:
    """Returns (open?, reason).

    sec_type: "STK" / "FUT" — taken from AssetMeta.sec_type.
    """
    s = (sec_type or "").upper()
    now = _now_et(now_utc)
    wd = now.weekday()  # 0=Mon, 6=Sun
    t = now.time()

    if s == "STK":
        # RTH 9:30-16:00 ET, Mon-Fri
        if wd >= 5:
            return False, "weekend (STK closed)"
        if t < _time(9, 30):
            return False, f"pre-RTH ({t.strftime('%H:%M')} ET)"
        if t >= _time(16, 0):
            return False, f"post-RTH ({t.strftime('%H:%M')} ET)"
        # Pre-close cutoff: skip new entries in last N minutes of RTH.
        # Compute as 16:00 minus N minutes using datetime arithmetic.
        from datetime import timedelta
        close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
        cutoff_dt = close_dt - timedelta(minutes=_RTH_PRE_CLOSE_CUTOFF_MIN)
        if now >= cutoff_dt:
            return False, f"pre-close cutoff ({_RTH_PRE_CLOSE_CUTOFF_MIN}min): {t.strftime('%H:%M')} ET"
        return True, "RTH"

    if s == "FUT":
        # Globex: Sun 18:00 ET → Fri 17:00 ET, with 1h daily maintenance 17:00-18:00 ET
        if wd == 5:  # Saturday
            return False, "Saturday closed (FUT)"
        if wd == 6 and t < _time(18, 0):  # Sunday before 18:00
            return False, f"pre-Sun-open (Sun {t.strftime('%H:%M')} ET, opens 18:00)"
        if wd == 4 and t >= _time(17, 0):  # Friday after 17:00
            return False, f"post-Fri-close (Fri {t.strftime('%H:%M')} ET, closes 17:00)"
        # Daily 1h maintenance break (17:00-18:00 ET, Mon-Thu)
        if 0 <= wd <= 3 and _time(17, 0) <= t < _time(18, 0):
            return False, f"daily maintenance break ({t.strftime('%H:%M')} ET)"
        return True, "Globex"

    # Unknown sec_type (crypto/HL/KF/Nado/Paci/Extended) → always open, no-op
    return True, "n/a (always-open)"
