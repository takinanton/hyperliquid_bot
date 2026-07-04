# Roadmap — будущие доработки

Юзер 2026-05-03: 3 будущих доработки в очередь. Перед каждой — спросить детали и приоритет.

---

## #0 — Vstop param sweep (foundation, юзер 2026-05-03: "проверить эффективность")

**Текущее**: `atr_mult=2.5, period=ATR(14)`. Валидировано в OVERNIGHT_REPORT_2 (mult 1.5..5.0 → 2.5 баланс), но это было ДО:
- Option B config (net 30, cd 12h)
- ATR regime filter
- pattern_min_rr (flag_short → 1.5)

**Sweep params**:
- atr_mult: 1.5, 2.0, 2.5, 3.0, 3.5, 4.0
- period: 10, 14, 20
- Опционально: trailing-mode варианты (chandelier vs supertrend vs naive ATR)

**Метрики**: ΣR/DD/Sharpe/avgR-per-trade/avg-hold-bars

**Ожидаемый эффект**: cleaner exits = ↑ avg R per trade, ↓ DD.

---

## #1 — Осцилляторы (RSI / MACD / углы импульса)

**Источники**: TS_Hyperliquid_v0.4 + Vyacheslav ТС

- **RSI(14)** — для дивергенций (рабочий TF + старший)
- **MACD** — для паттерна Поджатие (заход через 0 ровно 2 раза) — статус: deployed ✅ (2026-05-06)
- **Угол импульса > угол коррекции** — momentum filter в каждом паттерне
- **На старшем TF т3 = экстремум RSI** — multi-TF confirmation

**ПРАВИЛА ВНЕДРЕНИЯ** (юзер 2026-05-03):
1. **Только ENTRY filter / R/R booster**. НЕ exit-trigger.
   Vstop = единственный exit-механизм. Не закрывать позу досрочно на дивергенции.
2. **Каждую идею — отдельный backtest**. Никаких combo-релизов.
3. Workflow:
   - Реализовать (только entry-filter версию)
   - Backtest на 27 мес HL data
   - Logic-check: n≥500, 7+/10 кварталов профитны
   - Сравнение vs baseline B (Sharpe/ΣR/DD/avgMM)
   - Deploy ТОЛЬКО если Sharpe не падает + DD не растёт
   - Если Sharpe падает — откат
   - Если deployed — неделя live, потом следующая

**Очередь**:
- 🔧 **#0**: Vstop param sweep (foundation)
- ✅ **MACD entry filter** (deployed 2026-05-06)
- 🥇 **#2A**: Угол импульса vs коррекции — easy, в каждом паттерне Vyacheslav
- 🥈 **#3A**: RSI overbought/oversold guard (skip long если RSI>75)
- **#1A**: RSI Divergence (entry-only)
- **#4A**: Multi-TF RSI extremum confirmation
- **#5A**: MACD Compression — НОВЫЙ паттерн (отдельная задача)

**Логика порядка**: сначала foundation (Vstop) — меняя Vstop, меняется который trade winning vs losing. Потом oscillator filters в порядке возрастающей сложности.

**Эмпирические наблюдения 2026-05-03**:
- #2A углы: avgR +11%, n -74% → даже risk-ramping не спасает
- #3A RSI guard: avgR +6%, n -14% → marginal

---

## #2 — Xnn (CM_SS scaling-in)

**Полный анализ**: TS_Hyperliquid_v0.4 + Vyacheslav ТС май.

### Что такое Xnn:
- 2 EMA из ряда Фибоначчи (fast + slow) → "канал" вокруг тренда
- Сигнал = цена заходит МЕЖДУ EMAs → "сигнальный треугольник"
  - Зелёный = LONG, красный = SHORT (по тренду)
- Вход = пробой предыдущего extremum

### Per-trade EMA tuning (по спеке):
Соседние Fib пары: 5/8, 8/13, 13/21, 21/34, 34/55, 55/89...
Подбор индивидуально под coin+TF чтобы:
- Цена пробивает быструю EMA
- Допустимы одиночные пробои медленной
- ≤ 2 закрытых баров за пределами медленной

