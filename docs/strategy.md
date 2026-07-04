# Strategy & Production Config

## АКТИВНЫЕ СТРАТЕГИИ В PROD

1. **patterns_v2**: flag/triangle/123 long+short (через `detect_all_v2`)
2. **Cascade filter v2**: skip if ≥3 confirmed losses в last 60m
3. **Coin whitelist (HL only)**: daily refresh, OI≥$2M + Vol≥$1M + spread≤0.1% + depth≥$5k + levels≥3
4. **BTC market-regime filter** (юзер 2026-05-06): блокирует `flag_short` и `123_short` на крипте если BTC > EMA200 на 4h. HIP-3 (`xyz:*` — stocks/forex/commodities/indices) **exempt** (свои циклы, не зависят от BTC). Backtest KF 4y: drop −36R убытков, +19% avgR per slot. HL OOS 804d: +15.3% avgR per slot. Логика в `bot/risk.py:is_btc_regime_blocked`. BTC ticker per bot в env `BTC_REGIME_TICKER` (HL: `BTC`, KF: `BTC/USD:USD`, Nado: пусто → silent skip). Лог только при изменении регима.

(MACD filter удалён 2026-05-08; ema_50_200 cross удалён 2026-05-10 — см. `docs/log.md`.)

## PRODUCTION CONFIG (юзер выбрал 2026-05-03 — Option B)

```bash
# Risk & R/R
RISK_PER_TRADE=0.01
LOW_LEV_MIN_RR=1.5            # 3x пары
MID_LEV_MIN_RR=1.1            # 5x пары
MIN_RR=0.9                    # 10x+ base
MIN_RR_COUNTERTREND=1.1
PATTERN_MIN_RR=flag_short:1.5

# Position management
MAX_NET_POSITIONS=30
MAX_OPENS_PER_DAY=999          # caps OFF
MAX_OPENS_PER_CYCLE=999
COOLDOWN_HOURS=12
MAX_MARGIN_USED_PCT=0.50       # ⛔ DEX-CAP, не повышать!

# Circuit breaker
MAX_CONSECUTIVE_LOSSES=10
MAX_DRAWDOWN_PCT=0.07

# Exit
EXIT_MODE=vstop_struct
STRUCT_BUFFER_PCT=0.003        # → 0.007 на 2026-10-01

# Premium tier — DISABLED 2026-05-06 (revalidation: antifilter, см. docs/log.md)
PREMIUM_RISK_MULT=1.0
PREMIUM_EXTREMUM_WINDOW=5

# Filter
ATR_REGIME_THRESHOLD=0.7
MACD_FILTER_ENABLED=true
CASCADE_WINDOW_MIN=60
CASCADE_THRESHOLD=3            # was 2; tuned 2026-05-06
MIN_SL_NOTIONAL_USD=100

# Execution quality gates (2026-05-06)
SIGNAL_MAX_DRIFT_PCT=0.01      # skip signal если |mark - entry|/entry > 1.0% (stale-signal guard)
ENTRY_SLIP_WARN_PCT=0.5        # WARN-лог при |fill slip| ≥ 0.5%
MAX_FILL_SLIP_PCT=1.5          # emergency close если |fill slip| ≥ 1.5%
HIP3_MIN_FILL_RATIO=0.5        # HIP-3 partial-fill abort threshold
HIP3_MIN_SL_PCT=0.015          # HIP-3 min SL distance (slippage съедает headroom)
```

## EXECUTION QUALITY GATES (2026-05-06)

Полный pipeline checks ДО market_open (в порядке вызова в `bot/trader.py`):

1. **MM cap** — used_maint + new_maint < MAX_MARGIN_USED_PCT × equity
2. **HIP-3 min SL distance** (только xyz:*) — `(entry-SL)/entry ≥ HIP3_MIN_SL_PCT`
3. **Signal freshness** — `|client.mark_price(coin) - signal.entry| / signal.entry ≤ SIGNAL_MAX_DRIFT_PCT` И direction-aware thesis (long: mark ≥ entry-thr; short: mark ≤ entry+thr). Защита от late-fire после outage / restart.
4. **Liquidity drag** — `effective_RR = signal.rr - (fee_rt + 2×slip_per_side) / stop_pct ≥ MIN_RR`. На KF/Nado tier-defaults для slip (без historical fills).

После fill (в порядке проверки):

