"""ATR-based regime detection: trending vs sideways/compressing.

Идея: если current ATR(14) << baseline ATR(50) — рынок compressed
(side ways/range), pattern breakouts чаще fail (whipsaws). Trend-following
strategy теряет деньги в boковике.

Текущий trend filter (EMA50/200 + swings) ловит ОБЩЕЕ направление, но
не ловит compressed volatility. Добавляем ATR regime check.

Пример:
  trending: ATR_14 = 200, ATR_50 = 180. ratio = 1.11 → trending ✓
  sideways: ATR_14 = 100, ATR_50 = 180. ratio = 0.55 → SKIP ✗
"""
from __future__ import annotations
import pandas as pd

from bot.swings import atr


def atr_regime_ratio(df: pd.DataFrame, fast: int = 14, slow: int = 50) -> float:
    """Returns ratio of recent volatility to baseline.
    >1.0 = expanding (trending), <1.0 = compressing (sideways).
    Returns 1.0 if not enough data (assume trending = no filter)."""
    if df is None or df.empty or len(df) < slow + 1:
        return 1.0
    atr_fast = float(atr(df, fast).iloc[-1])
    atr_slow = float(atr(df, slow).iloc[-1])
    if atr_slow <= 0:
        return 1.0
    return atr_fast / atr_slow


def is_sideways(df: pd.DataFrame, threshold: float = 0.7,
                fast: int = 14, slow: int = 50) -> bool:
    """True если рынок compressed/sideways (skip new entries)."""
    return atr_regime_ratio(df, fast, slow) < threshold
