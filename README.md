# Hyperliquid Pattern Bot

Бот для автоматической торговли на бирже **Hyperliquid** по графическим паттернам.
Работает 24/7 на VPS, ищет паттерны (ГИП, двойная вершина/дно, треугольник, клин)
на 1ч свечах с фильтром тренда по 4ч, открывает позицию через `market_open`,
ставит SL и два TP.

**Старт всегда на Testnet.** Переход на mainnet — только после успешного MVP.

---

## Что делает бот (коротко)

1. Каждые 5 минут опрашивает 5 пар: BTC, ETH, SOL, BNB, XRP.
2. Берёт 200 свечей по 1ч и 200 по 4ч.
3. На 4ч считает тренд по EMA(200) — Close > EMA → long, иначе short.
4. На 1ч ищет 4 типа паттернов через библиотеку `tradingpatterns`.
5. Для свежих сигналов (последние 3 свечи) считает entry, SL, два TP.
6. Фильтрует: R/R ≥ 1.5 (или 2.0 для контртренда), funding-rate, защита от повтора.
7. Ставит плечо = max по паре (BTC 40x, ETH 25x, альты 10–20x), риск 1% на сделку.
8. Открывает позицию по рынку с slippage 1%, ставит триггерный SL и два триггерных TP по 50%.
9. Всё пишет в SQLite (`data/trades.db`) — журнал сделок и отвергнутых сигналов.
10. **Стопы:** 5 убытков подряд ИЛИ −7% drawdown → бот перестаёт открывать новые сделки.

**В MVP НЕТ:** Vstop, перенос в б/у, Xnn-доборы, Telegram, веб-UI, бэктест, доп. паттерны.

---

## Шаг 1. Получить ключи Hyperliquid Testnet

1. Открой https://app.hyperliquid-testnet.xyz
2. Подключи MetaMask. **Адрес кошелька MetaMask = `HYPERLIQUID_ACCOUNT_ADDRESS`** в .env
3. На странице получи тестовые USDC через faucet.
4. Перейди в **Settings → API** → **Generate Agent Wallet**.
5. Сохрани **приватный ключ агента** (показывается ОДИН раз) — это `HYPERLIQUID_AGENT_PRIVATE_KEY` в .env.
   Он начинается с `0x` и состоит из 64 hex-символов после `0x`.

> Agent — это отдельный кошелёк, который имеет право торговать на твоём аккаунте, но не может выводить средства. Если ключ агента утекёт — атакующий не сможет украсть деньги.

---

## Шаг 2. Поднять VPS на Hetzner

1. Зарегистрируйся на https://www.hetzner.com/cloud
2. Cloud → **Add Server**.
3. **Image:** Ubuntu 24.04
4. **Type:** CX22 (4GB RAM) — около €5/мес. Хватит за глаза.
5. **Location:** Frankfurt или Helsinki.
6. Загрузи свой SSH-ключ или сохрани пароль root, выданный после создания.
7. Получи IP сервера (например, `1.2.3.4`).

---

## Шаг 3. Установка бота на VPS

Подключаемся:
```bash
ssh root@1.2.3.4
```

Ставим зависимости системы:
```bash
apt update && apt install -y python3.10 python3.10-venv git sqlite3
```

Кладём проект (вариант A — свой git):
```bash
git clone https://github.com/USER/hyperliquid_bot.git
cd hyperliquid_bot
```

Вариант B — скопировать с локалки через `scp`:
```bash
# на локалке:
scp -r hyperliquid_bot root@1.2.3.4:/root/
# на VPS:
cd /root/hyperliquid_bot
```

Создаём виртуальное окружение и ставим зависимости:
```bash
python3.10 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install git+https://github.com/white07S/TradingPatternScanner.git
```

Конфигурация:
```bash
cp .env.example .env
nano .env
```

Заполни в `.env`:
- `HYPERLIQUID_NETWORK=testnet`
- `HYPERLIQUID_AGENT_PRIVATE_KEY=0x…` (приватный ключ агента из шага 1)
- `HYPERLIQUID_ACCOUNT_ADDRESS=0x…` (адрес твоего MetaMask)

Сохрани (`Ctrl+O`, `Enter`, `Ctrl+X`).

---

## Шаг 4. Тестовый запуск без торговли

```bash
python -m bot.main --dry-run --once
```

Должно вывести:
- `Account value: $...`
- лог обнаруженных паттернов и расчётных уровней
- БЕЗ реальных ордеров

Если ошибка про ключи — проверь `.env`. Если ошибка `tradingpatterns not installed` — установи библиотеку (см. шаг 3).

---

## Шаг 5. Один реальный проход на Testnet

```bash
python -m bot.main --once
```

Если есть свежие паттерны и они проходят фильтры — бот откроет сделки на Testnet.
Проверь в веб-интерфейсе Hyperliquid Testnet: позиции, SL, TP должны появиться.

