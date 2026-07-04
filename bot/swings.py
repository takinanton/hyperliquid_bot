"""Swing-point detection (zigzag) и классификация тренда по структуре свингов.

Логика по файлам стратегии:
- Тренд = серия HH+HL (uptrend) или LH+LL (downtrend) на старшем ТФ
- Паттерны строятся на точках 0-1-2-3-4-5 (swing-points)
- Импульс = 1-3-5, коррекция = 2-4 или A-B-C
- Каждое следующее движение в треугольнике >= 61.8% от предыдущего

Реализация:
1. Локальные экстремумы по правилу "max/min в окне ±k баров"
2. Фильтр: соседние swing-точки должны отличаться на >= min_pct (или min_atr_mult * ATR)
3. Альтернация: за swing high обязательно следует swing low (и наоборот)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class SwingKind(str, Enum):
    HIGH = "H"
    LOW = "L"


@dataclass
class Swing:
    idx: int  # bar index в df
    time: pd.Timestamp
    price: float
    kind: SwingKind

    def __repr__(self) -> str:
        return f"{self.kind}@{self.idx}({self.price:.4f})"


# ============================================================
# ATR — для фильтра минимального движения
# ============================================================
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


# ============================================================
# Swing detection
# ============================================================
def find_swings(
    df: pd.DataFrame,
    k: int = 3,
    min_atr_mult: float = 1.0,
    atr_period: int = 14,
) -> list[Swing]:
    """Поиск swing-точек.

    Шаг 1: бар i — локальный max если High[i] = max(High[i-k:i+k+1]) (аналогично для min).
    Шаг 2: фильтр альтернации + минимальной амплитуды.

    Args:
        k: размер окна по обе стороны (3 = ±3 бара). Меньше k → больше swings.
        min_atr_mult: минимальная амплитуда swing'а в ATR. 1.0 = разница High-Low соседних
                      swings должна быть >= 1×ATR. Защищает от шумовых пилообразных swings.
    """
    if len(df) < 2 * k + 2:
        return []

    highs = df["High"].values
    lows = df["Low"].values
    times = df["time"].values
    n = len(df)

    # Шаг 1: кандидаты в swings
    candidates: list[Swing] = []
    a = atr(df, atr_period).values

    for i in range(k, n - k):
        window_high = highs[i - k : i + k + 1]
        window_low = lows[i - k : i + k + 1]
        # Локальный максимум
        if highs[i] == window_high.max() and highs[i] > window_high[k - 1] and highs[i] >= window_high[k + 1]:
            candidates.append(Swing(i, pd.Timestamp(times[i]), float(highs[i]), SwingKind.HIGH))
        # Локальный минимум
        if lows[i] == window_low.min() and lows[i] < window_low[k - 1] and lows[i] <= window_low[k + 1]:
            candidates.append(Swing(i, pd.Timestamp(times[i]), float(lows[i]), SwingKind.LOW))

    if not candidates:
        return []

    # Сортируем по индексу (на одном баре может быть и high и low — оба остаются)
    candidates.sort(key=lambda s: (s.idx, 0 if s.kind == SwingKind.HIGH else 1))

    # Шаг 2: альтернация и фильтр амплитуды
    # Если идут несколько swings одной природы подряд (например, два high'а) — оставляем экстремальный.
    filtered: list[Swing] = []
    for sw in candidates:
        if not filtered:
            filtered.append(sw)
            continue

        last = filtered[-1]
        if sw.kind == last.kind:
            # Несколько подряд той же природы — оставляем более экстремальный
            if sw.kind == SwingKind.HIGH and sw.price > last.price:
                filtered[-1] = sw
            elif sw.kind == SwingKind.LOW and sw.price < last.price:
                filtered[-1] = sw
            # иначе — пропускаем (этот swing хуже текущего)
        else:
            # Альтернация — проверяем минимальную амплитуду
            amp = abs(sw.price - last.price)
            min_amp = min_atr_mult * float(a[sw.idx]) if sw.idx < len(a) else 0
            if amp >= min_amp:
                filtered.append(sw)
            # иначе — игнорируем "псевдо-swing" в шуме

    return filtered


# ============================================================
# Классификация тренда по структуре swings
# ============================================================
class Trend(str, Enum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "side"
    UNKNOWN = "unknown"


def classify_trend(swings: list[Swing], lookback: int = 6) -> Trend:
    """Тренд по последним N swings.

    Классическое определение TA:
    - UPtrend = серия higher-lows (HL) без нарушения (LL). Highs в идеале HH, но единичный LH допускается.
    - DOWNtrend = серия LH без HH.
    - SIDEWAYS = иначе.
    """
    if len(swings) < 4:
        return Trend.UNKNOWN

    last = swings[-lookback:]
    highs = [s for s in last if s.kind == SwingKind.HIGH]
    lows = [s for s in last if s.kind == SwingKind.LOW]

    if len(highs) < 2 or len(lows) < 2:
        return Trend.UNKNOWN

    # Проверяем серию highs и lows
    def consecutive_relation(seq: list[Swing], rel: str) -> int:
        """Сколько подряд от конца идут отношения: 'higher' или 'lower'."""
        cnt = 0
        for i in range(len(seq) - 1, 0, -1):
            if rel == "higher" and seq[i].price > seq[i - 1].price:
                cnt += 1
            elif rel == "lower" and seq[i].price < seq[i - 1].price:
                cnt += 1
            else:
                break
        return cnt

    higher_lows = consecutive_relation(lows, "higher")
    lower_lows = consecutive_relation(lows, "lower")
    higher_highs = consecutive_relation(highs, "higher")
    lower_highs = consecutive_relation(highs, "lower")

    # UP: 2+ HL подряд И последние highs не образуют 2+ LL подряд
    if higher_lows >= 2 and lower_lows == 0:
        return Trend.UP
    # DOWN: 2+ LH подряд И не было HH подряд
    if lower_highs >= 2 and higher_highs == 0:
        return Trend.DOWN
    # Если только последний low ниже — тренд под угрозой / разворот
    if lower_lows >= 1 and higher_lows == 0:
        return Trend.SIDEWAYS
    if higher_highs >= 1 and lower_highs == 0 and higher_lows >= 1:
        return Trend.UP
    if lower_highs >= 1 and higher_highs == 0 and lower_lows >= 1:
        return Trend.DOWN

    return Trend.SIDEWAYS


# ============================================================
# Фаза рынка: импульс vs коррекция
# ============================================================
@dataclass
class MarketPhase:
    trend: Trend
    in_correction: bool
    last_impulse_start: float  # цена начала последнего impulse-движения
    last_impulse_end: float
    correction_low: float  # для uptrend: минимум коррекции; для downtrend: максимум
    correction_pct: float  # сколько откатили в долях от impulse


def detect_phase(swings: list[Swing], current_price: float) -> MarketPhase | None:
    """Определяем: импульс или коррекция, и насколько глубоко откатили.

    Логика:
    - UP-trend, последние swings: ... HL — HH — pullback (текущая цена) → коррекция в up-тренде
    - DOWN-trend, последние swings: ... LH — LL — pullback up → коррекция в down-тренде

    correction_pct: 38-78% — рабочая зона входа на пробое предыдущего экстремума.
    """
    trend = classify_trend(swings)
    if trend in (Trend.UNKNOWN, Trend.SIDEWAYS):
        return None
    if len(swings) < 3:
        return None

    if trend == Trend.UP:
        # Берём последний HH и предыдущий HL → impulse leg
        last_high = next((s for s in reversed(swings) if s.kind == SwingKind.HIGH), None)
        prev_low = None
        for s in reversed(swings):
            if s.kind == SwingKind.LOW and s.idx < (last_high.idx if last_high else 0):
                prev_low = s
                break
        if last_high is None or prev_low is None:
            return None
        impulse_start = prev_low.price
        impulse_end = last_high.price
        # Коррекция: текущая цена ниже HH но выше impulse_start
        if current_price >= impulse_end:
            # ещё в импульсе или новый HH
            return MarketPhase(trend, False, impulse_start, impulse_end, current_price, 0.0)
        if current_price <= impulse_start:
            # коррекция > 100% — тренд под угрозой
            return None
        correction_pct = (impulse_end - current_price) / (impulse_end - impulse_start)
        return MarketPhase(trend, True, impulse_start, impulse_end, current_price, correction_pct)

    else:  # DOWN
        last_low = next((s for s in reversed(swings) if s.kind == SwingKind.LOW), None)
        prev_high = None
        for s in reversed(swings):
            if s.kind == SwingKind.HIGH and s.idx < (last_low.idx if last_low else 0):
                prev_high = s
                break
        if last_low is None or prev_high is None:
            return None
        impulse_start = prev_high.price
        impulse_end = last_low.price
        if current_price <= impulse_end:
            return MarketPhase(trend, False, impulse_start, impulse_end, current_price, 0.0)
        if current_price >= impulse_start:
            return None
        correction_pct = (current_price - impulse_end) / (impulse_start - impulse_end)
        return MarketPhase(trend, True, impulse_start, impulse_end, current_price, correction_pct)
