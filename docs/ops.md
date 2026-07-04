# Ops — VPS, Deploy, Scripts

## 4 МАШИНЫ ДЛЯ ТЕСТОВ

| Машина | CPU | RAM | Параллелизм | Доступ | venv |
|---|---|---|---|---|---|
| **wsl-pc** (Windows+WSL) | **20** | **24GB** | до 15 | `ssh wsl-pc` | `~/venv/bin/python3` |
| **Mac M4 Max** | 14 | 36GB | до 7 (75% cap) | direct | `/Users/ak/Desktop/HL/hyperliquid_bot/venv/bin/python3` |
| **kraken-bot** (Snel NL) | 4 | 8GB | до 3 (1 ядро prod) | `ssh kraken-bot` | `/root/hyperliquid_bot/venv/bin/python3` |
| **hl-bot** (Lightsail Tokyo) | 2 | 1GB | до 1 | `ssh hl-bot`, `sudo` | `/root/hyperliquid_bot/venv/bin/python3` |

**Суммарно**: до **26 параллельных задач**.

**Приоритет**: wsl-pc → Mac → Snel → hl-bot. Самые тяжёлые → wsl-pc (20 ядер).

**wsl-pc IP нестабилен**: при `wsl --shutdown` или Windows reboot WSL IP меняется. Сейчас port forward 192.168.110.211:22 → 172.25.253.86:22. TODO: настроить mirrored networking mode (`~/.wslconfig` → `[wsl2] networkingMode=mirrored`).

## SSH ALIASES

```bash
ssh hl-bot         # <HOST> (Lightsail Tokyo), user=ubuntu, sudo нужен
ssh kraken-bot     # Snel NL, root direct
ssh wsl-pc         # 192.168.110.211, user=ak (за NAT, port forward)
```

`~/.ssh/config` для hl-bot:
```
Host hl-bot
  HostName <HOST>
  IdentityFile ~/.ssh/lightsail-hl.pem
  User ubuntu
```

## DEPLOY ПРОЦЕДУРА

**После каждого `git push` ОБЯЗАТЕЛЬНО** на всех 3 ботах:

```bash
# Pull + restart
ssh hl-bot "sudo bash -c 'cd /root/hyperliquid_bot && git fetch origin main && git reset --hard origin/main && systemctl restart hyperliquid-bot'"
ssh kraken-bot "cd /root/hyperliquid_bot && git fetch origin main && git reset --hard origin/main && systemctl restart kraken-bot"
ssh kraken-bot "cd /root/nado_bot && git fetch origin main && git reset --hard origin/main && systemctl restart nado-bot"

# Verify все на одном коммите
ssh hl-bot "sudo bash -c 'cd /root/hyperliquid_bot && git log --oneline -1'"
ssh kraken-bot "cd /root/hyperliquid_bot && git log --oneline -1"
ssh kraken-bot "cd /root/nado_bot && git log --oneline -1"

# Verify health
ssh hl-bot "systemctl is-active hyperliquid-bot"
ssh kraken-bot "systemctl is-active kraken-bot && systemctl is-active nado-bot"
```

## NADO-SPECIFIC

`.env` должен иметь:
- `NETWORK=mainnet` (default — testnet, опасно для prod!)
- `HYPERLIQUID_AGENT_PRIVATE_KEY=<любое>` (для compat с shared config)

После git reset поле `nado_subaccount` теперь сохраняется (commit `c25c41b`).

## BACKTEST — ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА

### 1. Output ВСЕГДА в файл
```bash
ssh kraken-bot "nohup /path/python script.py > /root/results/<name>.txt 2>&1 < /dev/null &"
```
- `nohup` — не убивается при ssh disconnect
- `> file 2>&1` — output на disk
- `< /dev/null` — clean detach

**НИКОГДА**:
- ❌ `ssh ... python script.py | grep ...` — pipe через ssh, теряется
- ❌ Запуск без записи в файл

### 2. PID verify через pgrep, не `$!`

```bash
nohup venv/bin/python3 script.py > out.txt 2>&1 &
echo $! > /tmp/job_pid.txt
pgrep -af script.py     # ОБЯЗАТЕЛЬНО verify
```