### Strategy levels:
- 1 = без доборов
- 2-3 = доборы разрешены (target)

### ROADMAP реализации:

**Phase 1 — POC** (~2 часа): симуляция "после +1R открыть ещё одну leg" на исторических winning trades. Если improvement >30% — продолжаем.

**Phase 2 — Detector** (~4 часа):
- `bot/xnn.py`: find_fib_ema_pair(), is_xnn_signal(), xnn_entry_price()
- Интеграция в manage_open_positions

**Phase 3 — Risk mgmt** (~2 часа):
```bash
XNN_MAX_LEGS=3
XNN_LEG_SIZE_DECAY=0.5      # halving
XNN_MAX_PARENT_RR=5         # cap: если parent >5R — не добавлять
XNN_MIN_PARENT_RR=1         # минимум +1R перед dobor
```
DB: `parent_trade_id`, `leg_number` columns.

**Phase 4 — Live test** (~1 неделя на малом capital).

### Key insight:
**Сначала fixed pair (21/55)**, не adaptive Fib tuning. Если fixed работает — потом усложнять.

### Open questions (юзер должен решить):
- Strategy 2 (доборы по тренду) vs 3 (доборы в зонах коррекции)?
- Halving sizing (1/0.5/0.25) vs equal (1/1/1)?
- Cascade SL (parent SL → all close) vs independent legs?

---

## #3 — Циклы ликвидности (Liquidity cycles)

**Понимание (требует подтверждения)**: Wyckoff-style рынок alternates между accumulation (low vol, range) и distribution (trending). Цель — поймать переход.

Возможные применения:
- **Liquidity sweep detection**: pump через recent high (триггер шортовых стопов) с быстрым reversal вниз → вход в обратную сторону
- **Accumulation/distribution detection**: volume + range + close-position в баре
- **Smart money concepts (SMC)**: order blocks, fair value gaps, breaker blocks
- **ICT методы**: liquidity grab patterns

Это БОЛЬШАЯ тема, потребует отдельной сессии. Research разных школ (Wyckoff vs SMC vs ICT) перед выбором подхода.

---

## ПРИОРИТЕТ (моё предложение, юзер пусть скажет свой)

**2 → 1 → 3**:
- **Xnn (#2)** даст самый быстрый return-boost на УЖЕ работающей стратегии
- **Осцилляторы (#1)** могут улучшить WR (постепенно по очереди)
- **Liquidity cycles (#3)** — большой research, отложить

---

## ПЛАНОВЫЕ ИЗМЕНЕНИЯ

### 2026-10-01: STRUCT_BUFFER_PCT 0.003 → 0.007 (всех ботах)

См. `docs/strategy.md`.

---

## TV — TRADINGVIEW STRATEGIES (юзер 2026-05-05)

**MCP сервер для TradingView**: пока НЕ подключён. Когда юзер скажет имя — добавлю.

### ПРАВИЛА:
1. При запросе "найди стратегию X" — сначала TradingView через MCP, не свои интерпретации
2. Обязательное summary ПЕРЕД портом:
   - Название
   - Core logic (одной строкой)
   - Entry/Exit conditions (точно, не догадки)
   - Risk management
   - Все индикаторы + параметры
3. Неполная логика — явно говорю что неясно, делаю **консервативные** предположения
4. Не выдумывать фичи которых не было в оригинале (помечать как `optional improvement`)

### TV → Python:
- **Python first** (бот — Python, не Pine)
- Бэктест порта обязателен на KF 4y И HL 2y через `_metrics.compute_metrics()`
- Сверка с TV strategy tester — расхождение >5% = алгоритм портирован неверно
- НЕ пихать TV-стратегию сразу в production бот. Сначала backtest-скрипт в `/Users/ak/Desktop/HL/scripts/`
- TV alerts → webhook → бот: отдельный endpoint, НЕ смешивать с patterns_v2 сигналами
- Pine → Python трансляция вручную (авто-конвертеры ломают `ta.barssince`, repaint behavior, intra-bar логику)
