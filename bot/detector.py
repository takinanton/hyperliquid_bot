"""Детектор паттернов и расчёт уровней (entry / SL / TP).

Использует библиотеку white07S/TradingPatternScanner. Функцию detect_wedge
переопределяем здесь — в апстриме баг с pandas>=2.0 (KeyError x[-1] в rolling.apply).
"""
from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

# Шум от tradingpatterns (FutureWarning по dtype в .loc) — не наш баг
warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"tradingpatterns\..*",
)

log = logging.getLogger(__name__)

# --- Импорт из библиотеки (с фолбэком, если каких-то функций нет) ---
try:
    from tradingpatterns.tradingpatterns import (
        detect_head_shoulder as _detect_head_shoulder_lib,
    )
    from tradingpatterns.tradingpatterns import (
        detect_double_top_bottom as _detect_double_lib,
    )
    from tradingpatterns.tradingpatterns import (
        detect_triangle_pattern as _detect_triangle_lib,
    )
except Exception as e:  # pragma: no cover
    log.warning("tradingpatterns not installed (ok for syntax check): %s", e)
    _detect_head_shoulder_lib = None  # type: ignore[assignment]
    _detect_double_lib = None  # type: ignore[assignment]
    _detect_triangle_lib = None  # type: ignore[assignment]


# Сколько баров назад смотреть для вычисления геометрии паттерна
PATTERN_LOOKBACK: int = 30


# ============================================================
# Перепатченный detect_wedge (фикс под pandas >= 2.0)
# ============================================================
def detect_wedge(df: pd.DataFrame, window: int = 3) -> pd.DataFrame:
    """Детект wedge с raw=True, чтобы apply работал на ndarray (а не Series)."""
    roll = window * 2
    df = df.copy()
    df["trend_high"] = (
        df["High"]
        .rolling(window=roll)
        .apply(
            lambda x: 1 if (x[-1] - x[0]) > 0 else (-1 if (x[-1] - x[0]) < 0 else 0),
            raw=True,
        )
    )
    df["trend_low"] = (
        df["Low"]
        .rolling(window=roll)
        .apply(
            lambda x: 1 if (x[-1] - x[0]) > 0 else (-1 if (x[-1] - x[0]) < 0 else 0),
            raw=True,
        )
    )
    df["wedge_pattern"] = np.where(
        (df["trend_high"] == 1) & (df["trend_low"] == 1),
        "Wedge Up",
        np.where((df["trend_high"] == -1) & (df["trend_low"] == -1), "Wedge Down", None),
    )
    return df


# ============================================================
# PatternSignal
# ============================================================
@dataclass
class PatternSignal:
    coin: str
    timeframe: str
    pattern: str  # 'head_shoulder' | 'inv_head_shoulder' | 'double_top' | 'double_bottom' |
    #                'asc_triangle' | 'desc_triangle' | 'wedge_up' | 'wedge_down'
    direction: str  # 'long' | 'short'
    detected_at: str  # ISO время свечи, на которой паттерн зафиксирован
    bar_index: int
    entry: float
    stop_loss: float
    take_profits: list[float] = field(default_factory=list)
    rr: float = 0.0
    height: float = 0.0
    extras: dict = field(default_factory=dict)


# ============================================================
# Вспомогательные геометрические функции
# ============================================================
def _window_slice(df: pd.DataFrame, idx: int, lookback: int = PATTERN_LOOKBACK) -> pd.DataFrame:
    start = max(0, idx - lookback + 1)
    return df.iloc[start : idx + 1]


def _rr(entry: float, stop: float, tps: list[float]) -> float:
    """Взвешенный R/R: позиция делится 50/50 между TP1 и TP2 → используем средний reward."""
    if not tps:
        return 0.0
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0
    if len(tps) == 1:
        reward = abs(tps[0] - entry)
    else:
        # 50/50 split: средний из двух тейков
        reward = 0.5 * abs(tps[0] - entry) + 0.5 * abs(tps[1] - entry)
    return reward / risk


# ============================================================
# Расчёт уровней по типу паттерна
# ============================================================
def _levels_head_shoulder(window: pd.DataFrame, inverse: bool) -> tuple[float, float, list[float], float] | None:
    if len(window) < 5:
        return None
    # B1 финал: TP1 = 1.0×H, TP2 = 1.5×H (взвешенный R/R = 1.25 при идеальных уровнях)
    if not inverse:
        head = float(window["High"].max())
        neck = float(window["Low"].min())
        if head <= neck:
            return None
        height = head - neck
        entry = neck * 0.999
        stop = head * 1.005
        tp1 = entry - 1.0 * height
        tp2 = entry - 1.5 * height
        return entry, stop, [tp1, tp2], height
    else:
        head = float(window["Low"].min())
        neck = float(window["High"].max())
        if neck <= head:
            return None
        height = neck - head
        entry = neck * 1.001
        stop = head * 0.995
        tp1 = entry + 1.0 * height
        tp2 = entry + 1.5 * height
        return entry, stop, [tp1, tp2], height


