# Расширенные правила работы

Детальные version правил из CLAUDE.md (которые там присутствуют summary form).

---

## METRICS MARGIN — INITIAL vs MAINTENANCE

**Юзер 2026-05-05** (4-й раз): "ММ 30% какие 70???"

Я путал initial margin и maintenance margin — это РАЗНЫЕ метрики:

| Метрика | Что | Когда |
|---|---|---|
| **Initial Margin** = position_value / leverage | collateral required для открытия | при открытии позы |
| **Maintenance Margin** | минимум для удержания позы | liquidation = MM > equity |

`marginSummary.totalMarginUsed` — **INITIAL** margin. UI показывает **MAINTENANCE**.

### HL API:
```python
state = info.user_state(address)
cmm = float(state["crossMaintenanceMarginUsed"])  # real MM dollars
mm_pct = cmm / account_value  # matches UI
```

### Правило:
1. **MM cap** (block opening) → `crossMaintenanceMarginUsed / equity` ≤ MAX_MM_PCT
2. **Liquidation alert** → тот же показатель, threshold 80%+
3. Если бот выдаёт 2-3× больше или меньше чем UI — **баг метрики**. Сверять до публикации.

---

## MM CAP — ОТ БИРЖИ, А НЕ ОТ НАС

**Юзер 2026-05-05**: "пометь себе что капа от биржи а не нас, больше 50 — ок"

MM cap (`MAX_MARGIN_USED_PCT`) — это **brake на открытие**. При MM > cap биржа/бот **отказывает новым ордерам**, но **существующие позы продолжают** до своих real-liq уровней:
- DEX (HL/Nado): exchange сама блокирует новые при MM > 50%. Hard rule.
- KF: бот сам себе блокирует через `MAX_MARGIN_USED_PCT=0.90`. Real hard-stop ставит на ~100% margin per-position.

**Эквивалентность 0.9 isolated ≈ 0.5 cross**:
- Cross 50% (HL): 50% equity в margin, 50% свободны
- Isolated 90% (KF): 90% equity locked раздельно, 10% свободны, каждая поза имеет own 10% буфер до own liq

В отчётах писать с указанием режима: `avgM% (cross)` для HL/Nado, `avgM% (isolated, ≈cross/1.8)` для KF.

---

## STRICT SL VERIFICATION (4 критерия)

**Юзер 2026-05-05**: "100 раз перепроверь чтобы так не произошло больше"

После любого place_sl / cancel_order / re-balance, перед restart bot service:
1. **Coverage ≥ 99%** — суммарный sl_size покрывает position size
2. **reduceOnly=True** — на КАЖДОМ SL ордере (иначе перевернёт позицию)
3. **Правильная сторона** — sell для long, buy для short
4. **Trigger на безопасной стороне** mark price:
   - long: trig < mark
   - short: trig > mark

**Скрипт**: использовать markPrice → fallback на ticker.last (markPrice часто 0 в fetch_positions). Не использовать division без guard на zero.

**Архитектура — ИСПРАВИТЬ TODO**:
В коде bot должна быть `verify_all_sl()`:
1. Запускается каждый цикл
2. При найденной проблеме — алерт + автоматический re-place
3. Логировать в trades.db

`/root/scripts/verify_kf_sl.py` (TODO создать) — standalone проверка для cron.

---

## LEVERAGE CAP — ТОЛЬКО НА KF, НЕ НА NADO/HL

- **KF**: cap 10x — добавлено после DOGE 50x ликвидации в isolated mode
- **HL**: cross-margin от биржи, наш cap НЕ нужен
- **Nado**: cross-margin от биржи, наш cap НЕ нужен (нет его и сейчас)

В Nado rejected log `leverage=20x` — это `AssetMeta.max_leverage=20`, дефолт Nado для большинства перпов, не наш cap.

---

## БЕЗ РУГАТЕЛЬСТВ В ОТВЕТАХ

**Юзер 2026-05-05**: "ругательные слова не используй"

Не использовать в чате с юзером и в коде/комментах:
- говно, дрянь, херня, хрен, etc.
- мат, грубости

Использовать вместо: «слабые», «низкокачественные», «теряющие», «убыточные».

---

## ТЕСТЫ ЗАПУСКАЕМ БЕЗ РАЗРЕШЕНИЯ ЮЗЕРА

**Юзер 2026-05-05**: "всегда тесты запускай без разрешений"

Если предложил A/B/C/D тесты — **сразу запускаю**, не жду "ок".

Применение:
- Backtest предложил → launch
- Sweep идею → launch
- "Хочу проверить гипотезу X" → launch
- A/B сравнение → launch all variants параллельно

Исключения (всё ещё спрашиваю):
- Code change в production бота → нет, только тест
- Deploy на production → нет, нужен явный go
- Изменение config (.env / risk / leverage) → нет
- Удаление файлов / git reset → нет
- Любое действие с реальными деньгами

---

## ТОЛЬКО COMPOUNDING, НИКАКИХ FIXED RISK

**Юзер 2026-05-05**: "забудь про фикс навсегда"