5. **Slip cap abort** — если `|filled_slip| ≥ MAX_FILL_SLIP_PCT` → `client.market_close` + mark trade=cancelled. Срабатывает даже при favorable slip (магнитуда indicates broken thesis).
6. **Partial fill abort** — для HIP-3 если `fill_ratio < HIP3_MIN_FILL_RATIO` → emergency close.

**Tier-defaults для slip_per_side** (без historical fills per-coin):

| Tier | HL (file) | KF | Nado |
|---|---|---|---|
| majors (BTC/ETH/SOL/XRP/DOGE) | per-coin | 0.10% | 0.20% |
| mid alts | per-coin | 0.20% | 0.40% |
| thin / exotic / HIP-3 | 0.50% (HIP-3 default) | 0.40% | 0.80% |


## PER-BOT ПЕРЕОПРЕДЕЛЕНИЯ

| Param | HL | KF | Nado | Margin (disabled) |
|---|---|---|---|---|
| MAX_MARGIN_USED_PCT | 0.50 | **0.90** | 0.50 | 0.50 |
| RISK_PER_TRADE | 0.01 | 0.01 | 0.01 | 0.012 |
| MIN_RR | **1.1** | 0.9 | 0.9 | 1.2 |
| MIN_RR_COUNTERTREND | 1.1 | 1.1 | 1.1 | 1.5 |
| MAX_NET_POSITIONS | 30 | 30 | **10** | 30 |
| MAX_DRAWDOWN_PCT | 0.07 | 0.07 | **0.10** | 0.07 |

**Объяснения**:
- KF MM 0.90 ≡ HL/Nado 0.50 — Kraken Flex isolated, не cross. 0.90 isolated даёт тот же буфер что 0.50 cross
- HL MIN_RR 1.1: account забит (50% MM cap часто) → нужна selectivity. Backtest 333d: RR=1.1 даёт +50% денег vs 0.9
- Nado MAX_NET_POSITIONS 10: малый capital, малое min_size

## PATTERN DETECTION

- `bot/patterns_v2.py`: detect_all_v2() — flag/triangle/123 long+short
- `bot/swings.py`: ATR, swings detection
- `bot/ema_strategy.py`: EMA50/200 cross
- Working TF: 1h, Higher TF: 4h (для подтверждения тренда)

## EMA CROSSOVER (research, NOT deployed)

**Победитель** (sweep 2026-05-05, 36 variants):
- EMA50/200 Golden Cross + ATR×3 SL + 1d EMA200 trend filter

| Метрика | Pattern bot (current) | EMA winner |
|---|---|---|
| Avg R / trade | +0.214 | **+0.675** (×3.2) |
| CAGR (cmp) | ~+118%/y | **+241%/y** |
| Max DD | ~84% | **56%** |
| Calmar | ~1.4 | **4.31** |

**Status**: not deployed. Нужно out-of-sample, walk-forward, live paper-trade 2-4 нед.

## HIP-3 (Hyperliquid)

`HIP3_USDC_DEXES = ["xyz"]`. Содержит crypto + stocks (BABA/META/AMZN) + forex (DXY, KRW).

**Реальные max_leverage** (НЕ путать "HIP-3 = низкий leverage" — это враньё):

| Leverage | Coins |
|---|---|
| 3x | REZ, ORDI |
| 5x | FET, STRK |
| 10x | DOGE; **HIP-3 stocks** (TSLA, GOOGL, MSFT, AMZN, META, MSTR, COIN) |
| 20x | SOL; HIP-3: NVDA, AAPL, COST, GOLD, CL |
| 25x | ETH; HIP-3: SILVER |
| 30x | XYZ100 (S&P) |
| 40x | BTC |
| 50x | HIP-3 forex (JPY, EUR) |

**Если в проде акции открываются плохо**: добавить explicit crypto-whitelist для xyz subset.

## DEX HARD-CAP (юзер 2026-05-03)

> "учитывай, что дексы не дают открывать позы, когда ММ выше 50"

Hyperliquid и другие DEXes блокируют open new position если current MM > 50% от equity. Любой backtest где peak MM > 50% — нереалистичный.

`MAX_MARGIN_USED_PCT=0.50` → не трогать на DEX (HL/Nado).

## ЛОГИКА > СТАТА (юзер 2026-05-03)

