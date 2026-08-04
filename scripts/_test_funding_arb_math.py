"""Проверка pair-математики сканера на синтетике: путь кода с двумя venue
из песочницы не отрабатывает (кроме HL всё блокировано egress-policy)."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from funding_arb_scan import Quote, hourly_grid, spread_stats, net_apr, leg_stats

HOUR = 3_600_000
now = int(time.time() * 1000)
now -= now % HOUR
start = now - 30 * 24 * HOUR

# venue A: 1h интервал, ставка 0.00125/ч → 10.95% APR ровно
a = [Quote("A", "BTC", "BTC/USDC:USDC", 0.0000125, 1.0, ts_ms=start + i * HOUR)
     for i in range(1, 30 * 24 + 1)]
# venue B: 8h интервал, ставка 0.00005/8ч → 5.475% APR ровно (в 2 раза меньше)
b = [Quote("B", "BTC", "BTC/USDT:USDT", 0.00005, 8.0, ts_ms=start + i * 8 * HOUR)
     for i in range(1, 30 * 3 + 1)]

print("APR A =", round(a[0].apr, 4), "(ожидаем 10.95)")
print("APR B =", round(b[0].apr, 4), "(ожидаем 5.475)")

ga, gb = hourly_grid(a, start, now), hourly_grid(b, start, now)
print("часов в сетке A =", len(ga), "| B =", len(gb), "(ожидаем ~720 обе — 8ч-venue раскладывается назад)")

sp = spread_stats(ga, gb)
print("spread mean =", round(sp["mean_apr"], 4), "(ожидаем 5.475)")
print("spread %>0  =", round(sp["pct_positive"], 1), "(ожидаем 100.0)")
print("worst 7d    =", round(sp["worst_7d_apr"], 4), "(ожидаем 5.475 — ставка постоянная)")

# обратное направление должно быть отрицательным (пара отсеивается в main)
sp_rev = spread_stats(gb, ga)
print("reverse mean =", round(sp_rev["mean_apr"], 4), "(ожидаем -5.475)")

# комиссии: 0.09% + 0.10% RT = 0.19% за 30d удержания → 2.31% APR штрафа
n = net_apr(sp["mean_apr"], 0.0009 + 0.0010, hold_days=30)
print("net APR =", round(n, 4), "(ожидаем 5.475 - 2.3117 = 3.163)")

# нестабильный спред: половина времени +20%, половина -20% → mean~0, худшее 7д отрицательное
flip = [Quote("C", "X", "X", (0.0000125 if (i // 24) % 2 == 0 else -0.0000125), 1.0,
              ts_ms=start + i * HOUR) for i in range(1, 30 * 24 + 1)]
st = leg_stats(flip)
print("flip mean =", round(st["mean_apr"], 3), "| %>0 =", round(st["pct_positive"], 1),
      "(ожидаем ~0 и ~50%)")
gc = hourly_grid(flip, start, now)
spf = spread_stats(gc, gb)
print("flip vs B: mean =", round(spf["mean_apr"], 3), "| худшее 7d =", round(spf["worst_7d_apr"], 3),
      "(худшее 7д должно быть заметно ниже среднего)")
