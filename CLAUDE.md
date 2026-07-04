# Multi-Exchange Pattern Bot — Project Memory

**Цель**: автоматический торговый бот, торгует pattern-сигналы (flag/triangle/123 + EMA cross)
на 4 биржах одновременно с unified codebase.

## АРХИТЕКТУРА

| Бот | Биржа | VPS | Path | Status |
|---|---|---|---|---|
| HL | Hyperliquid (DEX) | hl-bot (Lightsail Tokyo) | `/root/hyperliquid_bot/` | active |
| KF | Kraken Futures | kraken-bot (Snel NL) | `/root/hyperliquid_bot/` | active |
| Nado | Vertex (Ink L2) | kraken-bot | `/root/nado_bot/` (separate copy) | active |
| Margin | Kraken Spot Margin | kraken-bot | `/root/kraken_margin_bot/` | DISABLED ($0) |

**Code base**: один общий `bot/` модуль (`exchange.py`, `exchange_kraken.py`, `exchange_nado.py`, `trader.py`, `main.py`, `journal.py`). Per-bot config в `.env`.

**Deploy**: после `git push` — `git fetch+reset+restart` на каждом боте. См. `docs/ops.md`.

**Tech stack**: Python 3.12, SQLite (`data/trades.db`), systemd units, ccxt (KF/Margin), hyperliquid-python-sdk (HL), nado-sdk (Nado).

## КРИТИЧНЫЕ ПРАВИЛА (часто нарушаю — следить)

### Перед "FIXED"
1. ROOT cause найден (не симптом)
2. Verified bug с real data (cmd + output) ДО fix
3. Все relevant API methods enumerate'нуты (не первый match)
4. DB vs exchange consistency проверен
5. После deploy: cmd + measurable число → "deployed, awaiting verify" пока не verify

Если нет всех 6 — это "deployed, awaiting verify", **НЕ "fixed"**.

### Никогда
- ❌ Не доверять DB > exchange — query exchange first
- ❌ Не отключать feature вместо root cause fix
- ❌ Не публиковать число без сверки с предыдущим тестом на тех же данных
- ❌ Не делить периоды в backtests (всегда полная история)
- ❌ Не убирать coin вручную в blacklist
- ❌ Не использовать `multiprocessing.Pool` на Mac с heavy bot imports
- ❌ Не запускать backtests без `nohup ... > file 2>&1` (теряем при ssh disconnect)

### Всегда
- ✅ End-to-end trace (declaration → usage → exchange API call) перед "бот делает X"
- ✅ Backtests с fees + per-coin slippage (HL 0.09%, KF 0.10%, Nado 0.10% RT)
- ✅ Compounding by close_ts, MM cap 50%, max history depth (KF 4y > HL 333d)
- ✅ После git push на VPS — verify все 3 бота на одном коммите
- ✅ Mac notification на каждый завершённый response: `osascript -e 'display notification ... sound name "Submarine"'`
- ✅ Тесты запускаем сразу без подтверждения (юзер 05-05)

## РАЗРЕШЕНИЯ ОТ ЮЗЕРА (явные)
- Прод-боты можно фиксить и деплоить **без подтверждения** (urgent bugs)
- Тесты, sweep, backtest — **без спроса**
- Изменения `.env` / risk / leverage / стратегия — **без подтверждения** (юзер 2026-05-06)
- Удаление файлов / git reset / push --force — **только с явным go**

## SSH

```bash
ssh hl-bot          # Lightsail Tokyo, sudo nужен (Ubuntu)
ssh kraken-bot      # Snel NL, root direct
```

## ДЕТАЛЬНАЯ ДОКУМЕНТАЦИЯ

- **`docs/log.md`** — лог разработки, dated incidents, история фиксов
- **`docs/strategy.md`** — production config, pattern rules, EMA cross результаты, HIP-3 правила, осцилляторы
- **`docs/ops.md`** — VPS машины, deploy procedure, scripts, auto_monitor, multiprocessing на Mac, backtest правила
- **`docs/roadmap.md`** — планы (Xnn, осцилляторы, liquidity cycles), плановые изменения
- **`docs/rules.md`** — расширенные правила (метрики MM, MM cap, fees & slippage, leverage caps per-bot, copyright, notifications, и т.д.)

## КЛЮЧИ И АДРЕСА

### Mainnet (production)
- **HYPERLIQUID_ACCOUNT_ADDRESS**: `0x100B03683F7A62f32fB6ab276eAe06529505f506` (Rabby)
- **HYPERLIQUID_AGENT_PRIVATE_KEY**: только в `.env` на VPS, никогда в чате/git
- **NETWORK**: mainnet (на всех ботах)
- **Hyperliquid UI**: https://app.hyperliquid.xyz

**GitHub repo**: https://github.com/takinanton/hyperliquid_bot — **PRIVATE**. Auth через PAT в git remote URL на VPS.

## ПРИОРИТЕТЫ (юзер явно указал)
1. **Время юзера** (главное) — не дискутировать, делать через свои инструменты
2. **Удобство** — минимум когнитивной нагрузки
3. **Деньги** — оптимизация cost вторична
