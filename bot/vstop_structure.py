"""Structure-based trailing stop по swing-points.

Long: SL = последний HL (higher-low) после entry × (1 - buffer)
Short: SL = последний LH (lower-high) после entry × (1 + buffer)

Стоп ТРЕЙЛИТСЯ ТОЛЬКО В СТОРОНУ ПРОФИТА (как ATR vstop):
- long: новый стоп принимается ТОЛЬКО если он выше текущего
- short: новый стоп принимается ТОЛЬКО если ниже текущего

Защита от look-ahead: swing считается "подтверждённой" только через k баров
(после того как сформированы оба плеча). На баре i используем swings c idx <= i-k.

Если post-entry swings нет — используем initial_stop (из сигнала).
"""
from __future__ import annotations

import pandas as pd

from bot.swings import Swing, SwingKind


def find_structure_exit(
    df: pd.DataFrame,
    swings: list[Swing],
    direction: str,  # "long" | "short"
    entry_idx: int,
    initial_stop: float,
    buffer_pct: float = 0.003,
    swing_confirm_lag: int = 3,  # k=3 в find_swings
    max_holding_bars: int = 200,
) -> tuple[int, float, str]:
    """Returns (exit_idx, exit_px, reason).
    reason: 'wick_stop' | 'gap_through_stop' | 'timeout'

    gap_through_stop = bar opened beyond stop → fill at Open (overnight gap).
    wick_stop        = intra-bar Low (long) or High (short) touched stop → fill at stop price.

    (close-based 'structure_stop' removed 2026-05-17 — was redundant w.r.t. wick
    check AND understated stop-outs vs real exchange. See
    feedback_vstop_structure_wick_bug.md.)
    """
    if entry_idx >= len(df) - 1:
        return entry_idx, float(df.iloc[entry_idx]["Close"]), "timeout"

    current_stop = initial_stop

    # Sort swings by idx (should already be sorted, but defensively)
    swings_sorted = sorted(swings, key=lambda s: s.idx)
    next_swing_idx = 0  # pointer into swings_sorted

    end_bar = min(entry_idx + max_holding_bars, len(df))
    for i in range(entry_idx + 1, end_bar):
        # Update stop based on new "confirmed" post-entry swings
        # Confirmed = swing.idx <= i - swing_confirm_lag
        confirm_cutoff = i - swing_confirm_lag
        while next_swing_idx < len(swings_sorted):
            s = swings_sorted[next_swing_idx]
            if s.idx > confirm_cutoff:
                break
            # This swing is confirmed; check if post-entry
            if s.idx > entry_idx:
                if direction == "long" and s.kind == SwingKind.LOW:
                    candidate = s.price * (1 - buffer_pct)
                    if candidate > current_stop:
                        current_stop = candidate
                elif direction == "short" and s.kind == SwingKind.HIGH:
                    candidate = s.price * (1 + buffer_pct)
                    if candidate < current_stop:
                        current_stop = candidate
            next_swing_idx += 1

        # Exit check (WICK-MODE, 2026-05-17). Order matters:
        # 1) Open gap-through stop → fill at Open (worse than stop, overnight
        #    gap models real exchange filling at next-session open price).
        # 2) Intra-bar wick touch stop → SL fires on touch (real exchange
        #    behavior: SL is limit/market at stop price, triggers when
        #    Low<=stop for long or High>=stop for short).
        # Close-based check REMOVED 2026-05-17 — was redundant (Low<=Close
        # always, so wick fires before close) AND understated stop-outs
        # in cases where Low touched stop intra-bar but Close recovered
        # above. See feedback_vstop_structure_wick_bug.md.
        bar = df.iloc[i]
        o = float(bar["Open"])
        if direction == "long":
            if o < current_stop:
                return i, o, "gap_through_stop"
            l = float(bar["Low"])
            if l <= current_stop:
                return i, current_stop, "wick_stop"
        else:  # short
            if o > current_stop:
                return i, o, "gap_through_stop"
            h = float(bar["High"])
            if h >= current_stop:
                return i, current_stop, "wick_stop"

    # Reached max holding
    last_idx = end_bar - 1
    return last_idx, float(df.iloc[last_idx]["Close"]), "timeout"