### 3. Smoke test FIRST

`if '--smoke' in sys.argv: 1 pair × 1 variant; sys.exit(0)`. Если smoke 5 сек ОК — full sweep.

### 4. Использовать shared loader

```python
import sys; sys.path.insert(0, '/Users/ak/Desktop/HL/scripts')
from _data_loader import load_4h, load_slippage
from _strategies import generate_all_trades  # patterns_v2 + ema_cross
from _metrics import compute_metrics, apply_mm_cap
```

### 5. Force env (не setdefault)

```python
PROD_ENV = {...}
for k,v in PROD_ENV.items():
    os.environ[k] = v  # FORCE — иначе тест ≠ prod
```

### 6. Все параметры через env (zero hardcoded magic numbers)

```python
INITIAL_BUFFER = float(os.environ.get('INITIAL_BUFFER_PCT', '0.0001'))
STRUCT_BUFFER  = float(os.environ.get('STRUCT_BUFFER_PCT', '0.003'))
```

### 7. ВСЕГДА fees + per-coin slippage

```python
real_rt_cost = fee_rt + 2 * slip_per_side
fee_R = (notional * real_rt_cost) / (account * effective_risk)
pnl_R_net = pnl_R_gross - fee_R
```

| Биржа | fee RT | slippage source |
|---|---|---|
| HL | 0.0009 (0.09%) | `/root/data/hl_slippage.json` per-coin (L2 walk) |
| KF | 0.0010 (0.10%) | `/root/data/kf_slippage.json` |
| Nado | 0.0010 (0.10%) | per-coin |

Default 0.002 (0.2%) если пары нет в json.

## MULTIPROCESSING.POOL НА MAC — НЕ ИСПОЛЬЗОВАТЬ

С heavy bot imports на Mac M4 macOS:
- `fork`: objc[NSMutableString initialize] crash
- `spawn`: процессы стартуют но Pool.imap_unordered блокируется на 0% CPU

**Решение**: sequential (one process) для тестов с heavy bot imports. 55 пар × 3 sec = 3 мин — терпимо. Или `subprocess.Popen` для каждой задачи.

**Можно**: Pool для лёгких задач (data download, slippage compute) без bot.*. Sequential single-threaded — стабильнее всего.

## CPU CAP MAC 75%

```python
N_WORKERS_MAC = 7  # cap, не больше
N = min(int(os.environ.get('N_WORKERS', 7)), 7)
```

## РАСПРЕДЕЛЕНИЕ ТЕСТОВ

При получении N тестов:
1. Самый тяжёлый → wsl-pc (20 ядер)
2. Средние → Mac (cap 7)
3. Лёгкие → kraken-bot (3 cores)
4. Минимальные → hl-bot

**Никогда не очередь** — всегда параллельный fan-out на разные машины.

## MAC LOAD-OFF РЕЖИМ (юзер 2026-05-06)

> "на маке тесты в течение 6 часов не гоняй"

6-часовое окно "Mac не нагружать". Все тесты переносятся на wsl-pc/kraken-bot/hl-bot.
Если тест уже бежит на Mac — kill (даже если близко к завершению).

## OVERNIGHT TASKS — VPS CRON, НЕ MAC

macOS crontab silently drops entries требующие Full Disk Access. ScheduleWakeup session-only — не fire когда Mac в idle.

**Правило**: любая overnight задача → cron на VPS (kraken-bot/hl-bot). Linux cron надёжен.

```bash
# kraken-bot cron:
*/30 * * * * /root/vps_audit.sh > /root/audit_cron_run.log 2>&1
```

## AUTO_MONITOR (Mac)

`/Users/ak/Desktop/HL/scripts/auto_monitor.sh` — bash-демон. Запускается:
```bash
nohup bash /Users/ak/Desktop/HL/scripts/auto_monitor.sh > /tmp/auto_monitor.log 2>&1 &
```

Каждые 30 сек проверяет active tests, kill при CPU<1% за 3+ мин, Mac notification на DONE/FAILED, создаёт UNREPORTED файл-lock.