НЕ используем:
- `ret_fix` / `ann_fix` / `DD_fix`
- "fixed risk" колонки в таблицах

Используем ТОЛЬКО:
- **Compounding** — реинвест прибыли, как настоящие деньги работают
- `ret_cmp` (за период), `ann_cmp` (CAGR), `DD_cmp`, `Calmar`

Compound order = **close_ts**, всегда. Не open_ts. PnL материализуется при закрытии.

---

## НЕ ПИСАТЬ $-СУММЫ В РЕЗУЛЬТАТАХ

**Юзер 2026-05-05**: "сумму никогда не пиши. пиши прибыль на сделку и годовую доходность"

НЕ используем: `$ P&L`, `total $`, `Final equity $X`.

Используем:
- **Avg R / trade (net)** — главная метрика edge
- **Annual return % (CAGR)** — для compounding
- **Max DD %** — риск
- **Calmar ratio** — efficiency

Исключения:
- Финансист-PDF где юзер сказал «как для финансиста»
- Live position size (real money)
- Slippage cost USD per trade (для понимания costs)

---

## ОТЧЁТЫ — ЯВНО "(net)"

**Юзер 2026-05-05**: "результат пиши со слипередж и физ"

В каждом отчёте/таблице с метриками — явно указывать:
- `Avg R / trade (net)`
- `Total R (net)`
- `CAGR (net)`
- `Max DD (net)`

В заголовке: "**со fees + slippage** учтены".

---

## PROACTIVE АУДИТ — НЕ ЖДАТЬ ЮЗЕРА

**Юзер 2026-05-04**: "почему я все проверяю за тебя!"

При каждой значимой паузе (разговор > 30 мин активной работы), перед "жду команду":

1. **Bot health** (HL/KF/Nado):
   - `systemctl is-active`?
   - Circuit breaker activity?
   - manage_open_positions: "Stop ..." log lines в последний час?
   - Open positions vs DB: согласовано?

2. **SL integrity** (для каждой open позы):
   - SL стоит на бирже? (fetch open orders)
   - SL на правильной стороне entry?
   - Trail подтягивается за движением цены?

3. **Settings drift**:
   - `.env` vs canonical PROD config
   - Per-bot allowed overrides actually applied?

4. **Risk caps**:
   - Margin used% < cap?
   - Consecutive losses count?
   - DD vs MAX_DRAWDOWN_PCT?

5. **Cron health**:
   - Watchdog запускается каждые 5 мин?
   - Equity_monitor ежечасно?
   - verify_config.py daily?

Если найден issue — **сразу же фиксить** (если не critical). Critical (force-close pos) → коротко спросить "найден X, фиксить?".

---

## НАЙДЁН БAГ → ПРОВЕРИТЬ И ПОФИКСИТЬ ВЕЗДЕ

**Юзер 2026-05-04**: "если ты нашел что-то для исправления на одной бирже/боте иди всегда проверять/исправлять везде на других"

При обнаружении бага:
1. Зафиксить на текущей бирже
2. **Сразу же**: проверить ВСЕ остальные:
   - HL: `/root/hyperliquid_bot/`
   - KF: `/root/hyperliquid_bot/`  ← shared
   - Nado: `/root/nado_bot/`  ← separate copy
   - Margin: `/root/kraken_margin_bot/`  ← separate copy
3. Если код общий — sync через git pull
4. Если раздельный — копировать вручную: `cp /root/hyperliquid_bot/bot/file.py /root/kraken_margin_bot/bot/file.py`
5. Restart всех затронутых ботов
6. Verify в логах что все работают по новому коду

Когда НЕ применимо: per-bot specific config (.env), HL-only feature.

---

## UNIVERSE FILTER — SLIPPAGE-BASED, НЕ OI

**Юзер 2026-05-04**: "ему ОИ нужен такой, чтобы при конкретной сумме открытия слиппередж был таким, чтобы РР был минимум 0.9"

```
typical_position_notional = account × RISK_PER_TRADE / typical_stop_pct
                          ≈ depo × 1% / 2.5% = depo × 0.40
slip_per_side = L2_walk(pair, typical_position_notional)
drag_R = (fee_RT + 2 × slip_per_side) / typical_stop_pct

# Pair PASS условие:
slip_per_side ≤ ~0.20-0.30%  (drag 0.10-0.20R при stop 2.5%)
```

Per-bot threshold:
| Bot | Account | Notional | Acceptable slip/side |
|---|---|---|---|
| HL | $2.9k | $1.16k | ~0.30% |
| KF | $43k | $17.2k | ~0.20% |
| Margin | $20k | $8k | ~0.20% |
| Nado | $5.5k | $2.2k | ~0.30% |

Refresh hourly + per-trade dynamic check на actual signal stop.

**Реализация TODO**: `bot/liquidity.py::passes_slip_filter(coin, notional, fee_rt, max_drag_R)`.

---

## APPLES-TO-APPLES ПРИ СРАВНЕНИИ БИРЖ

**Юзер 2026-05-04**: "никогда больше не сравнивать два площадки с разными параметрами"