> "смотри чтоб логично было. возможно слабая статистика где удивительные результаты."

При sweep / выборе параметров:
1. Сначала ЛОГИКА — почему параметр должен работать?
2. Потом числа из бэктеста
3. Удивительный результат (+1000R на 1 паре) = red flag слабой статистики, не "находка"
4. Минимальный sample: **n ≥ 500 trades** на 27 мес

## НЕ КРУТИТЬ COSMETICS

> "крутим стратегию только на качество — никакой косметики" (юзер 2026-05-05, 3-й раз)

**КОСМЕТИКА** (НЕ использовать в sweep):
- risk_per_trade — пропорциональное масштабирование, edge не меняет
- MM cap, account size, leverage — то же

**КАЧЕСТВО** (крутим):
1. Что **меняет avgR**: лучшие entry/exit правила
2. Что **меняет WR**: фильтры на bad setups
3. Конкретно: breakeven SL, trend filter, vol filter, pattern filter, DD circuit breaker, loss streak pause, multi-confirmation, time-of-day filter

## SHARPE = КАЧЕСТВЕННАЯ МЕТРИКА

`Sharpe ≈ avgR × √n / std`. Удвоение avgR компенсирует потерю 75% количества (sqrt(0.25)=0.5).

**Risk-ramping**: если фильтр даёт avgR +X% при n -Y%, можно поднять `RISK_PER_TRADE × (1+X/100)` без роста DD-per-dollar. Только если Sharpe падение < 5%.

## NO COIN BLACKLIST

Юзер двойно: "никаких пар убирать не будем вручную" (02-05) + "монеты исскуственно не удаляем... сегодня нет тренда, завтра появился" (03-05).

**DEFAULT COIN_BLACKLIST = пусто.** Если хочется добавить — спросить юзера явно.

Что РАЗРЕШЕНО:
- HIP-3 dexes по collateral (xyz USDC vs cash/flx)
- Pre-trade live liquidity (если 1h vol тонкий — skip конкретный signal)
- FORCE_LONG_COINS / FORCE_SHORT_COINS (direction bias, не exclusion)

## NO PERIOD SPLITTING

Юзер 2026-05-05: "В тестах не дели периоды. Мы не делим и не узнаем что у нас bear market слишком поздно. Общий анализ всегда".

- Все тесты — на полной истории (KF 4y, Binance 7y, HL 333d only smoke check)
- Никаких train/test split, walk-forward
- Метрики (avg R, DD, Calmar) — на ВСЁМ периоде

## TREND FILTER ВМЕСТО PAIR WHITELIST

Юзер 2026-05-05: "завтра будет альткоин булл ран а мы в жопе все отрезали."

Используем strict trend-strength filter (ADX > 30, EMA200 slope, multi-TF confluence) — natural adaptation. В bull-alts → strong trend → пара пройдёт. В bear/range → не пройдёт.

## ПЛАНОВЫЕ ИЗМЕНЕНИЯ

### 2026-10-01: STRUCT_BUFFER_PCT 0.003 → 0.007 (всех ботах)

По результатам ночного backtest (2026-05-05):
- Текущий 0.30%: $66,985 / DD 41.5% за 333д
- Тестовый 0.70%: **$93,813 / DD 58.6%** = +40% денег, +17pp DD
- Юзер выбрал переключиться 1 окт 2026 (5 мес от now)

```bash
# На каждом боте:
ssh hl-bot "sudo sed -i 's/STRUCT_BUFFER_PCT=0.003/STRUCT_BUFFER_PCT=0.007/' /root/hyperliquid_bot/.env && sudo systemctl restart hyperliquid-bot"
ssh kraken-bot "sed -i 's/STRUCT_BUFFER_PCT=0.003/STRUCT_BUFFER_PCT=0.007/' /root/hyperliquid_bot/.env && systemctl restart kraken-bot"
ssh kraken-bot "sed -i 's/STRUCT_BUFFER_PCT=0.003/STRUCT_BUFFER_PCT=0.007/' /root/nado_bot/.env && systemctl restart nado-bot"
```

Также обновить `verify_config.py` canonical → 0.007.

## KRAKEN MARGIN BOT — DISABLED ($0 balance)

Юзер вывел весь $19,839 на 2026-05-05. Strategy структурно мёртвая (0.52% RT fees убивают edge). Не запускать обратно без явного approve. В audit ignore Margin.