Проверить жив: `pgrep -af auto_monitor.sh`. Если упал — перезапустить.

## UNREPORTED ФАЙЛ-ЛОК

Watcher автоматически создаёт `/Users/ak/Desktop/HL/data/unreported/<name>.txt` на DONE/FAILED. Файл живёт пока я не отчитаюсь — `bash /Users/ak/Desktop/HL/scripts/ack_test.sh <name>` его удаляет.

`check_tests.sh` показывает большой алерт если unreported. Запускать в начале каждого turn.

## MAC NOTIFICATIONS — на каждый ответ

```bash
osascript -e 'display notification "<краткое содержание>" with title "Claude • HL Bot" sound name "Submarine"'
```

Sounds:
- `Submarine` — обычное (default)
- `Glass` — позитивное (успех)
- `Bottle` — внимание (ошибка/проблема)
- `Sosumi` — критично

Notif — последний tool call перед текстовым ответом.

## STRICT SL VERIFICATION (4 критерия)

После любых изменений на бирже:
1. Coverage ≥ 99% — суммарный sl_size покрывает position size
2. reduceOnly=True на каждом SL ордере
3. Правильная сторона (sell для long, buy для short)
4. Trigger на безопасной стороне mark price

`/root/scripts/verify_kf_sl.py` (TODO создать) — standalone проверка для cron.

## END-TO-END TRACE (НЕ grep-and-assume)

При утверждении "бот делает X":
1. Найти где X **вычисляется** (calc/declaration)
2. Найти ВСЕ usages downstream
3. Дотрассировать до **actual exchange API call** (place_order)
4. LIVE evidence: journalctl logs, trades.db, fetch_open_orders
5. ТОЛЬКО ТЕПЕРЬ можно говорить "бот делает X"

Если не можешь дотрассировать — сказать точно: "В коде есть calculation X, **НО** я не проследил что используется для ордера. Возможно calc-only".

## DB SIZE != EXCHANGE SIZE

При анализе trades:
1. **Group by coin+direction** прежде чем считать PnL/WR (cross-margin merge)
2. **Use exchange position size** (не DB size) для real risk
3. **Real slippage** = (exit_px - SL_at_close) / SL — не (DB_size_loss - planned)

DB size = INTENDED, exchange = FILLED. При partial fill / liquidity cap реальная позиция МЕНЬШЕ.

## КАК ЗАПУСКАТЬ ПАРАЛЛЕЛЬНО НА 4 МАШИН

```bash
# Mac
caffeinate -is nice -n 5 /path/python A.py > /tmp/A.log 2>&1 & echo $! > /tmp/A.pid
caffeinate -is nice -n 5 /path/python B.py > /tmp/B.log 2>&1 & echo $! > /tmp/B.pid
# wsl-pc
ssh wsl-pc "nohup ~/venv/bin/python3 ~/scripts/C.py > ~/results/C.log 2>&1 & echo \$!"
# kraken-bot (Snel)
ssh kraken-bot "nohup /root/hyperliquid_bot/venv/bin/python3 /root/scripts/D.py > /root/results/D.log 2>&1 & echo \$!"
```

После запуска — сразу вернуть юзеру PID list + ETA каждого.

## НА МАК — PRIORITY DEV

> "этот мак основной будет. здесь мы будем допиливать бота, а потом ты без моего напоминания будешь обновлять боты на ВПС"

Цикл:
1. Правки кода → Mac `/Users/ak/Desktop/HL/hyperliquid_bot/`
2. Тестирование на Mac через `/Users/ak/Desktop/HL/scripts/`
3. Готово → git push + ssh deploy на каждом боте (БЕЗ напоминания)
4. Verify systemctl status
5. Mac notification

## VERIFY_CONFIG.PY (drift detection)

`scripts/verify_config.py` — periodic checker (cron daily 00:05 UTC):
- Reads CLAUDE.md/docs/strategy.md PRODUCTION CONFIG секцию
- Reads `/root/{hyperliquid,kraken_margin,nado}_bot/.env`
- Diff каждый параметр
- Drift → alert (separate channel или commit log)