При cross-exchange сравнении:
1. **Universe = INTERSECTION пар** доступных на обеих
2. **Период идентичен**
3. **Конфиг идентичен** — risk, MIN_RR, cooldown, MM cap, exit logic
4. **Fees могут различаться** (обоснованно)
5. **Slippage per-coin** — каждая биржа свой L2 walk
6. **Не сравнивать "лучше/хуже"** на разных universe

❌ "KF -$39k на 36 парах vs HL -$20k на 22 парах = HL лучше" — НЕТ, разные пары!

✅ "На общих 13 парах одинаковый период, MIN_RR=0.9: KF $X, HL $Y."

В каждом cross-exchange отчёте — **первая строка**: "Universe = intersection, N pairs: [...]"

---

## .ENV = PRODUCTION CONFIG ЯВНО

**Юзер 2026-05-04**: "какого фига мы вели настройки а засунули в ботов не то что хотели"

1. **`.env` каждого бота должен содержать ВСЕ production параметры ЯВНО**.
   Никаких "default из config.py подойдёт" — всегда explicit.
2. При любом изменении production config → apply на всех 4 ботах + restart + verify.
3. **`scripts/verify_config.py`** — periodic checker (daily cron):
   - Reads canonical config (CLAUDE.md / `docs/strategy.md`)
   - Reads each .env
   - Diff each parameter
   - Drift → alert
4. **Backtest скрипты** ОБЯЗАНЫ:
   - Импортировать `Settings.from_env()` (не hardcode)
   - Использовать `bot.risk.required_rr()` для RR
   - Тот же exit что в проде (`find_struct_vstop_exit`)
   - PREMIUM_RISK_MULT для is_premium signals
   - **REAL transaction cost** = fee_RT + 2 × slip_per_side
   - MM cap при заполнении позиций

---

## TESTING НА МАКС ГЛУБИНЕ

**Юзер 2026-05-05**: "надо тесты теорий гонять на макс глубине"

- KF 4 года = primary (включает 2022 bear + 2023 ranging + 2024-2026 bull = реальный edge тест)
- HL 333д = только smoke check для уже принятых решений (последний год bull only)
- HL 333д vs KF 4y расхождение >30% — копать причину

**Порядок тестирования**:
1. Гипотеза → KF 4 года full sweep
2. Если работает → детальный sweep на KF
3. Out-of-sample на HL 333д
4. Walk-forward (split train/test) — но юзер 05-05 запретил period split, поэтому пропустить
5. Live paper-trade 2-4 нед
6. Deploy

---

## END-TO-END TRACE (НЕ grep-and-assume)

См. `docs/ops.md`.

---

## DB SIZE != EXCHANGE SIZE

См. `docs/ops.md`.

---

## ОВЕРНАЙТ TASKS — VPS CRON

См. `docs/ops.md`.

---

## РАСПРЕДЕЛЕНИЕ ТЕСТОВ

См. `docs/ops.md`.

---

## NOTIFICATIONS НА КАЖДЫЙ DONE

**Юзер 2026-05-05**: "если ты сделал кучу всего за час, почему ты не сыдал по готовности?"

На КАЖДЫЙ DONE тест в фоне:
1. Mac notification ОБЯЗАТЕЛЬНО:
   ```bash
   osascript -e 'display notification "Test X done: avgR +0.X, DD Y%" with title "Claude" sound name "Glass"'
   ```
2. Если юзер активен — короткий summary в чате
3. Если юзер ушёл — Mac notification + ScheduleWakeup на check каждые 5-10 мин

**Notification содержит итог**: avgR + DD + Calmar (не процесс).

Sounds:
- `Glass` — success
- `Sosumi` — error/critical
- `Submarine` — обычное

---

## MIDPOINT HEALTH CHECK 25%

**Юзер 2026-05-05**: "сделай в памяти проверку на завис раз в 25% от времени процесса"

Раз в 25% от ETA теста — `check_tests.sh` + проверить CPU:
- ETA 10 мин → каждые 2.5 мин
- ETA 30 мин → каждые 7.5 мин
- ETA 60 мин → каждые 15 мин

ScheduleWakeup на 25%, 50%, 75% времени теста. На каждом wakeup: проверить, дать update юзеру, реагировать на висяки.

---

## BACKTEST vs LIVE — ОЖИДАНИЯ

**Цитата друга-трейдера 2026-05-03**: "у меня между тем когда бектест показывал отличную доходность и реальной доходностью прошло несколько месяцев."

Защита от over-fit:
- ✅ **Walk-forward** — 10/10 кварталов в плюс (не overfit)
- ❌ **Out-of-sample** — нет (использовал все 27 мес HL data)
- ⏳ **Live verification** — нужно 2-4 недели live на текущем капитале с ежедневным сравнением actual R vs backtest R
- ❌ **Conservative scaling** — НЕ масштабировать до $30k milestone пока live не покажет хотя бы 50% от backtest за 1 месяц

Если live даёт <30% от backtest — overfit. 50-80% — норм. >80% — слишком хорошо чтобы быть правдой.