---

## Шаг 6. Запуск 24/7 как systemd-сервис

Скопируй сервис, поправь пути в нём при необходимости:
```bash
cp systemd/hyperliquid-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable hyperliquid-bot
systemctl start hyperliquid-bot
```

Логи в реальном времени:
```bash
journalctl -u hyperliquid-bot -f
```

Перезапуск после редактирования `.env`:
```bash
systemctl restart hyperliquid-bot
```

Остановка:
```bash
systemctl stop hyperliquid-bot
```

---

## Шаг 7. Просмотр сделок

```bash
sqlite3 data/trades.db "SELECT id, coin, pattern, direction, entry, status, pnl_dollars FROM trades ORDER BY id DESC LIMIT 20;"
```

Отвергнутые сигналы (видно почему не зашёл):
```bash
sqlite3 data/trades.db "SELECT created_at, coin, pattern, direction, rr, reason FROM rejected_signals ORDER BY id DESC LIMIT 20;"
```

---

## Шаг 8. Переход на mainnet (после успешного теста)

1. Через Hyperliquid bridge закинь $200–500 USDC на свой основной кошелёк.
2. На https://app.hyperliquid.xyz сгенерируй **новый** Agent Wallet (НЕ переиспользуй testnet-ключ).
3. В `.env`:
   ```
   HYPERLIQUID_NETWORK=mainnet
   HYPERLIQUID_AGENT_PRIVATE_KEY=<новый mainnet agent key>
   HYPERLIQUID_ACCOUNT_ADDRESS=<твой mainnet address>
   ```
4. `systemctl restart hyperliquid-bot`
5. Первые сутки — смотри логи каждые пару часов.

---

## CLI флаги

| Флаг | Что делает |
|---|---|
| (без флагов) | Бесконечный цикл, итерация каждые 5 минут |
| `--once` | Один проход и выход |
| `--dry-run` | Не торгует, только пишет сигналы в журнал и логи |

Комбинируются: `--dry-run --once`.

---

## Структура проекта

```
hyperliquid_bot/
├── README.md                   ← этот файл
├── requirements.txt
├── .env.example
├── .gitignore
├── systemd/hyperliquid-bot.service
└── bot/
    ├── __init__.py
    ├── config.py               настройки из .env
    ├── exchange.py             обёртка над hyperliquid-python-sdk
    ├── detector.py             детекторы паттернов + расчёт уровней
    ├── risk.py                 размер позиции, фильтры, circuit breaker
    ├── trader.py               открытие сделки + SL + TP
    ├── journal.py              SQLite журнал
    └── main.py                 главный цикл, CLI
```

---

## Параметры стратегии (можно править в `.env`)

| Параметр | Дефолт | Что значит |
|---|---|---|
| `RISK_PER_TRADE` | `0.01` | Доля Account Value, которую готов потерять на одной сделке |
| `MIN_RR` | `1.5` | Минимальный R/R, чтобы зайти в сделку по тренду старшего ТФ |
| `MIN_RR_COUNTERTREND` | `2.0` | Минимальный R/R для сделок против старшего ТФ |
| `LEVERAGE_MODE` | `max` | Использовать максимальное плечо по паре |
| `SLIPPAGE` | `0.01` | Допустимое проскальзывание на market-входе (1%) |
| `LOOP_INTERVAL_SEC` | `300` | Период основного цикла в секундах |
| `MAX_CONSECUTIVE_LOSSES` | `5` | После N убытков подряд бот замолкает |
| `FUNDING_BLOCK_THRESHOLD` | `0.0005` | \|funding × 8\| > этого порога → не входим в направлении funding |

---

## Частые проблемы

- **`Не задана переменная окружения: HYPERLIQUID_AGENT_PRIVATE_KEY`** — `.env` не создан или поле пустое.
- **`tradingpatterns not installed`** — забыл `pip install git+https://github.com/white07S/TradingPatternScanner.git`.
- **`KeyError: -1` в wedge** — старая версия библиотеки. У нас в `detector.py` есть пропатченный `detect_wedge`, но если упало в импорте — обнови pandas/numpy.
- **Нет сделок несколько часов** — это нормально, паттерны редкие. Смотри `rejected_signals` — если все режутся по `R/R`, значит, рынок плоский.
- **`Order failed`** на Testnet — проверь, что у агента есть авторизация и баланс на основном кошельке (testnet faucet).

---

## Что дальше после MVP

После 2 недель стабильной работы на testnet/mainnet:
- Vstop (трейлинг по ATR)
- Перенос SL в б/у после TP1
- Xnn доборы по тренду
- Расширить до Топ-20 пар × 5 ТФ
- Telegram-алерты
- Бэктест на исторических свечах
- Доп. паттерны: Флаг, 1-2-3, Поджатие