def _levels_double(window: pd.DataFrame, top: bool) -> tuple[float, float, list[float], float] | None:
    if len(window) < 5:
        return None
    high = float(window["High"].max())
    low = float(window["Low"].min())
    if high <= low:
        return None
    height = high - low
    if top:
        entry = low * 0.999
        stop = high * 1.005
        tp1 = entry - 1.0 * height
        tp2 = entry - 1.618 * height
    else:
        entry = high * 1.001
        stop = low * 0.995
        tp1 = entry + 1.0 * height
        tp2 = entry + 1.618 * height
    return entry, stop, [tp1, tp2], height


def _levels_triangle(window: pd.DataFrame, ascending: bool) -> tuple[float, float, list[float], float] | None:
    if len(window) < 5:
        return None
    top = float(window["High"].max())
    bot = float(window["Low"].min())
    if top <= bot:
        return None
    height = top - bot
    # B1 финал: TP1 = 1.0×H, TP2 = 1.5×H
    if ascending:
        entry = top * 1.001
        stop = bot * 0.995
        tp1 = entry + 1.0 * height
        tp2 = entry + 1.5 * height
    else:
        entry = bot * 0.999
        stop = top * 1.005
        tp1 = entry - 1.0 * height
        tp2 = entry - 1.5 * height
    return entry, stop, [tp1, tp2], height


def _levels_wedge(window: pd.DataFrame, rising: bool) -> tuple[float, float, list[float], float] | None:
    if len(window) < 5:
        return None
    top = float(window["High"].max())
    bot = float(window["Low"].min())
    if top <= bot:
        return None
    height = top - bot
    # B1: TP1 = 1.0×H, TP2 = 1.5×H (было 0.5 / 1.0)
    if rising:  # Wedge Up → short
        entry = bot * 0.999
        stop = top * 1.005
        tp1 = entry - 1.0 * height
        tp2 = entry - 1.5 * height
    else:  # Wedge Down → long
        entry = top * 1.001
        stop = bot * 0.995
        tp1 = entry + 1.0 * height
        tp2 = entry + 1.5 * height
    return entry, stop, [tp1, tp2], height


# ============================================================
# Главный диспетчер
# ============================================================
def detect_all(df: pd.DataFrame, coin: str, timeframe: str, fresh_bars: int = 3) -> list[PatternSignal]:
    """Прогоняет все детекторы и возвращает свежие сигналы (на последних fresh_bars свечах)."""
    if df is None or df.empty or len(df) < 50:
        return []

    signals: list[PatternSignal] = []

    runners: list[tuple[str, Callable[[pd.DataFrame], pd.DataFrame] | None, str, dict]] = [
        ("head_shoulder", _detect_head_shoulder_lib, "head_shoulder_pattern", {}),
        ("double", _detect_double_lib, "double_pattern", {}),
        ("triangle", _detect_triangle_lib, "triangle_pattern", {}),
        ("wedge", detect_wedge, "wedge_pattern", {}),
    ]

    last_idx = len(df) - 1
    fresh_start = max(0, last_idx - fresh_bars + 1)

    for kind, fn, col, kwargs in runners:
        if fn is None:
            continue
        try:
            out = fn(df.copy(), **kwargs) if kwargs else fn(df.copy())
        except Exception as e:
            log.warning("Detector %s failed for %s: %s", kind, coin, e)
            continue
        if col not in out.columns:
            continue

        for i in range(fresh_start, last_idx + 1):
            val = out[col].iloc[i]
            if val is None or (isinstance(val, float) and pd.isna(val)) or val == "":
                continue
            sig = _build_signal(coin, timeframe, df, i, kind, str(val))
            if sig is not None:
                signals.append(sig)

    return signals


def _build_signal(
    coin: str, timeframe: str, df: pd.DataFrame, i: int, kind: str, value: str
) -> PatternSignal | None:
    win = _window_slice(df, i)
    detected_at = df["time"].iloc[i].isoformat()

    if kind == "head_shoulder":
        if value == "Head and Shoulder":
            levels = _levels_head_shoulder(win, inverse=False)
            direction, pname = "short", "head_shoulder"
        elif value == "Inverse Head and Shoulder":
            levels = _levels_head_shoulder(win, inverse=True)
            direction, pname = "long", "inv_head_shoulder"
        else:
            return None
    elif kind == "double":
        if value == "Double Top":
            levels = _levels_double(win, top=True)
            direction, pname = "short", "double_top"
        elif value == "Double Bottom":
            levels = _levels_double(win, top=False)
            direction, pname = "long", "double_bottom"
        else:
            return None
    elif kind == "triangle":
        if value == "Ascending Triangle":
            levels = _levels_triangle(win, ascending=True)
            direction, pname = "long", "asc_triangle"
        elif value == "Descending Triangle":
            levels = _levels_triangle(win, ascending=False)
            direction, pname = "short", "desc_triangle"
        else:
            return None
    elif kind == "wedge":
        if value == "Wedge Up":
            levels = _levels_wedge(win, rising=True)
            direction, pname = "short", "wedge_up"
        elif value == "Wedge Down":
            levels = _levels_wedge(win, rising=False)
            direction, pname = "long", "wedge_down"
        else:
            return None
    else:
        return None

    if levels is None:
        return None
    entry, stop, tps, height = levels
    rr = _rr(entry, stop, tps)
    return PatternSignal(
        coin=coin,
        timeframe=timeframe,
        pattern=pname,
        direction=direction,
        detected_at=detected_at,
        bar_index=i,
        entry=entry,
        stop_loss=stop,
        take_profits=tps,
        rr=rr,
        height=height,
    )
