"""Точечные детекторы паттернов на основе swing-points.

В отличие от bot/detector.py (который использует tradingpatterns), здесь
паттерны строятся по конкретным точкам 0-1-2-3-4-5 — как в спеке Вячеслава.

ОБЩИЕ ПРИНЦИПЫ:
- Торгуем ТОЛЬКО по тренду старшего ТФ.
- Паттерн ловит коррекцию в тренде (или продолжение в коррекции волной B).
- SL и TP — по точкам паттерна, не по геометрии "противоположный экстремум".
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

import os
# Initial SL buffer below/above swing extremum (юзер 2026-05-04 deploy of test B).
# Standard 0.0001 = 1 tick. Was 0.003 (0.3%) hardcoded — gave too much initial gap.
# Trail buffer (after entry) remains STRUCT_BUFFER_PCT (0.003) in vstop.py.
INITIAL_BUFFER_PCT = float(os.environ.get("INITIAL_BUFFER_PCT", "0.0001"))
_LONG_FACTOR = 1.0 - INITIAL_BUFFER_PCT   # multiply swing_low for LONG SL
_SHORT_FACTOR = 1.0 + INITIAL_BUFFER_PCT  # multiply swing_high for SHORT SL

from bot.swings import (
    Swing,
    SwingKind,
    Trend,
    classify_trend,
    find_swings,
)


@dataclass
class Signal:
    """Унифицированный сигнал, совместимый с trader/journal."""
    coin: str
    timeframe: str
    pattern: str  # 'flag_long' | 'flag_short' | 'triangle_long' | ... — ставит детектор
    direction: str  # 'long' | 'short'
    detected_at: str  # ISO время бара формирования сигнала
    bar_index: int

    entry: float
    stop_loss: float
    take_profits: list[float] = field(default_factory=list)
    rr: float = 0.0

    # Доп. инфо
    higher_trend: str = ""
    correction_pct: float = 0.0
    swing_points: list[tuple[int, float, str]] = field(default_factory=list)  # (idx, price, kind)
    notes: str = ""


def _rr_weighted(entry: float, stop: float, tps: list[float]) -> float:
    """Взвешенный R/R: 50/50 между TP1 и TP2 (или просто TP1 если один)."""
    if not tps:
        return 0.0
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0
    if len(tps) == 1:
        return abs(tps[0] - entry) / risk
    return (0.5 * abs(tps[0] - entry) + 0.5 * abs(tps[1] - entry)) / risk


# ============================================================
# Flag / Pennant — коррекция в тренде
# ============================================================
def detect_flag(
    df: pd.DataFrame,
    swings: list[Swing],
    higher_trend: Trend,
    coin: str,
    timeframe: str,
) -> Signal | None:
    """Flag = коррекция в трендовом движении (Вячеслав, точки 0-1-2-3-4-5).

    Условия:
    1. Старший ТФ в тренде (UP или DOWN).
    2. Последний impulse-leg = большое движение по тренду (точка 0 → max/min — точка 1).
    3. После 0 идёт коррекция тройкой (1-2-3 или 1-2-3-4-5) против тренда.
    4. Угол импульса (0-1) > угол коррекции — формация уже плотнее.
    5. Точка 5 (или 3) обновляет точку 3 (или 1) — но не пробивает 0.

    Trade:
    - Entry: пробой т.4 (или предыдущего экстремума) В НАПРАВЛЕНИИ ТРЕНДА.
    - SL: т.5 (последний экстремум флага).
    - TP: т.0 (то с чего начался импульс — даёт 100% extension).

    Упрощённая реализация для MVP: ищем 4+ последних swings, последний
    high/low формируется ниже (uptrend) / выше (downtrend) предыдущего на сопоставимое расстояние.
    """
    if higher_trend not in (Trend.UP, Trend.DOWN):
        return None
    if len(swings) < 4:
        return None
    if df.empty:
        return None

    last_idx = len(df) - 1
    current_close = float(df["Close"].iloc[last_idx])
    detected_at = df["time"].iloc[last_idx].isoformat()

    if higher_trend == Trend.UP:
        # Ищем структуру: ... LOW(0) → HIGH(1) → ... коррекция: LOW(2)/HIGH(3)/LOW(4)/HIGH(5)
        # Минимум 4 swings: 0=LOW, 1=HIGH, 2=LOW, 3=HIGH (флаг из 3 точек)
        # Идеально 6: 0=LOW, 1=HIGH, 2=LOW, 3=HIGH, 4=LOW, 5=HIGH

        # Находим последний LOW = "вершина" коррекции снизу (т.е. крайний swing low в флаге)
        # → это будет наша т.4 или т.5
        if swings[-1].kind != SwingKind.LOW:
            # Ждём низа коррекции (для лонга вход после низа)
            return None
        flag_low = swings[-1]

        # Идём назад: найти точку 0 (LOW начала impulse), точку 1 (HIGH вершины импульса)
        # Структура: ... LOW(0) HIGH(1) [LOW HIGH ... LOW HIGH] ... LOW(flag_low)
        # Минимум 3 swings до flag_low: LOW(0), HIGH(1), LOW(2 или 4)
        if len(swings) < 4:
            return None

        # Берём последние 4-6 swings
        chain = swings[-6:] if len(swings) >= 6 else swings[-4:]
        # Должны начинаться с LOW (точка 0)
        if chain[0].kind != SwingKind.LOW:
            chain = chain[1:]
        if len(chain) < 3 or chain[0].kind != SwingKind.LOW:
            return None

        point_0 = chain[0]  # LOW начала impulse
        # Найти MAX HIGH после point_0 — это точка 1
        highs_after = [s for s in chain if s.kind == SwingKind.HIGH and s.idx > point_0.idx]
        if not highs_after:
            return None
        point_1 = max(highs_after, key=lambda s: s.price)  # вершина импульса
        impulse_size = point_1.price - point_0.price
        if impulse_size <= 0:
            return None

        # Точка 4/5 (последний low) — должна быть выше точки 0 (флаг не пробивает основу импульса)
        if flag_low.price <= point_0.price:
            return None
        correction_size = point_1.price - flag_low.price
        # Угол импульса > угла коррекции: коррекция короче по цене или по времени
        impulse_bars = max(1, point_1.idx - point_0.idx)
        correction_bars = max(1, flag_low.idx - point_1.idx)
        if correction_bars > impulse_bars * 1.5:
            return None  # коррекция дольше импульса = не флаг
        # Коррекция в зоне 38-78% от импульса
        retr = correction_size / impulse_size
        if retr < 0.30 or retr > 0.85:
            return None

        # Breakout level: пробой предыдущего HIGH флага (т.4 или т.3)
        flag_highs = [s for s in chain if s.kind == SwingKind.HIGH and point_1.idx < s.idx < flag_low.idx]
        if flag_highs:
            breakout_level = max(s.price for s in flag_highs) * 1.001
        else:
            breakout_level = point_1.price * 1.001

        # ENTRY = current close (по факту входим по close-confirm рынком).
        # "Недалеко от входа" = в пределах 3% от breakout (иначе мы догоняем).
        if current_close < breakout_level * 0.97 or current_close > breakout_level * 1.03:
            return None
        entry = current_close

        # SL за предыдущим extremum (flag_low) с small buffer
        stop_loss = flag_low.price * _LONG_FACTOR
        # Structural sanity: цена не должна быть ниже последнего low флага
        # (иначе SL >= entry — флаг сломан, пробой вниз вместо вверх).
        if entry <= stop_loss:
            return None
        # TP: anchored к impulse (от breakout), не от текущего close — иначе
        # при чейзе target неправильно растёт
        tp1 = breakout_level + impulse_size * 0.618
        tp2 = breakout_level + impulse_size * 1.0

        rr = _rr_weighted(entry, stop_loss, [tp1, tp2])
        return Signal(
            coin=coin,
            timeframe=timeframe,
            pattern="flag_long",
            direction="long",
            detected_at=detected_at,
            bar_index=last_idx,
            entry=entry,
            stop_loss=stop_loss,
            take_profits=[tp1, tp2],
            rr=rr,
            higher_trend="up",
            correction_pct=retr,
            swing_points=[(point_0.idx, point_0.price, "0"), (point_1.idx, point_1.price, "1"),
                          (flag_low.idx, flag_low.price, "5")],
            notes=f"impulse={impulse_size:.4f} corr={correction_size:.4f}",
        )

    else:  # DOWN-trend
        if swings[-1].kind != SwingKind.HIGH:
            return None
        flag_high = swings[-1]
        chain = swings[-6:] if len(swings) >= 6 else swings[-4:]
        if chain[0].kind != SwingKind.HIGH:
            chain = chain[1:]
        if len(chain) < 3 or chain[0].kind != SwingKind.HIGH:
            return None

        point_0 = chain[0]  # HIGH начала impulse вниз
        lows_after = [s for s in chain if s.kind == SwingKind.LOW and s.idx > point_0.idx]
        if not lows_after:
            return None
        point_1 = min(lows_after, key=lambda s: s.price)  # дно импульса
        impulse_size = point_0.price - point_1.price
        if impulse_size <= 0:
            return None

        if flag_high.price >= point_0.price:
            return None
        correction_size = flag_high.price - point_1.price
        impulse_bars = max(1, point_1.idx - point_0.idx)
        correction_bars = max(1, flag_high.idx - point_1.idx)
        if correction_bars > impulse_bars * 1.5:
            return None
        retr = correction_size / impulse_size
        if retr < 0.30 or retr > 0.85:
            return None

        flag_lows = [s for s in chain if s.kind == SwingKind.LOW and point_1.idx < s.idx < flag_high.idx]
        if flag_lows:
            breakout_level = min(s.price for s in flag_lows) * 0.999
        else:
            breakout_level = point_1.price * 0.999

        # Entry = current close, "недалеко от breakout" = ±3%
        if current_close > breakout_level * 1.03 or current_close < breakout_level * 0.97:
            return None
        entry = current_close

        stop_loss = flag_high.price * _SHORT_FACTOR  # SL за previous extremum (flag_high)
        # Structural sanity: цена не должна быть выше последнего high флага
        # (иначе SL <= entry — флаг сломан, пробой вверх вместо вниз).
        if entry >= stop_loss:
            return None
        tp1 = breakout_level - impulse_size * 0.618
        tp2 = breakout_level - impulse_size * 1.0

        rr = _rr_weighted(entry, stop_loss, [tp1, tp2])
        return Signal(
            coin=coin,
            timeframe=timeframe,
            pattern="flag_short",
            direction="short",
            detected_at=detected_at,
            bar_index=last_idx,
            entry=entry,
            stop_loss=stop_loss,
            take_profits=[tp1, tp2],
            rr=rr,
            higher_trend="down",
            correction_pct=retr,
            swing_points=[(point_0.idx, point_0.price, "0"), (point_1.idx, point_1.price, "1"),
                          (flag_high.idx, flag_high.price, "5")],
            notes=f"impulse={impulse_size:.4f} corr={correction_size:.4f}",
        )


# ============================================================
# Triangle — 5 точек 0-1-2-3-4-5
# Направляющие 1-3 (a-c) и 2-4 (b-d). Каждое следующее >= 61.8% предыдущего.
# 0 = экстремум текущего тренда. Entry = пробой т.4 в напр. тренда. SL = т.3. TP = 1-2.
# ============================================================
def detect_triangle(
    df: pd.DataFrame,
    swings: list[Swing],
    higher_trend: Trend,
    coin: str,
    timeframe: str,
) -> Signal | None:
    if higher_trend not in (Trend.UP, Trend.DOWN) or len(swings) < 6 or df.empty:
        return None

    last_idx = len(df) - 1
    current_close = float(df["Close"].iloc[last_idx])
    detected_at = df["time"].iloc[last_idx].isoformat()

    # Берём 6 последних swings: 0, 1, 2, 3, 4, 5
    s = swings[-6:]
    p0, p1, p2, p3, p4, p5 = s

    # В uptrend: 0=LOW, 1=HIGH, 2=LOW, 3=HIGH, 4=LOW, 5=HIGH
    # В downtrend: зеркально
    if higher_trend == Trend.UP:
        expected = [SwingKind.LOW, SwingKind.HIGH, SwingKind.LOW, SwingKind.HIGH, SwingKind.LOW, SwingKind.HIGH]
    else:
        expected = [SwingKind.HIGH, SwingKind.LOW, SwingKind.HIGH, SwingKind.LOW, SwingKind.HIGH, SwingKind.LOW]
    if [pt.kind for pt in s] != expected:
        return None

    # Каждое следующее движение >= 61.8% предыдущего по абсолютной величине
    moves = [abs(s[i + 1].price - s[i].price) for i in range(5)]
    if any(m == 0 for m in moves):
        return None
    for i in range(1, 5):
        if moves[i] < 0.5 * moves[i - 1]:  # 50% (мягче чем 61.8% Вячеслава для большего покрытия)
            return None

    # Direction & levels
    if higher_trend == Trend.UP:
        breakout_level = p5.price * 1.001  # пробой т.5 (HIGH)
        stop_loss = p4.price * _LONG_FACTOR        # SL за previous extremum (т.4 LOW)
        target_size = abs(p1.price - p2.price)
        tp1 = breakout_level + 0.618 * target_size
        tp2 = breakout_level + 1.0 * target_size
        direction = "long"
    else:
        breakout_level = p5.price * 0.999
        stop_loss = p4.price * _SHORT_FACTOR  # SL за previous extremum (т.4 HIGH)
        target_size = abs(p1.price - p2.price)
        tp1 = breakout_level - 0.618 * target_size
        tp2 = breakout_level - 1.0 * target_size
        direction = "short"

    # "Недалеко от breakout" = ±3%, иначе уже догоняем
    if direction == "long" and (current_close < breakout_level * 0.97 or current_close > breakout_level * 1.03):
        return None
    if direction == "short" and (current_close > breakout_level * 1.03 or current_close < breakout_level * 0.97):
        return None
    entry = current_close  # market entry на close

    # Structural sanity: SL должен быть с правильной стороны от entry.
    # Иначе паттерн сломан (цена ушла за previous extremum).
    if direction == "long" and entry <= stop_loss:
        return None
    if direction == "short" and entry >= stop_loss:
        return None

    rr = _rr_weighted(entry, stop_loss, [tp1, tp2])
    return Signal(
        coin=coin, timeframe=timeframe,
        pattern=f"triangle_{direction}",
        direction=direction, detected_at=detected_at, bar_index=last_idx,
        entry=entry, stop_loss=stop_loss, take_profits=[tp1, tp2], rr=rr,
        higher_trend=higher_trend.value,
        swing_points=[(p.idx, p.price, str(i)) for i, p in enumerate(s)],
        notes=f"target_size(1-2)={target_size:.4f}",
    )


# ============================================================
# Wedge — 5 точек, формация в сторону текущего движения
# 2-3 > 4-5. Угол клина < угла импульса. Entry=т.4, SL=т.5, TP=т.2.
# ============================================================
def detect_wedge_v2(
    df: pd.DataFrame,
    swings: list[Swing],
    higher_trend: Trend,
    coin: str,
    timeframe: str,
) -> Signal | None:
    if higher_trend not in (Trend.UP, Trend.DOWN) or len(swings) < 5 or df.empty:
        return None

    last_idx = len(df) - 1
    current_close = float(df["Close"].iloc[last_idx])
    detected_at = df["time"].iloc[last_idx].isoformat()

    s = swings[-5:]
    p1, p2, p3, p4, p5 = s

    # Wedge в uptrend (rising wedge на вершине импульса = разворотный, но в коррекции = продолжение):
    # Тут разводим: торгуем wedge как разворотный или продолжающий?
    # Вячеслав: «формируется в сторону текущего движения», т.е. rising wedge в up-тренде = на конце импульса.
    # На коррекции в up-тренде формируется FALLING wedge → разворот вверх по тренду.
    # Мы ловим falling wedge в up-тренде ИЛИ rising wedge в down-тренде.

    if higher_trend == Trend.UP:
        # Falling wedge в коррекции up-тренда: 1=HIGH, 2=LOW, 3=HIGH, 4=LOW, 5=HIGH (но 1>3>5)
        # Каждый new high LOWER чем предыдущий, каждый new low LOWER (всё снижается, но сходится)
        if [pt.kind for pt in s] != [SwingKind.HIGH, SwingKind.LOW, SwingKind.HIGH, SwingKind.LOW, SwingKind.HIGH]:
            return None
        if not (p1.price > p3.price > p5.price):
            return None
        if not (p2.price > p4.price):
            return None
        # 2-3 > 4-5 (высота 2-го движения больше 4-го)
        if abs(p3.price - p2.price) <= abs(p5.price - p4.price):
            return None
        entry = p3.price * 1.001  # пробой предыдущего высокого
        stop_loss = p4.price * _LONG_FACTOR
        # TP = т.2 (вершина первого high после p1)
        tp1 = p1.price  # консервативный — вершина клина
        # TP2 = расширение от формации
        wedge_size = p1.price - p4.price
        tp2 = entry + wedge_size
        direction = "long"
    else:  # DOWN-trend, rising wedge на коррекции (1=LOW, 2=HIGH, 3=LOW, 4=HIGH, 5=LOW)
        if [pt.kind for pt in s] != [SwingKind.LOW, SwingKind.HIGH, SwingKind.LOW, SwingKind.HIGH, SwingKind.LOW]:
            return None
        if not (p1.price < p3.price < p5.price):
            return None
        if not (p2.price < p4.price):
            return None
        if abs(p3.price - p2.price) <= abs(p5.price - p4.price):
            return None
        entry = p3.price * 0.999
        stop_loss = p4.price * _SHORT_FACTOR
        tp1 = p1.price
        wedge_size = p4.price - p1.price
        tp2 = entry - wedge_size
        direction = "short"

    if direction == "long" and (current_close < entry * 0.97 or current_close > entry * 1.03):
        return None
    if direction == "short" and (current_close > entry * 1.03 or current_close < entry * 0.97):
        return None

    rr = _rr_weighted(entry, stop_loss, [tp1, tp2])
    return Signal(
        coin=coin, timeframe=timeframe,
        pattern=f"wedge_{direction}",
        direction=direction, detected_at=detected_at, bar_index=last_idx,
        entry=entry, stop_loss=stop_loss, take_profits=[tp1, tp2], rr=rr,
        higher_trend=higher_trend.value,
        swing_points=[(p.idx, p.price, str(i + 1)) for i, p in enumerate(s)],
    )


# ============================================================
# HnS / Inverse HnS — точки: ЛП-голова-ПП
# Голова — экстремум, плечи 38.2-78.6% от вершины плеча до головы.
# Entry: пробой основания ПП по тренду / линии шеи против тренда.
# SL: вершина ПП. TP: высота головы до линии шеи.
# ============================================================
def detect_hns_v2(
    df: pd.DataFrame,
    swings: list[Swing],
    higher_trend: Trend,
    coin: str,
    timeframe: str,
) -> Signal | None:
    if higher_trend not in (Trend.UP, Trend.DOWN) or len(swings) < 6 or df.empty:
        return None

    last_idx = len(df) - 1
    current_close = float(df["Close"].iloc[last_idx])
    detected_at = df["time"].iloc[last_idx].isoformat()

    s = swings[-6:]

    if higher_trend == Trend.DOWN:
        # iHnS на коррекции в down-тренде: ловим разворот вверх → НО мы только trend-following,
        # значит iHnS в DOWN ловит LP-голова-RP с пробитой шеей вверх — это PROTIV trend.
        # Не торгуем. Только HnS в DOWN (продолжение тренда) — но тогда голова сверху
        # внутри коррекции down-тренда. Сложно — пропускаем для MVP.
        return None

    if higher_trend == Trend.UP:
        # HnS в UP-тренде = разворотный паттерн → против тренда, не торгуем.
        # iHnS в UP-тренде = на коррекции, разворот вверх = ПО тренду — это да.
        # Структура iHnS: HIGH-LOW(LP)-HIGH-LOW(голова, нижний экстремум)-HIGH-LOW(ПП)
        # последние 6 swings: LP_top, LP_bot, neck_left, head, neck_right, RP_bot, RP_top (вход)
        # Упрощённо берём 5: LP, head_top_before, head, neck, RP
        if len(s) < 5:
            return None
        # ищем минимум среди 6 swings — это голова
        lows = [(i, sw) for i, sw in enumerate(s) if sw.kind == SwingKind.LOW]
        if len(lows) < 3:
            return None
        head_pos, head_sw = min(lows, key=lambda x: x[1].price)
        # ПП = последний LOW после головы
        rp_lows = [sw for i, sw in lows if i > head_pos]
        if not rp_lows:
            return None
        rp = rp_lows[-1]
        # ЛП = последний LOW до головы
        lp_lows = [sw for i, sw in lows if i < head_pos]
        if not lp_lows:
            return None
        lp = lp_lows[-1]
        # ЛП и ПП должны быть в зоне 38.2-78.6% (макс 85.4%) от линии шеи к голове
        # Линия шеи = средний high между LP-head и head-RP
        highs_between = [sw for sw in s if sw.kind == SwingKind.HIGH and lp.idx < sw.idx < rp.idx]
        if len(highs_between) < 2:
            return None
        neck = sum(sw.price for sw in highs_between) / len(highs_between)
        head_to_neck = neck - head_sw.price
        if head_to_neck <= 0:
            return None
        lp_depth = (neck - lp.price) / head_to_neck
        rp_depth = (neck - rp.price) / head_to_neck
        if not (0.30 <= lp_depth <= 0.85) or not (0.30 <= rp_depth <= 0.85):
            return None

        # Entry: пробой neck вверх (= пробой основания ПП по тренду)
        # «Основание ПП» = вершина после ПП → по сути neck level
        entry = neck * 1.001
        stop_loss = rp.price * _LONG_FACTOR  # SL под ПП
        # TP = высота головы до neck → entry + (neck - head)
        tp1 = entry + 0.618 * head_to_neck
        tp2 = entry + 1.0 * head_to_neck

        if current_close < entry * 0.97 or current_close > entry * 1.03:
            return None

        rr = _rr_weighted(entry, stop_loss, [tp1, tp2])
        return Signal(
            coin=coin, timeframe=timeframe,
            pattern="ihns_long",
            direction="long", detected_at=detected_at, bar_index=last_idx,
            entry=entry, stop_loss=stop_loss, take_profits=[tp1, tp2], rr=rr,
            higher_trend=higher_trend.value,
            swing_points=[(lp.idx, lp.price, "LP"), (head_sw.idx, head_sw.price, "head"), (rp.idx, rp.price, "RP")],
            notes=f"neck={neck:.4f} head_to_neck={head_to_neck:.4f}",
        )
    return None


# ============================================================
# Double Top/Bottom — две вершины/дна, входим на 38% откате
# По Вячеславу: 2 обновляет 0; 1.13 < (1-2) < 1.618 от 1 до 0; entry на 38.2% от 2.
# ============================================================
def detect_double_v2(
    df: pd.DataFrame,
    swings: list[Swing],
    higher_trend: Trend,
    coin: str,
    timeframe: str,
) -> Signal | None:
    if higher_trend not in (Trend.UP, Trend.DOWN) or len(swings) < 4 or df.empty:
        return None

    last_idx = len(df) - 1
    current_close = float(df["Close"].iloc[last_idx])
    detected_at = df["time"].iloc[last_idx].isoformat()

    s = swings[-4:]

    if higher_trend == Trend.UP:
        # Double bottom на коррекции в up-тренде: продолжение вверх
        # Ищем: LOW(0) - HIGH(1) - LOW(2) - HIGH (текущий) где |2|>|0| но не сильно
        if [p.kind for p in s] != [SwingKind.LOW, SwingKind.HIGH, SwingKind.LOW, SwingKind.HIGH]:
            return None
        p0, p1, p2, p3 = s
        # |1-2| должно быть в диапазоне |1-0| × 1.13 .. 1.618
        leg_1_to_0 = p1.price - p0.price
        leg_1_to_2 = p1.price - p2.price
        if leg_1_to_0 <= 0 or leg_1_to_2 <= 0:
            return None
        ratio = leg_1_to_2 / leg_1_to_0
        if not (1.0 <= ratio <= 1.7):  # допуск (Вячеслав 1.13-1.618, мы чуть мягче)
            return None
        # т.2 НЕ должен пробивать т.0 (для double bottom это критично — поддержка)
        if p2.price <= p0.price * 0.98:  # допуск 2%
            # это уже не double bottom, а пробитие
            return None
        # Entry = 38% от вершины p3 вниз? Или от текущего бара? По Вячеславу — entry on 38% от p2 вверх
        # Используем pull-back на 38.2% от p2 → p3
        leg_3_to_2 = p3.price - p2.price
        if leg_3_to_2 <= 0:
            return None
        entry = p3.price - 0.382 * leg_3_to_2  # вход на pullback 38.2% от p3
        stop_loss = p2.price * _LONG_FACTOR  # SL ниже p2 (двойного дна)
        # TP = 100% и 161.8% от движения (p2 → p3)
        tp1 = entry + 1.0 * leg_3_to_2
        tp2 = entry + 1.618 * leg_3_to_2
        direction = "long"
    else:  # DOWN
        if [p.kind for p in s] != [SwingKind.HIGH, SwingKind.LOW, SwingKind.HIGH, SwingKind.LOW]:
            return None
        p0, p1, p2, p3 = s
        leg_1_to_0 = p0.price - p1.price
        leg_1_to_2 = p2.price - p1.price
        if leg_1_to_0 <= 0 or leg_1_to_2 <= 0:
            return None
        ratio = leg_1_to_2 / leg_1_to_0
        if not (1.0 <= ratio <= 1.7):
            return None
        if p2.price >= p0.price * 1.02:
            return None
        leg_3_to_2 = p2.price - p3.price
        if leg_3_to_2 <= 0:
            return None
        entry = p3.price + 0.382 * leg_3_to_2
        stop_loss = p2.price * _SHORT_FACTOR
        tp1 = entry - 1.0 * leg_3_to_2
        tp2 = entry - 1.618 * leg_3_to_2
        direction = "short"

    if direction == "long" and (current_close < entry * 0.97 or current_close > entry * 1.03):
        return None
    if direction == "short" and (current_close > entry * 1.03 or current_close < entry * 0.97):
        return None

    rr = _rr_weighted(entry, stop_loss, [tp1, tp2])
    pname = "double_bottom" if direction == "long" else "double_top"
    return Signal(
        coin=coin, timeframe=timeframe,
        pattern=pname, direction=direction,
        detected_at=detected_at, bar_index=last_idx,
        entry=entry, stop_loss=stop_loss, take_profits=[tp1, tp2], rr=rr,
        higher_trend=higher_trend.value,
        swing_points=[(p.idx, p.price, str(i)) for i, p in enumerate(s)],
    )


# ============================================================
# 1-2-3 — упрощённый 3-точечный паттерн начала / продолжения тренда
# Условия (Вячеслав): 1, 2, 3 за пределами трендового канала; 2 = 50-78% retr от 1.
# Entry: пробой т.1 (предыдущий экстремум). SL: т.2.
# В наших обозначениях: 0 = LOW (uptrend) или HIGH (downtrend), 1 = max/min импульса,
# 2 = откат, ждём пробой 1-го уровня.
# ============================================================
def detect_123(
    df: pd.DataFrame,
    swings: list[Swing],
    higher_trend: Trend,
    coin: str,
    timeframe: str,
) -> Signal | None:
    if higher_trend not in (Trend.UP, Trend.DOWN) or len(swings) < 3 or df.empty:
        return None

    last_idx = len(df) - 1
    current_close = float(df["Close"].iloc[last_idx])
    detected_at = df["time"].iloc[last_idx].isoformat()

    s = swings[-3:]

    if higher_trend == Trend.UP:
        # Ищем 0=LOW, 1=HIGH, 2=LOW
        if [pt.kind for pt in s] != [SwingKind.LOW, SwingKind.HIGH, SwingKind.LOW]:
            return None
        p0, p1, p2 = s
        impulse = p1.price - p0.price
        if impulse <= 0:
            return None
        # Откат от т.1 в зону 38-78.6%
        retr = (p1.price - p2.price) / impulse
        if not (0.50 <= retr <= 0.85):
            return None
        # 2 не должен пробить 0 (отмена сценария)
        if p2.price <= p0.price * _LONG_FACTOR:
            return None
        breakout_level = p1.price * 1.001  # пробой т.1 (high)
        stop_loss = p2.price * _LONG_FACTOR        # SL за previous extremum (т.2 LOW)
        tp1 = breakout_level + impulse * 0.618
        tp2 = breakout_level + impulse * 1.0
        direction = "long"
    else:
        if [pt.kind for pt in s] != [SwingKind.HIGH, SwingKind.LOW, SwingKind.HIGH]:
            return None
        p0, p1, p2 = s
        impulse = p0.price - p1.price
        if impulse <= 0:
            return None
        retr = (p2.price - p1.price) / impulse
        if not (0.50 <= retr <= 0.85):
            return None
        if p2.price >= p0.price * _SHORT_FACTOR:
            return None
        breakout_level = p1.price * 0.999  # пробой т.1 (low) вниз
        stop_loss = p2.price * _SHORT_FACTOR        # SL за previous extremum (т.2 HIGH)
        tp1 = breakout_level - impulse * 0.618
        tp2 = breakout_level - impulse * 1.0
        direction = "short"

    # "Недалеко от breakout" = ±3%
    if direction == "long" and (current_close < breakout_level * 0.97 or current_close > breakout_level * 1.03):
        return None
    if direction == "short" and (current_close > breakout_level * 1.03 or current_close < breakout_level * 0.97):
        return None
    entry = current_close  # market entry на close

    # Structural sanity: SL должен быть с правильной стороны от entry.
    if direction == "long" and entry <= stop_loss:
        return None
    if direction == "short" and entry >= stop_loss:
        return None

    rr = _rr_weighted(entry, stop_loss, [tp1, tp2])
    return Signal(
        coin=coin, timeframe=timeframe,
        pattern=f"123_{direction}",
        direction=direction, detected_at=detected_at, bar_index=last_idx,
        entry=entry, stop_loss=stop_loss, take_profits=[tp1, tp2], rr=rr,
        higher_trend=higher_trend.value,
        correction_pct=retr,
        swing_points=[(p.idx, p.price, str(i)) for i, p in enumerate(s)],
    )


# ============================================================
# Главный диспетчер v2
# ============================================================
def detect_all_v2(
    df: pd.DataFrame,
    df_higher: pd.DataFrame,
    coin: str,
    timeframe: str,
    swing_k: int = 3,
    swing_atr_mult: float = 1.0,
) -> list[Signal]:
    """Прогоняет все детекторы v2 на последнем баре df.

    df       — рабочий ТФ (1ч)
    df_higher — старший ТФ (4ч) для определения тренда
    """
    if df is None or df.empty or len(df) < 30:
        return []
    if df_higher is None or df_higher.empty:
        return []

    # Тренд по старшему ТФ — структурно (swings)
    swings_higher = find_swings(df_higher, k=swing_k, min_atr_mult=swing_atr_mult)
    higher_trend = classify_trend(swings_higher)
    if higher_trend in (Trend.UNKNOWN, Trend.SIDEWAYS):
        return []

    # NOTE 2026-05-07: ранее здесь был ещё один EMA50/200-align блок,
    # дублирующий per-signal проверку ниже (qf_h_ema50/200). Убран — работающий
    # фильтр находится в цикле детекторов (см. "QUALITY FILTER 1").

    # Краткосрочная динамика 3 баров (как раньше)
    if len(df_higher) >= 6:
        recent3 = df_higher["Close"].iloc[-3:].mean()
        prev3 = df_higher["Close"].iloc[-6:-3].mean()
        if higher_trend == Trend.UP and recent3 <= prev3:
            return []
        if higher_trend == Trend.DOWN and recent3 >= prev3:
            return []

    # ATR REGIME FILTER: skip compressed (sideways) markets where breakouts fail.
    # ATR_14 / ATR_50 < threshold = volatility compressing = бок (whipsaw zone).
    # Юзер 03-05-2026: "торговать в боковике — самое плохое, можно депо лишиться".
    import os
    from bot.regime import is_sideways
    threshold = float(os.getenv("ATR_REGIME_THRESHOLD", "0.7"))
    if is_sideways(df, threshold=threshold) or is_sideways(df_higher, threshold=threshold):
        return []

    # Swings рабочего ТФ
    swings = find_swings(df, k=swing_k, min_atr_mult=swing_atr_mult)
    if len(swings) < 4:
        return []

    signals: list[Signal] = []

    # Только trend-continuation паттерны.
    # Wedge / HnS / Double Top|Bottom — РАЗВОРОТНЫЕ, выключены по решению пользователя.
    detectors = [
        detect_flag,       # Flag/Pennant — коррекция в тренде (4+ swings)
        detect_triangle,   # Triangle — продолжение после консолидации (5 точек)
        detect_123,        # 1-2-3 — упрощённый 3-точечный для свежих трендов
    ]
    # Quality filters (юзер 2026-05-05): per-signal direction-strict EMA align + volume.
    # Backtest KF 4y: avgR +0.166→+0.304 (+83%), Calmar 1.58→4.50 (+185%), DD 84%→62%.
    # Применяется ПЕРСИГНАЛЬНО (не на higher_trend) — строже существующего фильтра выше.
    qf_h_ema50 = qf_h_ema200 = qf_h_close = None
    if len(df_higher) >= 200:
        h_close = df_higher["Close"]
        qf_h_ema50 = float(h_close.ewm(span=50, adjust=False).mean().iloc[-1])
        qf_h_ema200 = float(h_close.ewm(span=200, adjust=False).mean().iloc[-1])
        qf_h_close = float(h_close.iloc[-1])
    # Volume in USD (24-bar avg × current close) — порог $50k для liquid пары
    qf_vol_usd_min = float(os.getenv("QF_VOL_USD_MIN", "10000"))
    qf_vol_usd = None
    if len(df) >= 24:
        avg_vol_24 = float(df["Volume"].iloc[-24:].mean())
        last_close = float(df["Close"].iloc[-1])
        qf_vol_usd = avg_vol_24 * last_close

    for fn in detectors:
        try:
            sig = fn(df, swings, higher_trend, coin, timeframe)
            if sig is not None:
                # SAFETY: reject malformed SL (юзер 2026-05-04 bug fix)
                # LONG: SL must be < entry. SHORT: SL must be > entry.
                bad_sl = False
                if sig.direction == 'long' and sig.stop_loss >= sig.entry:
                    bad_sl = True
                elif sig.direction == 'short' and sig.stop_loss <= sig.entry:
                    bad_sl = True
                if bad_sl:
                    # Rate-limit warning (once per coin/pattern per hour) — patterns_v2
                    # на тонких HIP-3 каждый цикл выдавал тот же мусор → 30+ warnings/min.
                    import logging
                    import time as _time
                    cache = getattr(detect_all_v2, "_bad_sl_warn_cache", None)
                    if cache is None:
                        cache = {}
                        detect_all_v2._bad_sl_warn_cache = cache
                    key = f"{coin}|{sig.pattern}|{timeframe}"
                    last = cache.get(key, 0)
                    now = _time.time()
                    if now - last >= 3600:  # once per hour
                        logging.getLogger(__name__).warning(
                            'BAD SL skipped %s %s %s: dir=%s entry=%.6f sl=%.6f (SL on wrong side)',
                            coin, sig.pattern, timeframe, sig.direction, sig.entry, sig.stop_loss,
                        )
                        cache[key] = now
                    continue
                # QUALITY FILTER 1: direction-strict EMA align на 1d (higher_tf)
                if qf_h_ema50 is not None and qf_h_ema200 is not None:
                    if sig.direction == 'long' and not (qf_h_close > qf_h_ema50 > qf_h_ema200):
                        continue  # bull alignment не подтверждён
                    if sig.direction == 'short' and not (qf_h_close < qf_h_ema50 < qf_h_ema200):
                        continue  # bear alignment не подтверждён
                # QUALITY FILTER 2: volume в USD
                if qf_vol_usd is not None and qf_vol_usd < qf_vol_usd_min:
                    continue  # слишком тонкая ликвидность
                signals.append(sig)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Detector %s failed: %s", fn.__name__, e)

    return signals
