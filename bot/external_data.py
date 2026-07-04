"""External data feeds: VIX (CBOE Volatility Index) for dynamic position sizing.

Cache-friendly: VIX is a daily-resolution input (close-of-day). We refresh once per
hour at most. On fetch failure → return cached value or None (caller falls back to 1.0×).
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

_vix_cache: tuple[float, float] | None = None  # (value, fetched_ts)
_VIX_TTL_SEC = 3600  # 1 hour
_VIX_FAIL_BACKOFF = 300  # if last fetch failed, retry after 5 min
_last_fail_ts: float = 0.0


def get_vix_close() -> float | None:
    """Return latest VIX close. None on failure (no cache available)."""
    global _vix_cache, _last_fail_ts
    now = time.time()

    if _vix_cache is not None and (now - _vix_cache[1]) < _VIX_TTL_SEC:
        return _vix_cache[0]

    if (now - _last_fail_ts) < _VIX_FAIL_BACKOFF and _vix_cache is not None:
        return _vix_cache[0]  # serve stale during backoff

    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed — VIX sizing disabled (returning None)")
        _last_fail_ts = now
        return _vix_cache[0] if _vix_cache else None

    try:
        df = yf.download("^VIX", period="5d", interval="1d",
                         auto_adjust=False, progress=False, threads=False)
        if df is None or df.empty:
            raise RuntimeError("empty VIX df")
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        v = float(df["Close"].iloc[-1])
        _vix_cache = (v, now)
        log.info("VIX close fetched: %.2f", v)
        return v
    except Exception as e:
        log.warning("VIX fetch failed: %s — using cached %s", e,
                    _vix_cache[0] if _vix_cache else "None")
        _last_fail_ts = now
        return _vix_cache[0] if _vix_cache else None


def vix_size_multiplier(settings) -> float:
    """Dynamic position-size multiplier based on current VIX regime.

    Disabled (returns 1.0) if SETTINGS.vix_sizing_enabled is False or fetch fails.
    """
    if not settings.vix_sizing_enabled:
        return 1.0
    v = get_vix_close()
    if v is None:
        return 1.0  # safe default — no sizing change if data unavailable
    if v > settings.vix_high_threshold:
        return settings.vix_size_high
    if v < settings.vix_low_threshold:
        return settings.vix_size_low
    return 1.0
