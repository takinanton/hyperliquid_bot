# Лог разработки — Multi-Exchange Pattern Bot

Хронологический лог инцидентов, фиксов, и решений юзера. Сверху — свежие.

## 2026-08-04 — Funding-арбитраж: сканер 45 бирж, замер по BTC/ETH/SOL

Юзер: «найти биток эфир или солану, чтобы был такой большой доходный стабильный
арбитраж, пусть и без страховки чисто в одну сторону; посмотреть по всем 50 биржам».

**Ответ по существу: на BTC/ETH/SOL такого нет.** Замеры (`scripts/funding_arb_scan.py`):

| монета | кросс-venue спред (gross) | net после комиссий, hold 30d | ранг среди 213 монет |
|---|---|---|---|
| BTC | 4.77% APR | 2.45% APR | 113/213 |
| ETH | 6.18% APR | 3.87% APR | 109/213 |
| SOL | 4.70% APR | 2.27% APR | 114/213 |

Медиана по всем монетам 7.13% → мажоры **ниже** медианы. Большие спреды есть
только на неликвиде (ACE 167%, JELLY 163%, STABLE 142%, GRIFFAIN 108% APR).

HL funding history 365d (8760 часов): BTC mean 6.34% APR / median 10.95% (= базовая
ставка HL, т.е. половину времени премии нет) / положителен 81.9% времени; ETH 6.50%
/ 83.0%; SOL 0.41% mean при median 6.07% — один час −1797% APR стёр весь год.

Одноногий carry vs риск: BTC ann.vol 43.3%, средний дневной ход 1.62% → **93 дня**
carry = один средний день цены. ETH 135 дней. Без хеджа это не арбитраж.

**NEW `scripts/funding_arb_scan.py`** — 5 режимов: `venues` (авто-дискавери 45 бирж
с perp+funding через ccxt 4.5.70, включая pacifica/nado/extended/lighter/aster/
paradex/backpack/hibachi/grvt/dydx/apex), `snapshot`, `history --days N`,
`one-leg --venue X --coins all`, `hl-multi` (все монеты HL × HL/Binance/Bybit одним
запросом через `predictedFundings`). Нормализация APR по interval_h, комиссии из
`market['taker']×2` с fallback на таблицу из `docs/rules.md`, амортизация по
`--hold-days`, метрика стабильности = % интервалов одного знака + худшее 7д окно.

Pair-математика (спред двух venue с разными интервалами) проверена на синтетике —
`scripts/_test_funding_arb_math.py`: 1ч-venue 10.95% APR vs 8ч-venue 5.475% APR →
спред 5.475%, net после 0.19% RT за 30d = 3.163%, худшее 7д на флип-серии ниже
среднего. Из песочницы Claude полный скан невозможен: egress-policy пропускает
только `api.hyperliquid.xyz`, остальные 44 venue → `403 CONNECT`. Полный прогон —
с VPS, команды в `docs/arbitrage.md`.

## 2026-05-12 — IB bot: FX и shorts вырезаны (rip-and-rebuild)

Юзер: «из ай би боты вырезаем торговлю форексом, сфд на форекс и фьючи на форекс. вырезаем так же все шорты чего угодно».

Полный rip (не env-toggle). Удалено в 8 production файлах:
- `bot/exchange_ib.py`: `_FX_FUT_MAP`, FX micros, FX hint branch, FX CFD fallback, CFD case в `_floor_size_for_ib`
- `bot/main.py`: `if settings.long_only:` блок → hardcoded `if settings.exchange == "ib":` (no exempt)
- `bot/config.py`: поля `long_only` + `long_only_exempt_sec_types`, env-парсинг
- `bot/trader.py`: `MIN_SL_DIST_PCT_FX`, is_fx branch в liquidity sizing
- `bot/sessions.py`: CASH/CFD branch (FX 24/5 routing)
- `scripts/build_ib_universe.py`: FX_PAIRS, FX futures (6E/6J/6B/6A/6C/6S/6N), currency ETFs
- `data/ib_universe.json`: 6363 → **6319** entries (by_type: future 45, stock 6274)
- `data/ib_universe_top1000.json`: 1037 → **1000** entries (stocks only)
- `.env`: `LONG_ONLY=true` + `LONG_ONLY_EXEMPT_SEC_TYPES=CFD`

Deploy 10:28 UTC, bot restarted, 35 long stock positions сохранены, 0 shorts, 0 FX в open state.

Backup: `data/manual_ops/2026-05-12_ib_rip_fx_shorts/` (Mac + VPS). См. `project_ib_rip_fx_shorts_2026_05_12.md`.

## 2026-05-11 — IB bot full deployment: live, $100 risk, multi-source data, CFD-routed FX

**Trigger**: юзер «давай сразу делаем норм бота — потом бота вернуть не могли». Single-day end-to-end deploy: empty VPS → live bot trading 6,364 instruments.

**Final state** (account U16531055, NLV $240,149):
- Universe: **6,274 US stocks** + **52 CME/CBOT/NYMEX/COMEX futures** + **38 FX CFDs**
- Risk per trade = **$100** (`RISK_PER_TRADE=0.000417`)
- TF 4h+1h, long_only with `LONG_ONLY_EXEMPT_SEC_TYPES=CFD` (FX bidirectional)
- Pyramid R≥1, max 3 levels, excluded triangle_short/123_short
- LIQUIDITY_MIN_4H_USD=$1M (bypassed for CASH/CFD)
- VIX sizing on (×0.5 high, ×1.5 low)
- Session gate STK/FUT/CFD-aware (RTH cutoff 30min, Globex 23/5, FX 24/5)

**Architecture (новое)**:
1. **yfinance offload**: ВСЕ historical bars (1h/4h/1d × 6,364 symbols) идут через yfinance с disk-кэшем (`data/yf_cache/<interval>/<sym>.parquet`). IBKR socket port 4001 зарезервирован под execution + position queries (5-10 req/min steady-state vs 160 если всё через IB). Permits IBKR-pacing budget остается чистым для latency-critical operations.
2. **Auto-universe** (no manual enumeration): `scripts/build_ib_universe.py` daily cron 00:05 UTC. Источники: NASDAQ+NYSE+AMEX symbol files (rreichel3/US-Stock-Symbols GitHub mirror) + 52 CME/CBOT/NYMEX/COMEX futures roster + 38 IDEALPRO FX pairs + 691 ETF roster (toggled OFF). Crypto regex filter.
3. **Universe-type hints**: `bot/exchange_ib.py::_TYPE_HINTS` loaded at IBClient init from `data/ib_universe.json`. Resolves ticker collisions — ES=Eversource Energy stock OR E-mini S&P future, universe disambiguates.
4. **Session-aware gate**: `bot/sessions.py::is_session_open(sec_type)` — RTH for STK with pre-close cutoff, Globex for FUT, 24/5 for CASH/CFD.
5. **FX via CFD**: routed `Contract(secType='CFD', symbol=base, exchange='SMART', currency=quote)` — 30:1 ESMA leverage on majors, LONG+SHORT, cross-pairs work. Replaces useless spot Forex (cash conversion no leverage, shorts blocked).

**Broker-side blockers (deep-checked + worked around)**:
| product | blocker | resolution |
|---|---|---|
| ETFs (691) | PRIIPs KID missing (EU retail) | `INCLUDE_ETFS=False` until user reclassify as Professional |
| Futures | first-trade token verify | User entered token `057205` in Client Portal → verified live via ES LMT ✓ |
| FX shorts/crosses (spot) | currency-leverage permit | Route through CFD instead (no leverage permit needed) |
| Leveraged ETPs (UVXY/TQQQ) | Complex permit | dropped from ETF_ROSTER |
| Live market data (stocks/futures) | not subscribed | bot uses yfinance bars; mark_price fallback to last cached close |

**Files added/changed**:
- NEW `bot/data_source.py` — `YFinanceSource.candles()` + `warmup_batch()`, disk+mem cache, batch yf.download
- NEW `bot/sessions.py` — `is_session_open()` for STK/FUT/CASH/CFD
- NEW `scripts/build_ib_universe.py` — universe builder with feature flags (`INCLUDE_ETFS/FUTURES/FX_CROSSES`)
- NEW `scripts/warmup_yf_cache.py` — batch warmup for cold-start
- MOD `bot/exchange_ib.py` — full IBClient: type-hints, CFD routing for FX, `_FUT_EXCH` map, available_coins() from JSON
- MOD `bot/config.py` — IB Settings fields, `long_only_exempt_sec_types: frozenset`, IB branch in `_coins_from_env`
- MOD `bot/main.py` — liquidity-bypass for sec_type in (CASH, CFD); long_only sec_type-aware
- MOD `bot/trader.py` — session gate в execute_signal; FX-class liquidity sizing bypass
- MOD `bot/exchange_factory.py` — register `ib`/`extended`/`pacifica`

**Infrastructure**:
- VPS `ib-bot` (AWS Lightsail us-east-1, <HOST>), 2vCPU/8GB/154GB
- systemd: `ib-gateway.service` (IBC 3.23 + Xvfb headless, Java 21) + `ib-bot.service` (depends on gateway)
- IB Gateway 10.46 with `AutoRestartTime=23:55`, 2FA auto-relogin
- Daily cron 00:05 UTC rebuilds universe
- Account creds in `/home/ubuntu/ibc/config.ini` (chmod 600), populated via `/home/ubuntu/set_ib_creds.py`

**Deploy method**: rsync from Mac (no git push on Mac — `gh` CLI installed, OAuth working, but origin/main has 2 unrelated commits from kraken-bot session; pushed to `ib-bot-deploy-2026-05-11` branch instead; merge to main later session).

**Verify** (full audit pre-RTH):
- whatIf LMT order via adapter routing: AAPL/NVDA stocks ✓, ES/MES/GC futures ✓, EUR.USD CFD LONG ✓, **EUR.USD CFD SHORT ✓**, GBP.AUD cross ✓, USD.MXN exotic ✓
- type-hints loaded: 6,364 ✓ (resolves collisions ES/CL/SI/NG/HG → stocks per universe)
- session gate: STK pre-RTH 09:21 ET (closed), FUT/CASH/CFD open
- Settings: risk_per_trade=0.000417 ($100), long_only=True, exempt={CFD}, pyramid R≥1 max 3
- IBKR account: NLV $240,149, AvailableFunds $139k, BP $929k, MaintMargin $90k от COIN existing — все cushion fine
- yf cache: 12,442 parquet files (~4,150 unique symbols, 66% warmed)

**Rollback**: revert commits in `ib-bot-deploy-2026-05-11` branch, OR flip feature flags in `build_ib_universe.py` to True/False to scope what's in universe.

**Открытые задачи (future sessions)**:
1. Реклассифицировать как Professional → unlock 691 ETFs (IBKR Client Portal)
2. CME market data sub ($25/mo) — точная live mark_price для open futures positions
3. NYSE/Nasdaq snapshot ($1.50/mo) — same для stocks
4. Merge `ib-bot-deploy-2026-05-11` в `origin/main` после resolve HL session conflicts
5. Monitor first trades (RTH open at 13:30 UTC)

**Memory**: `manual_ops/2026-05-11_ib_bot_full_deployment.md` (этот файл) + `project_ib_bot_full_deployment_2026_05_11.md` в memory dir.

**Git branch**: `origin/ib-bot-deploy-2026-05-11` (head `381ad45`, 16 commits)

---

## 2026-05-09 ~22:35 WITA — Pyramid feature LIVE на 4 ботах (HL/KF/Nado/Pacifica)

**Trigger**: юзер «сделай на всех ботах возможность добора позиции» → backtest HL/KF (pyramid_replay_*.py в /tmp) → deploy с моими поправками.

**Что**: при `unrealized_R >= 1.0R` от LAST entry в той же `(coin, tf, dir)` бот разрешает повторный вход; total levels capped 3; risk per level flat = `RISK_PER_TRADE`; каждый level — свой vstop. PYRAMID_EXCLUDED_PATTERNS = `{triangle_short, 123_short}`.

**Backtest justification (на live VPS .env snapshot)**:
- HL 178d / 22c / 1h: trades 155 → 198 (+28%), net_R 75.04 → 91.06 (+21%), return +52.7% → +65.5%, max_DD 13.0% → 21.5% (×1.65); addons 44 (lvl1=34, lvl2=8, lvl3+4=2); avgR initial 0.49, avgR addon 0.34.
- KF 304d / 230c / 4h: trades 306 → 374 (+22%), net_R 323.6 → 388.0 (+20%), max_DD 24.2% → 35.6% (×1.47); addons 84 (lvl1=63, lvl2=13, lvl3-5=8); avgR initial 1.09, avgR addon 0.85.

**Поправки vs юзеровских изначальных «unbounded + flat 1%»**:
1. MAX_PYRAMID_LEVELS=3 (lvl4-5 в backtest нулевой R).
2. MAX_DRAWDOWN_PCT_HALT=20 на HL/Nado/Pacifica, 30 на KF — DD halt restored (memory: project_dd_halt_removed → re-introduced gated by setting).
3. PYRAMID_EXCLUDED_PATTERNS = triangle_short, 123_short (avgR addon < initial в backtest).

**Изменённые файлы**:
- `bot/config.py` — Settings + `PYRAMID_EXCLUDED_PATTERNS`
- `bot/journal.py` — ALTER TABLE миграция + `open_levels_for_tf_dir` + `insert_trade(pyramid_level, parent_trade_id)`
- `bot/trader.py` — pyramid trigger gate перед `has_open_trade_for_tf_dir` skip
- `bot/main.py` — `_check_drawdown_halt` + call после manage_open_positions
- `bot/prod_gates.py` — registers `pyramid_addon_trigger`, `dd_halt`

**Deploy method**: idempotent Python patcher `/tmp/apply_pyramid_patch.py` (try-multiple-anchors). Mac source has uncommitted state, не пушил через git. Pacifica и Nado — отдельные копии, patches применились на них тоже. IB — code не deploy'нут ещё, patcher применится при первом deploy.

**Verify**:
- 4/4 systemd active
- Settings.pyramid_enabled=True везде
- DB schema (pyramid_level, parent_trade_id) во всех 4 trades.db
- 90s journalctl без ERROR/Traceback

**Rollback**: backup `.env.bak.<TS>` на каждом боте; либо `PYRAMID_ENABLED=false` + `MAX_DRAWDOWN_PCT_HALT=0`.

**Manual ops**: `data/manual_ops/2026-05-09T143433Z_pyramid_feature_deploy.md`

## 2026-05-09 ~15:30 WITA — IB bot (4й инстанс): infra, code, paper setup; ждём активации (Mon 12 May)

**Trigger**: юзер «давай теперь напишем бота для IB» → длинная сессия исследования + setup.

**Infra (новый Lightsail us-east-1, 2vCPU/8GB)** — `ib-bot` (<HOST>). Latency NYSE 0.99ms. Stack: Java 21 + Python 3.12 + IB Gateway 10.45 + IBC 3.23 + Xvfb headless. SSH config alias `ib-bot` в `~/.ssh/config` мака.

**Code** (deployed):
- `bot/exchange_ib.py` — новый adapter ib_insync (~360 LOC). Stocks (SMART) + index/commod/bond/agri/fx-fut + crypto-fut с per-instrument multipliers
- `bot/exchange_factory.py` — branch `EXCHANGE=ib`
- `bot/config.py::Settings` — opt-in fields (defaults no-op): `long_only`, `liquidity_min_4h_usd`, `vix_*`, `ib_*`. Plus `_warn_duplicate_env_keys` guard на load.
- `bot/external_data.py` — `get_vix_close` через yfinance (1h TTL), `vix_size_multiplier`
- `bot/main.py::_collect_signals_for_tf` — liquidity floor + long_only filters
- `bot/trader.py` — VIX multiplier в risk_mult chain
- `scripts/honest_replay_hl.py` **bug fix**: добавлен `detect_ema_cross` после `detect_all_v2` (mirror prod; pre-fix backtests undercount EMA trades). + enriched fields в save (`higher_trend`, `vol_4h_usd`, `atr_pct`).

**Backwards-compat**: все новые env-keys default disabled. HL/KF/Nado/Pacifica поведение не меняется.

**4h prod config (`ib-bot:/root/hyperliquid_bot/.env`)**:
- `EXCHANGE=ib`, `WORKING_TFS=4h`
- `LONG_ONLY=true` ← юзер «1 и 4ч на всех рынках только лонг» (revert от 05-08 purge — для IB новое решение, crypto-bots не задеты)
- `MIN_RR=1.3` (sweep avgR +0.488 → +0.525)
- `LIQUIDITY_MIN_4H_USD=1000000`
- `VIX_SIZING_ENABLED=true` (`>25 → ×0.5`, `<14 → ×1.5`)
- Удалены за избыточностью: `MIN_RR_COUNTERTREND`, `SKIP_FLAG_SHORT`

**Backtest** (2y, 171 ткр, IB costs, MM cap=50%, anti-dup, LEV=10): final config — 353 trades / WR 43.0% / **avgR +0.565 / ΣR +202 / equity $50k→$211k (+322%) / mdd 14.0% / mean MM 27.9%**.

**Vstop sweep** (Binance 8.7y, 932 trades): scale-invariant params все в ±4.5%. Scale-dependent wider (k=7×lag=7×matr=2.0) → +4.6% ΣR но **equity ÷2.4** (compounding loss). Текущий vstop (`STRUCT_BUFFER_PCT=0.003, VSTOP_ATR_MULT=2.5`) оптимален. Не трогаем.

**IBC setup gotchas** (несколько часов диагностики):
- IBC default ищет `/opt/ibc/scripts` → symlink `/home/ubuntu/ibc → /opt/ibc` + `chmod +x scripts/*.sh`
- IBC ждёт Gateway в `/root/Jts/ibgateway/${VERSION}/` — `cp -r /home/ubuntu/ibgateway/* /root/Jts/ibgateway/1019/` + **отдельно `cp -r .install4j`** (glob `*` skip dotfiles)
- IBC config — `/root/ibc/config.ini` (default), `/etc/ibc/config.ini` игнорируется
- Java path: IBC bug `which java` returns file `/usr/bin/java` → check `$path/java` fail. Fix: создать `/root/Jts/ibgateway/1019/.install4j/inst_jre.cfg` с **dir** `/usr/lib/jvm/java-21-openjdk-amd64`

**Status**: Gateway code path работает (java/jars/vmoptions all found, login dialog reached). Login exits 1107 = paper account `DUQ913627` (username `knp419419419`) **не активен** до понедельника (IBKR review cycle). После активации systemd auto-restart Gateway → bot стартует автоматически.

**Open**:
- Sub-account U25671633 (additional account application submitted; 1-3д IBKR review) — для 1h-бота на отдельном acct (mirror config + `WORKING_TFS=1h` + `IB_GATEWAY_PORT=4001` после live switch)
- **Prod bug discovered**: `bot/trader.py:1080` — `tf="4h"` хардкод в `manage_open_positions` для всех trades включая 1h-trades. Затрагивает все 4 crypto-бота post-2026-05-08 multi-TF rollout. Fix отложен до отдельного апрува.

**Refs**: memory entry `project_ib_bot_4h_setup_2026_05_09.md` (full detail).

## 2026-05-09 08:23 WITA — RISK_PER_TRADE drift fix (HL: 0.02 → 0.01)

**Trigger**: юзер «тг бот фигню гонит» → разбор nightly_report 2026-05-09 (HL "3 opens / volume $456k"). Не bug отчёта: реальная BTC long #164 size 5.59 / notional $445.7k / risk_dollars=$1,513 (3.4% equity), `pattern=123_long`, 1h. Юзер: «у нас 1%, не 2%».

**Drift discovery**: manual_ops 2026-05-08 заявлял apply 2% на все 3 бота, но реально на VPS было `HL=0.02 / KF=0.01 / Nado=0.01` — KF+Nado каким-то откатом вернулись на 1% между 05-08 и 05-09 без manual_ops файла.

**Action**: `sed -i 's/^RISK_PER_TRADE=.*/RISK_PER_TRADE=0.01/' /root/hyperliquid_bot/.env` + `systemctl restart hyperliquid-bot.service`. Verify: `RISK_PER_TRADE=0.01`, service `active`, journalctl чисто, `Account value: $40965.52`.

**Existing BTC #164** не закрываем (per `feedback_paper_pnl_no_manual_close`), бот доведёт до SL/TP.

manual_ops: `data/manual_ops/2026-05-09_risk_per_trade_drift_fix.md`.

## 2026-05-09 02:13 WITA — BTC regime filter включён на всех 3 ботах (HIP-3 exempt в коде)

**Trigger**: юзер «вруби на всех трех. выруби только на xyz/hip3». До этого filter был фактически inert на всех:
- hl-bot: `BTC_REGIME_TICKER=` пустой
- kraken-bot, nado-bot: были две строки в `.env` — `BTC_REGIME_TICKER=BTC/USD:USD` (стр.51) и `BTC_REGIME_TICKER=` (стр.57). dotenv last-wins → пустая строка переопределяла → filter неактивен.

**Action**: на каждом VPS — `sed -i '/^BTC_REGIME_TICKER=/d'` + `echo` чистой строки:
- hl-bot (`/root/hyperliquid_bot/.env` через sudo): `BTC_REGIME_TICKER=BTC`
- kraken-bot, nado-bot: `BTC_REGIME_TICKER=BTC/USD:USD`

**HIP-3 exempt**: уже в `bot/risk.py::is_btc_regime_blocked` — early-return для `coin.startswith("xyz:")`. Никаких отдельных env-knobs не нужно.

**Restart**: `systemctl restart hyperliquid-bot|kraken-bot|nado-bot`. Все три active, journalctl чисто.

**Effect**: short-паттерны из `_BTC_BULL_BLOCKED_PATTERNS` теперь блокируются когда BTC 4h close > EMA200 (только crypto perps; HIP-3 stocks/forex/commodities идут как и шли).

manual_ops: `data/manual_ops/2026-05-09_btc_regime_filter_enable.md`.

## 2026-05-08 21:30 WITA — Multi-TF (4h+1h) deployed на ВСЕХ 3 ботах + cleanup

**Trigger**: после 8.7y Binance backtest (`/tmp/tf_sweep/results_long/`) — добавление 1ч поверх 4ч даёт ×2.6 капитала за 6 мес. при +1.2pp DD. Юзер: «погнали делать».

**Code changes** (Mac → 3 VPS sync):
- `bot/config.py`: `WORKING_TFS` list + `HIGHER_TF_MAP` (auto-mapping, override `WORKING_HIGHER_PAIRS`); removed `Settings.max_consecutive_losses` field.
- `bot/main.py`: `_collect_signals` → multi-TF wrapper; new `_collect_signals_for_tf(working_tf, higher_tf)`; HIP-3 на 1ч ВКЛЮЧЕН (юзер: «хип3 очень важен»). `_process_coin` (dry-run) тоже multi-TF.
- `bot/risk.py`: removed `circuit_breaker_tripped()` + `consecutive_losses` import.
- `bot/journal.py`: removed `has_open_trade_for(coin)` (single-coin lock полностью); removed `consecutive_losses()`. Added `has_open_trade_for_tf_dir(coin, tf, direction)` — anti-dup на same TF/dir, cross-TF (4h+1h на BTC long) разрешён.
- `bot/trader.py`: lock check заменён на anti-dup (`has_open_trade_for_tf_dir`).
- `bot/notifier.py`: removed `check_consecutive_losses()`.
- `bot/prod_gates.py`: removed Stage="circuit_breaker", `id="max_consecutive_losses"` gate (registry → 25).
- `bot/exchange_kraken.py`: bar-aligned `_candles_cache` + min-TTL gate + staggered offset (mirror HL); `_funding_cache` 60s TTL (без него 560 funding fetches/cycle = 4-5 мин). Added `last_4h_volume_usd()` метод (фиксил `'KrakenClient' object has no attribute` warning).
- `bot/exchange_nado.py`: added `last_4h_volume_usd()` — теперь `liquidity_cap_notional` работает на Vertex/Nado тоже.
- `.env.kraken.example`: removed `MAX_NET_POSITIONS=30`, `MAX_CONSECUTIVE_LOSSES=10`.

**Env changes** (3 VPS):
- KF, Nado, HL: `WORKING_TFS=4h,1h`, `WORKING_HIGHER_PAIRS=4h:1d,1h:4h`
- KF & Nado: `RISK_PER_TRADE=0.02 → 0.01` (юзер: «1% на все таймфреймы»)
- HL: `RISK_PER_TRADE=0.02` оставлен

**Verify** (15 мин после deploy):
- KF: 950 log lines, 9 SIGNAL fired, 157 SKIP anti-dup, 0 errors, 0 × 429.
- Nado: 495 ADAPTIVE liquidity scale-down (последний 4h_volume_usd работает!), 356 REJECT LIQUIDITY, 0 errors.
- HL: 132 WS subscribe (72×4h + 60×1h), 3 SIGNAL fired, 10 SKIP anti-dup, 4 × 429 (retry handled), 0 exceptions.

**Detail**: `data/manual_ops/2026-05-08_multi_tf_deploy.md`

**Open**:
- Trail-merge для cross-TF positions (когда 4h+1h на одной монете): сейчас бот ставит SLs независимо, последний put_stop wins ≈ merge-tight. Симуляция показала +18 R / 8 лет vs independent. Оставлено.
- Add-to-position (добор) — юзер: «ладно бы добрал, добор можно». SKIP пока, отдельная фича.
- 15m TF тестирование на komp ещё крутится.

**Refs**: 7 bot files × 3 VPS, +`exchange_kraken.py` cache, +`exchange_nado.py` last_4h_volume_usd.

---

## 2026-05-08 14:55 WITA — LONG_ONLY полностью выпилен (env + код + harness'ы + memory)

**Trigger**: юзер «LONG_ONLY=true удали на всех биржах. оставь фильтры против тренда не торговать» → потом «дальше фраза про лонг онли должна вообще пропасть чтобы ты не цеплялся».

**Контекст**: KF 4y backtest показал 466 long / 0 short trades — `LONG_ONLY=true` резал шорты на всех 3 ботах. Юзер решил снять directional restriction и оставить counter-trend filtering через soft RR (`MIN_RR_COUNTERTREND=1.1`).

**Purge scope**:
- **`.env` на 3 ботах**: `LONG_ONLY=` строка удалена sed'ом (HL, KF, Nado)
- **`bot/main.py`**: блок `if os.getenv("LONG_ONLY", ...): signals = [s for s ... != 'short']` удалён
- **Harness'ы**: `scripts/honest_replay_hl.py`, `hyperliquid_bot/scripts/honest_replay_kf.py`, `hyperliquid_bot/scripts/honest_replay_nado.py`, `scripts/replay_confirmation_compare.py` — переменная `LONG_ONLY` + gate-skip + banner print + JSON output field removed. Везде остался комментарий `# 2026-05-08: LONG_ONLY purged`.
- **Memory**: `feedback_backtest_sanity_directions_count.md` удалён, MEMORY.md ссылка убрана. `project_open_followups_2026_05_07.md` items 1/6/8 — LONG_ONLY references переписаны на «RESOLVED».

**Counter-trend filter — что осталось**:
- `MIN_RR_COUNTERTREND=1.1` на всех 3 .env (требует RR≥1.1 для counter-trend signals vs RR≥0.9 для тренда)
- `is_btc_regime_blocked()` filter в bot/risk.py (если `BTC_REGIME_TICKER` set)

**Deploy**:
- `scp main.py` на 3 VPS (HL via /tmp + sudo cp; KF/Nado direct root)
- `sed -i "/^LONG_ONLY=/d"` на каждом .env
- `systemctl restart hyperliquid-bot/kraken-bot/nado-bot` параллельно

**Verify**:
- HL/KF/Nado: active, 0 errors/traceback ✓
- `grep -c "LONG_ONLY"` на всех VPS-файлах = 0 ✓
- Local `grep -rn "LONG_ONLY"` = только memorial комментарии (не код)

**Refs**: `bot/main.py` (-7 LOC), 4 harness'а (-4-12 LOC each), 1 memory file deleted, 2 memory files edited.

**Open**: следующий 4y KF backtest (с full universe rsync) покажет новый baseline trade count + avgR с шортами включёнными.

---

## 2026-05-08 14:30 WITA — MACD filter v2 RIP — −17% avgR HL, −30% avgR KF на актуальном config

**Trigger**: только что (14:00) добавил MACD filter в 3 backtest harness'а как «applied gate». При тестировании MACD on vs off обнаружил: на VPS `MACD_FILTER_ENABLED=false` во ВСЕХ 3-х `.env` (без записи в логе — кто-то выключил тихо). Прогнал full бектест с MACD on vs off на каждой бирже — фильтр **отрицательный** на HL/KF.

**Findings**:

| Биржа | n trades (on/off) | avgR ON | avgR OFF | Δ |
|---|---|---|---|---|
| HL  | 70/72  | +0.607 | +0.730 | **−17%** |
| KF  | 109/116 | +1.190 | +1.694 | **−30%** |
| Nado proxy | 57/61 | +0.414 | +0.328 | +26% (HL data, шум) |

Лог 2026-05-06 commit `0eee45a` обещал «HL +8% avgR, +38% CAGR». Те результаты были на старом config: `MAX_NET_POSITIONS=12/30`, `Premium tier ×2.5`, `COOLDOWN_HOURS`, без `liquidity_tier_mult`. К 2026-05-08 все эти gates удалены/изменены, и MACD филтрация теперь режет хорошие trades в trending bull market.

**Rip** (per `feedback_rip_and_rebuild`):
- `bot/main.py:464-492` — удалён весь MACD-блок (try/except с `ema_fast/ema_slow/macd_line/macd_signal/macd_hist` и filtered loop).
- `bot/prod_gates.py:140-147` — удалён `Gate(id="macd_filter", ...)` entry.
- `scripts/honest_replay_hl.py`, `hyperliquid_bot/scripts/honest_replay_kf.py`, `hyperliquid_bot/scripts/honest_replay_nado.py`: удалён `macd_filter` из `APPLIED_GATES`, `MACD_FILTER_ENABLED` env-read, MACD-блок в `scan_coin/gen()`. Nado также удалён `macd_filter_enabled` из output JSON.
- `scripts/_backtest_harness.py:363`, `scripts/harness_full_sweep.py:30` — удалены `'MACD_FILTER_ENABLED': 'true'` из mock PROD_ENV.
- `data/backtest_params_2026-05-07/envs/{hl,kf,nado}.env` — удалена строка `MACD_FILTER_ENABLED=false`.
- `.env` на 3-х VPS (HL `/root/hyperliquid_bot/.env`, KF `/root/hyperliquid_bot/.env`, Nado `/root/nado_bot/.env`) — `sed -i '/^MACD_FILTER_ENABLED=/d'`.

**Deploy** (rsync/scp без commit — gh CLI отсутствует на Mac):
- `bot/{main,prod_gates}.py` → `hl-bot:/root/hyperliquid_bot/bot/` + `kraken-bot:/root/hyperliquid_bot/bot/` + `kraken-bot:/root/nado_bot/bot/`.
- restart 3 services: `hyperliquid-bot` (HL), `kraken-bot` (KF), `nado-bot` (Nado).

**Verify**:
- ✓ Все 3 active за 60s после restart.
- ✓ journalctl `--since '5 min ago'` — 0 errors / Traceback / MACD references на всех 3.
- ✓ harness'ы: parity OK (HL applied=18/exempt=7, KF applied=15/exempt=10, Nado applied=15/exempt=10).
- ✓ `python -m bot.prod_gates --check` — registry clean, нет drifted keys.

**Memory**: новая `project_macd_filter_removed.md`. Index `MEMORY.md` обновлён. Лог-запись 2026-05-06 «MACD filter v2 deployed» теперь устарела — оставлена для истории.

**Refs**:
- `bot/main.py:464` (now blank line after `return []`)
- `bot/prod_gates.py` — Gate registry без macd_filter
- `scripts/honest_replay_hl.py:144-159,189-191,371-372`
- `hyperliquid_bot/scripts/honest_replay_kf.py:115-118,165-176,290-296`
- `hyperliquid_bot/scripts/honest_replay_nado.py:90-93,128-138,247-251,556-563`

---

## 2026-05-08 14:00 WITA — Backtest parity fix: HL harness import + MACD/tier_mult applied in all 3 + Nado harness

**Trigger**: юзер «возьми бота, возьми любой короткий период бектеста на каждой бирже и сравни, так ли он отрабатывает как надо. учитывай только бота без всякого env». Запустил parity check + smoke на каждой бирже.

**Findings (drift between bot vs backtest)**:

1. **HL harness не запускался** — `scripts/honest_replay_hl.py:96` импортил `from bot.vstop import find_struct_vstop_exit`, но модуль `bot.vstop` удалён давно (заменён на `bot.vstop_structure.find_structure_exit` с новой сигнатурой: добавлен `swings` arg). `ModuleNotFoundError` падал ДО `assert_backtest_parity` — drift не детектился. KF harness (`hyperliquid_bot/scripts/honest_replay_kf.py:54`) уже мигрирован к новому API; HL — нет. Незаметно с момента переименования `bot.vstop`.

2. **`macd_filter` в EXEMPT TODO на обеих биржах** — но в проде MACD ON по дефолту (`bot/main.py:469`, MACD_FILTER_ENABLED=true default). 12/26/9 EMA hist; long требует hist>0, short hist<0; EMA-cross паттерны bypass. Backtest без него **переоценивал signal count** vs прод.

3. **`liquidity_tier_mult` в EXEMPT TODO на обеих биржах** — но в проде риск умножается ×0.5–2.0 по 1h-volume tier (`bot/trader.py:270-277`): vol_4h_usd/4 → ≥$2M tier_high → ×2.0; ≥$300k tier_mid → ×1.5; иначе 1.0. Backtest без него **недосайзил BTC/ETH** (×2 в проде), пересайзил thin coins.

4. **Nado harness не существовал** — был только `nado_proxy_acct36784.json` от komp, который был просто прогон `honest_replay_hl.py` с `HL_ENV_FILE=nado.env`. Fee-rt в результате 0.09% (HL), а не 0.10% (Nado). Парити не верифицировался.

**Fix**:
- `scripts/honest_replay_hl.py`: импорт `bot.vstop_structure.find_structure_exit` + `bot.swings.find_swings`, локальный wrapper `find_struct_vstop_exit(...)` (копия из KF harness). + добавлены MACD-фильтр в `scan_coin` (между SKIP_FLAG_SHORT и FORCE_*_COINS, mirrors `bot/main.py:469`), liquidity_tier_mult перед расчётом `rmult` (mirrors `bot/trader.py:270-277`). APPLIED_GATES обновлён: `macd_filter`, `liquidity_tier_mult` перенесены из EXEMPT_GATES.
- `hyperliquid_bot/scripts/honest_replay_kf.py`: то же самое — добавлены MACD + tier_mult.
- `hyperliquid_bot/scripts/honest_replay_nado.py` (NEW): полноценный Nado harness базируется на KF (no HIP-3, no funding gate). FEE_RT=0.0010 (Nado: 0.10% RT vs HL 0.09%). По дефолту использует `hl_1y` как PROXY data; `data_proxy: true` flag в output JSON. `_strip_perp(coin)` для маппинга `BTC-PERP` → `BTC`. `NADO_DATA_DIR` overridable когда native cache подвезут (memory: bot data path = `kraken-bot:/root/nado_bot/data/`).

**Verify (smoke 5 coins, all 3 биржах prod-env)**:

| Биржа | Parity | Trades | Win% | Net R | Equity | DD | Window |
|---|---|---|---|---|---|---|---|
| HL | ✅ 19 applied / 7 exempt | 26 | 30.8% | -3.06 | -8.4% | 11.5% | 167d |
| KF | ✅ 16 applied / 10 exempt | 15 | 26.7% | +23.3 | +32.1% | 13.0% | 156d |
| Nado (proxy) | ✅ 16 applied / 10 exempt | 23 | 30.4% | -2.8 | -7.2% | 10.3% | 146d |

HL/Nado loss: новый tier_mult удвоил риск на BTC/ETH (>$2M vol) → losses амплифицировались. KF profitable — windows перекрываются по-разному, KF попал в строный uptrend Jul-Dec'25.

**Refs**:
- `bot/prod_gates.py` — registry source of truth (без изменений)
- `scripts/honest_replay_hl.py:96-108,156-166,378-396,461-479` — vstop wrapper + MACD env + MACD filter + liquidity_tier_mult
- `hyperliquid_bot/scripts/honest_replay_kf.py:107-119,126-136,170-180,297-313,365-379` — то же самое
- `hyperliquid_bot/scripts/honest_replay_nado.py` (NEW)
- Memory: обновлён `reference_prod_gates_registry.md`

**Open follow-ups**:
- Native Nado data: rsync `kraken-bot:/root/nado_bot/data/` → `data/nado_*` → set `NADO_DATA_DIR` env.
- `funding_blocked` для KF — всё ещё TODO exempt. HL получил его 2026-05-08 (`data/hl_funding/all.json`); KF аналогичный dataset не построен.

---

## 2026-05-08 12:33 WITA — N22: revert retry budgets 5→3 на user_state endpoints

**Trigger**: верификация 04:00 UTC top-of-4h после step 2 deploy показала **8 × ClientError за 17 мин uptime** (vs pre-step-1: 1×/7h). Все 429 — на `spot_user_state`/`user_state`/`open_positions` endpoints, **не candles** (candle 429 уже устранён step 2 WS).

**Root cause**: step 1 (N20/N21) бампнул retry budget 3→5 attempts (3s→15s) на user_state endpoints с целью «пересилить transient bursts». Эффект противоположный:
- 5 retry × 1+2+4+8s exp backoff = бот **блокируется 15s** в одной функции `account_value()`
- 5 × 429 в логе spam'ятся в HL rate-limited window
- HL per-user throttle не успевает recover между retry attempts → следующий цикл снова 429
- При 30s loop interval + 15s retry storm = effective cycle 45s, бот делает в 1.5× меньше итераций

**Fix N22** (deployed 04:33:01 UTC):
- `bot/exchange.py`: `spot_user_state` retry 5→3 (3s budget), `user_state` (perp) 5→3, `open_positions` 5→3.
- Не трогал: candle retries в `candles()` 5 attempts (там работают), `cancel_sl_order` `_retry_429` wrap (order ops редкие).

**Verify** (post-restart 04:33:01 UTC, объединённый с step 2 WS deploy):
- Cold-start bootstrap (04:33:01-04:34:00, 60s): 9 × 429 на REST candle history fetches (200 баров × 60 coins × 2 TF) — ОК, ожидаемо.
- **Steady-state с 04:34:00: 0 × 429, 0 errors/traceback** ✓
- Account $34,455, whitelist 60/302, trail logic работает
- 0 user_state 429 events — revert работает

**Refs**: `bot/exchange.py:600-678` (retries reverted), `data/manual_ops/2026-05-08_revert_retries.md` (TBD).

---

## 2026-05-08 12:00 WITA — Step 2: HL candle REST poll → WebSocket subscribe (0 × 429 in steady-state)

**Trigger**: финальная фаза refactor'а после step 1 (N21). Step 1 убрал 1h-fetch и зажал 4h cap до 30s — но 60 coins × 30s loop = 2 RPS sustained, top-of-4h burst всё ещё ловит ~28 × 429 / 7h. Step 2 = переход с REST polling на WS push, 0 candle REST в steady-state.

**Architecture**:
- `Info(skip_ws=False)` (раньше True) при `HL_WS_CANDLES=true` (default).
- **Lazy subscribe**: `candles()` после первого REST fetch зовёт `_maybe_ws_subscribe(coin, interval)` → подписка идёт естественным образом из coin_universe в `_collect_signals` без изменений main.py.
- **`_on_candle_msg(msg)`** обновляет `_candles_cache` через `_ws_active_bar` буфер: на bar transition append'им last-known close-state предыдущего бара в df, обновляем `current_bar_start`. Thread-safe через `_ws_lock: RLock`.
- **`_ws_watchdog` thread** каждые 30s проверяет `ws_manager.is_alive()`, при death → `_reinit_ws()` (новый WebsocketManager + re-subscribe). Heartbeat лог раз в 5 мин.
- HIP-3 (xyz:*): subscribe идёт одинаково (`name_to_coin` мап через perp_dexs="xyz"); если HL не пушит — REST fallback в `candles()` через TTL/bar-boundary check сохраняет работоспособность.

**Fix** (commit pending — file scp'нут на VPS, deployed 03:56:18 UTC):
- `bot/exchange.py`: +1 import (threading), +5 instance attrs (`_ws_lock`, `_ws_subscriptions`, `_ws_active_bar`, `_ws_disabled`, `_ws_last_msg_at`), +4 method (`_maybe_ws_subscribe`, `_on_candle_msg`, `_ws_watchdog`, `_reinit_ws`), candles() обновлён для capture in-progress bar и lazy WS subscribe. ~120 строк.
- Cache check `cached[0] == current_bar_start` → `>=` (WS может выкатить новый bar до того как offset-clock advance'ится).

**Verify post-restart** (03:56:19 UTC, pid 210151):
- 134 WS subscriptions (60 whitelist × 2 TF + open positions × 4h + BTC) в течение ~150s
- 3,207 WS msgs in first 5 min (avg ~10/s), 132/134 active <10min
- 0 errors / traceback post-restart (1 WARNING — LDO bootstrap 429 retry exhausted, cosmetic)
- **Top-of-4h 04:00 UTC**: 0 × 429 candle requests (vs предыдущий baseline 8 × 429). Boundary прошёл silently через WS push. Trail SL fired для ETC, USUAL, BRETT в 04:01-04:02 — strategy читает freshly-pushed candles из cache.
- Bootstrap window 03:56-03:58 (~150s): 33 × 429 retries (handled), 1 unrecovered (LDO).
- Equity stable $33,996 → $34,303 в течение 5 min.

**Refs**: `data/manual_ops/2026-05-08_step2_ws_migration.md` (+ journalctl.log), `bot/exchange.py:32-47,76-100,197-301,326-484`. Backup pre-step2: `/root/hyperliquid_bot/bot/exchange.py.bak.20260508-step2-deployed`.

**Rollback**: `HL_WS_CANDLES=false` + restart → behaviour ≡ step 1 (skip_ws=True, _maybe_ws_subscribe no-op, watchdog не стартует).

**Open follow-ups**:
- Verify next top-of-4h 08:00 UTC (+4h) — 0 × 429 expected
- 24h stability check: WS reinit cycles? heartbeat active_subs / total ratio
- HIP-3 candle WS push behaviour — наблюдать active_bar staleness для xyz:* (выше 30s → REST fallback, документировать)
- KF/Nado WS migration — отдельный PR, не блокирующий

---

## 2026-05-08 11:39 WITA — N21: выпил 1h fetch + 4h cap → 30s (полное устранение burst)

**Trigger**: юзер про 4h burst fix: «зачем нам вообще 1ч если все решения на 4ч?». 1h-candle fetch использовался только в `trader.py:257` для liquidity check (1 запрос → 5 баров) — атавизм.

**Findings**: всё trading на 4h/1d (`WORKING_TF=4h, HIGHER_TF=1d`). 1h-fetch — только liquidity gauge. Cached 4h volume (уже фетчится в `_collect_signals`) даёт эквивалентный сигнал ликвидности → 0 extra requests.

**Также проверил**: HL public API не имеет батч-endpoint для исторических OHLCV. Доступны батчем только `meta_and_asset_ctxs` (current funding/mark/oi/dayNtlVlm) и `all_mids` (last px). Для true-batch OHLCV — только WebSocket subscribe (шаг 2).

**Fix** (deployed 03:37:29 UTC + tightened 03:39:20 UTC):
- `bot/exchange.py`: добавлен `last_4h_volume_usd(coin)` метод, читает кэш `_candles_cache[(coin, "4h")]`. `_coin_cache_offset_ms` cap 600s/60s → uniform 30s (без 1h-fetches overlap нет).
- `bot/trader.py`: `df1h = client.candles(coin, "1h", 5)` → `vol_4h_usd = client.last_4h_volume_usd(coin)`. Конверсия: `hourly_vol_est = vol_4h_usd / 4.0` для совместимости со старыми порогами в env.

**Verify**: post-restart 0 errors/traceback, account_value $33,997, universe 302/whitelist 60-65 ✓. Real test = top-of-1h (04:00 UTC = +20 min) → ожидаем 0 × 429.

**Refs**: `data/manual_ops/2026-05-08_remove_1h_fetch.md`, `bot/exchange.py:187-194,257-280`, `bot/trader.py:254-279`. Шаг 2 (WebSocket migration) — отдельная dedicated задача (~1-2 дня), не urgent.

---

## 2026-05-08 06:43 WITA — N20: HL 429 burst initial fix (4h cap → 600s + retry budgets)

**Note**: superseded N21 — оставлено для истории. Изначально 4h cap расширен до 600s чтобы decouple от 1h burst, но юзер указал что 4h trade decisions страдают от 10-min lag. N21 решил радикальнее: убрал 1h-fetches → cap можно вернуть к 30s без overlap.

**Trigger**: nightly audit показал 31 × 429 на hl-bot за 7h, кучкуются на top-of-4h (16:00U=16, 20:00U=8). 1 ERROR Traceback `account_value` retry exhausted.

**Fix N20** (intermediate, deployed 22:42-22:43 UTC):
- `_coin_cache_offset_ms`: cap для bar > 1h расширен 60s → 600s.
- `account_value`/`open_positions` retries 3 → 5 (15s budget).
- `cancel_sl_order` обёрнут в `_retry_429`.

**Result**: 00:00 UTC top-of-4h burst = 4 × 429 vs predeploy 16-24 × 429 (-75%). Но 4h candle data до 10 min stale — для prod TF=4h дорого. **N21 superseded**.

---

## 2026-05-08 17:00 WITA — Deep audit (round 2): F-warnings cleanup + .env orphan keys + drift inventory

**Trigger**: юзер ушёл на 2ч, попросил «глубокий анализ всех фейлов, глюков и тп и сам все исправляй без меня».

**Actions (autonomous, deployed)**:
- `bot/notifier.py:72` — F821 `undefined name 'message'` в `daily_summary(self, body)`. Сигнатура использовала `body`, но тело логировало `message`. Fix: используем `body`. Также убран мёртвый `'message' in dir()` guard в `info()`.
- `bot/patterns_v2.py:504` — F841 `direction = "long"` объявлен но не используется (внутри ihns_long detector). Fix: удалена строка.
- `bot/exchange_nado.py:598` — F841 `trig = None` placeholder в try/except для `get_trigger_orders` который завёрнут в `if False else None` (dead branch). Fix: удалена вся dead try/except, оставлен комментарий-placeholder.
- `bot/trader.py:915` — F841 `handled_by_lead: dict` объявлен с комментарием «помечаем какие trade_ids уже обработаны лидером», но никогда не читался — dedup-логика для same-coin групп никогда не была реализована. Verified DB на 3 ботах: 0 legacy duplicate (coin, direction) open trades → безопасно удалить, единственный per-coin guard = `has_open_trade_for(coin)` (since 2026-05-05). Fix: удалён мёртвый dict + комментарий.
- 15 F401 unused imports в `bot/*` — auto-fixed через `ruff --fix`.
- `.env` orphan keys на 3 ботах: `MAX_NET_POSITIONS=30/30/10` (gate удалён 2026-05-08 утром), `PREMIUM_EXTREMUM_WINDOW`, `PREMIUM_RISK_MULT` (premium tier удалён 2026-05-07). Cleanup: `sed -i` на всех 3 ботах.

**Drift findings (NOT yet fixed — deferred to user)**:
- `bot/risk.py`: VPS имеет dead import `get_state, set_state` from `bot.journal` + старый комментарий «premium-tier». Mac версия cleaner. Не критично — VPS работает; просто dead import.
- `bot/exchange.py` HL VPS NEWER чем Mac: имеет `CANDLES_MIN_TTL_SEC` cache feature с `fetched_at_ts`, упрощённый `_coin_cache_offset_ms` docstring. Mac не sync. Riск при blanket scp с Mac → потеря этой feature на HL. **Mac надо обновить с HL.**
- `bot/exchange.py` Nado VPS OLDER: не имеет `_coin_cache_offset_ms` (jitter fix 2026-05-06). При top-of-hour invalidation на Nado может быть thundering herd 429. **Nado надо обновить с Mac.**
- `bot/exchange_factory.py` HL OLDER: имеет dead `kraken_margin` handler (memory: kraken_margin disabled $0 — module не существует на VPS). Не критично, dead branch.
- `bot/main.py` Mac NEWER: auto-fix убрал unused `PROJECT_ROOT` + `json as _json` imports. Cosmetic.

**Broken sweep scripts inventory (NOT removed — destructive op needs explicit "go")**:
- `/Users/ak/Desktop/HL/scripts/`: combo_with_filters, conservative_sweep, pattern_ema_filter, per_pair_edge, rebench_kf_full, strict_trend, struct_buffer_sweep — импортят удалённый `bot.vstop` или missing data file.
- `/Users/ak/Desktop/HL/hyperliquid_bot/scripts/`: diag_macd_compression, sweep_macd_compression, sweep_macd_compression_v2, sweep_multitf_rsi, sweep_premium_revalidate, sweep_xnn_poc — импортят несуществующий `bot.backtest`.
- Total: 13 dead scripts. Кандидаты на git rm (по правилу "rip and rebuild") — ждать explicit go от юзера, т.к. деструктивная операция.

**Deploy (rsync)**: 4 файла (`bot/notifier.py`, `bot/trader.py`, `bot/patterns_v2.py`, `bot/exchange_nado.py`) на 3 ботов; restart всех 3.

**Verify**: ✓ HL/KF/Nado active, 0 errors/traces/unbound в journalctl за 3-минутное окно после restart. F823/F821/F841 на bot/ — `All checks passed!`. prod_gates registry: 27 gates, без cooldown_per_pattern и concentration_max_net_positions.

**Memory**: новая `project_cooldown_removed.md` (utром); этот audit-pass логирует findings прямо здесь.

---

## 2026-05-08 16:30 WITA — COOLDOWN feature removed (rip) + latent UnboundLocalError fix

**Trigger**: ruff/pyflakes аудит нашёл `F823 datetime referenced before assignment` в `bot/trader.py:165-166`. Корень: на line 244 внутри `execute_signal()` есть `from datetime import datetime, timezone, timedelta` — это делает datetime/timezone **локальными для всей функции**. Cooldown gate на lines 165-166 ссылается на них раньше → `UnboundLocalError`. `try/except: pass` (line 170-171) глотает. Bug dormant потому что `COOLDOWN_HOURS=0` в проде уже давно. Юзер: «вырежи нафиг все с кулдаун. не будет его никогда».

**Rip**:
- `bot/config.py`: удалена `COOLDOWN_HOURS` константа.
- `bot/trader.py`: убран import + cooldown gate (lines 162-171). Бонус: убран redundant local `from datetime import datetime, timezone, timedelta` на line 244 (оставлен только `timedelta`) — это и был root cause F823. Cooldown gate был мёртвый, но *fix также убирает потенциальный bug если бы кто-то поставил COOLDOWN_HOURS>0.*
- `bot/journal.py`: удалена функция `last_trade_time_for()` — единственный caller был cooldown gate в trader.py.
- `bot/main.py`: убран `config.COOLDOWN_HOURS` из `module_const` лога.
- `bot/prod_gates.py`: удалён `Gate(id="cooldown_per_pattern")` из registry; обновлён `notes` для `already_traded_dedup`.
- `scripts/verify_config.py`: убран `COOLDOWN_HOURS` из expected default env.
- `scripts/honest_replay_kf.py` (hyperliquid_bot/scripts/): убран import + cooldown_bars + applied_gates entry + `cooldown_hours` в JSON output. **bonus**: попутно также добавлен `MAX_NET_POSITIONS` workaround в `scripts/honest_replay_hl.py` (impl was broken — gate config was deleted earlier).
- `scripts/_backtest_harness.py`, `scripts/_strategies.py`, `scripts/replay_confirmation_compare.py`, `scripts/honest_replay_hl.py` (Mac scripts/): убраны cooldown_bars params, COOLDOWN_BARS constants, `last_per_pat` dicts, cooldown skip lines, COOLDOWN_HOURS из mock-PROD_ENV.
- 7 sweep/research scripts (combo/conservative/struct_buffer/pattern_ema/rebench_kf_full/per_pair_edge/analyze_trades + sweep_xnn/multitf_rsi/macd_compression/macd_compression_v2/premium_revalidate): аналогично batch-edited.
- `scripts/overnight_tasks.sh`: убран import + COOLDOWN_BARS.
- `.env` на 3 ботах (HL/KF/Nado): удалена строка `COOLDOWN_HOURS=0`.

**Deploy** (rsync, не git push — gh CLI отсутствует на Mac, см. предыдущий MAX_NET_POSITIONS entry):
- HL `/root/hyperliquid_bot/bot/{config,journal,main,trader,prod_gates}.py` + `scripts/verify_config.py`
- KF `/root/hyperliquid_bot/bot/{...}` + `scripts/verify_config.py`
- Nado `/root/nado_bot/bot/{...}` + `scripts/verify_config.py`
- restart всех 3 сервисов

**Verify**: ✓ все 3 active, журналы за 60s после restart — нет `error|trace|cooldown`. `compileall` clean, `ruff F823/F821` clean на bot/.

**Bonus findings (audit pass — еще не fixed)**:
- `scripts/honest_replay_hl.py` ранее был сломан: импортил `MAX_NET_POSITIONS` из `bot.config`, который был удалён в предыдущем rip. Хардкоднул `MAX_NET_POSITIONS = 999` как workaround (single-coin lock и так блочит).
- `bot/exchange_nado.py:1056` F811 — `NadoClient = NadoClient_` shadowing import (намеренный alias, минор).
- `bot/notifier.py:72` F821 — `undefined name 'message'` в no-op функции `daily_summary` (silent с 2026-05-04). Безвреден, но лучше fix.
- `bot/trader.py:928` F841 — `handled_by_lead: dict` объявлен, never used; комментарий «чтобы не делать дублирующих SL placement» — dedup-логика недореализована. Latent но ограничен legacy data.
- 8 broken sweep-скриптов в `scripts/` импортят удалённый `bot.vstop` или несуществующий `bot.backtest`. Кандидаты на git rm.
- 90 ruff issues всего: 52 unused-import, 15 пустых f-strings, остальное косметика.

**Memory**: новая `project_cooldown_removed.md`. Обновлён MEMORY.md (Bot state).

**Refs**: rsync deploy bot/{config,journal,main,trader,prod_gates}.py + scripts/verify_config.py локально (не committed — gh отсутствует, см. предыдущий entry).

---

## 2026-05-08 09:05 WITA — MAX_NET_POSITIONS removed (rip): cap создавал зомби-очередь + stale entries

**Trigger**: при разборе TIA −0.60% slip нашли реальный root: 24 SKIP'а подряд `net concentration: 30 longs vs 0 shorts (max +30)` в течение 12 мин. Сигнал стоял в очереди → drift до 1% → fill на сломанном тезисе. Юзер: «у нас такая хрень из-за капы. полдня разбирались». Решение — rip и не переделывать.

**Root cause**: `MAX_NET_POSITIONS` (default=12, HL `.env`=30, Nado `.env`=10) — cap на net-перекос (longs − shorts). В trend-only стратегии в bull-маркете ВСЕ паттерны генерят longs (`flag_long`/`triangle_long`/`123_long`); shorts на Nado дополнительно блочены `SKIP_FLAG_SHORT=1`. За пару дней набирается 30 longs → cap зажат → каждый новый long-сигнал каждый цикл скипается → ждёт closure existing position → к моменту освобождения quota рынок уже ушёл. Cap не диверсифицировал, а перепаковывал валидные сигналы в late entries.

**Rip**:
- `bot/trader.py`: убран import `MAX_NET_POSITIONS` + блок проверки (`# 0a. Concentration limit ...` 16 строк) перед `EXCLUDED_PATTERNS` check.
- `bot/config.py`: удалена константа + комментарий (плейсхолдер с датой удаления).
- `bot/main.py`: убран `config.MAX_NET_POSITIONS` из `module_const` лога.
- `bot/prod_gates.py`: удалён `Gate(id="concentration_max_net_positions")` из registry.
- `scripts/honest_replay_kf.py`: убран import + cap проверка в replay цикле + `rejected_netpos` counter + `max_net_positions` в JSON output.
- `scripts/verify_config.py`: убран `MAX_NET_POSITIONS` из expected `common` config + Nado override.

**Deploy**: rsync 4 файлов (`trader.py`, `config.py`, `main.py`, `prod_gates.py`) → /tmp на VPS → cp в `/root/{hyperliquid,nado}_bot/bot/` → `systemctl restart` всех 3.
- HL: hyperliquid-bot active, account loaded $33,912, 65/302 coins активны, 0 errors после restart.
- KF: kraken-bot active.
- Nado: nado-bot active.

**Verify**: ✓ `net concentration` строки исчезли из journalctl post-restart всех 3 ботов. Следующий часовой close покажет рост `SIGNAL X` без последующих `SKIP ... net concentration`.

**Risk**: backtest-логика теперь без cap — DD при 50+ одновременных longs может расти. По старому комментарию config.py: «без cap'а DD 50% в bear-разворот на 50 longs». Mitigation: `MAX_MARGIN_USED_PCT` (HL 0.50, KF 0.90) ловит margin перегруз; `MAX_CONSECUTIVE_LOSSES` ловит cascade. Если bear-revert вылезет — пересмотреть отдельно.

**Memory**: новая `feedback_drift_check_upstream_queues.md` (диагностический шаг: для drift/slip flag сначала ищи upstream queue, не сразу объясняй через рынок). Обновлён `feedback_config_vs_code_drift.md` (cap deprecated). Удалён residual #5 из `project_open_followups_2026_05_06.md` (предыдущий fix patterns_v2 в той же сессии).

**.env cleanup на VPS** (TODO): `MAX_NET_POSITIONS=30` в HL `.env`, `=10` в Nado `.env` теперь orphan. Не блокирует, но лучше удалить при следующем .env-touch'е.

**Refs**: rsync deploy bot/{trader,config,main,prod_gates}.py + scripts/{honest_replay_kf,verify_config}.py локально (не committed — gh CLI отсутствует на Mac, см. предыдущий entry).

---

## 2026-05-08 08:50 WITA — TRX BAD SL fix: structural sanity guards в patterns_v2


## 2026-05-07 13:50 UTC: Big cleanup (research scripts + vstop_atr legacy)

Юзер: "все сносим" → "го".

**Удалено целиком (~7300 строк, 43 файла)**:
- `bot/backtest.py` (545) — backtest engine
- `bot/oscillators.py` (137) — после удаления `is_premium_signal()` только `compute_rsi`, dead в prod
- `bot/vstop.py` (167) — legacy ATR vstop, импортировался только research scripts
- 40 research scripts: 7× `backtest_*`, 15× `sweep_*`, 15× analysis tools (`dd_analysis`, `daily_summary`, `efficiency_analysis`, `full_audit` дубль, `kraken_coins`, `kraken_strategy_research`, `liquidation_test`, `_metrics`, `nado_smoke_*`×2, `plot_*`×2, `slippage_stats`, `trend_detect_compare`, `validate_struct_vstop`), 3× старые (`verify_combo`, `watchdog.py` старый, `whatif`)

**Точечная чистка vstop_atr legacy**:
- `bot/trader.py`: `else: # legacy ATR vstop` ветка в `manage_open_positions` удалена (никогда не достижима — все 3 бота на vstop_struct); `tf = "4h" if vstop_struct else "1h"` → `tf = "4h"`; docstring упрощён; `settings.exit_mode` → захардкожено в notes JSON
- `bot/config.py`: drop `exit_mode: str` + `vstop_atr_mult: float` dataclass fields + env-load в `from_env()`

**Не тронуто (нужно для prod)**:
- `VSTOP_ATR_MULT=2.5` в `.env` + `from bot.swings import atr` — используется как **fallback** в `_compute_struct_stop()` когда struct не нашёл swing (2026-05-04 фикс трендовых движений без откатов).

**Scripts остаток** (12, все нужные): cron + manual ops + setup'ы.

**Verify**: compile OK везде, kraken-bot active, account $98,707, ноль ошибок post-restart, vstop_struct активно работает.

Backup: `/root/big_cleanup_backup_20260507_154059.tar.gz` (110K).

См. `data/manual_ops/2026-05-07_kraken_big_cleanup.md`.

## 2026-05-07 13:33 UTC: Dead code cleanup (kraken_margin + premium remnants)

Юзер: "теперь давай чистить что внутри файлов лишнее" → "го везде".

**Удалено целиком** (977 строк):
- `bot/exchange_kraken_margin.py` (382 lines) — KrakenMarginClient, биржа `kraken_margin` мертва
- `scripts/sweep_premium_tier.py` (245), `scripts/_strategies.py` (~210), `scripts/deep_summary.py` (~140) — premium-tier research, сломаны после удаления `is_premium_signal()`

**Точечная чистка**:
- `bot/exchange_factory.py` — drop `kraken_margin` branch
- `bot/config.py` — drop `kraken_spot_api_key/secret` dataclass fields + `kraken_spot_required` env-required flag
- `bot/notifier.py` — drop `check_margin_buffer()` (DEPRECATED, ноль вызывающих)
- `scripts/heartbeat.py`, `scripts/status.sh`, `scripts/verify_config.py`, `scripts/watchdog_kraken.py`, `scripts/live_vs_backtest.py` — убраны mentions `kraken-margin-bot` / `PREMIUM_*` env / dead `BASELINE_AVG_R_PREMIUM`.

**⚠️ Shared codebase**: правки в `bot/` пойдут на HL-bot и Nado-bot через `git pull`. Проверка: ни один из 4 живых ботов не использует `EXCHANGE=kraken_margin`, ни один не дёргает `is_premium_signal` или `check_margin_buffer` после prior cleanup.

**Verify**: kraken-bot перезапущен, compile OK, account $99,084, vstop активно обновляет SL.

Backup: `/root/code_cleanup_backup_20260507_152737.tar.gz` (29K).

См. `data/manual_ops/2026-05-07_kraken_dead_code_cleanup.md`.

## 2026-05-07 12:43 UTC: Kraken VPS cleanup

Юзер: "проверь бот кракен / что в нем может быть лишнего написано / хочу почистить".

**Удалено** (предварительно сохранено в `/root/cleanup_backup_20260507_144339.tar.gz`, 37M):
- 4 `.bak` бэкапа кода в `bot/` (main.py x2, trader.py x2)
- 2 `.env.bak*` бэкапа + 3 чужих `.env.*.example` (HL, nado, kraken_margin)
- 11 одноразовых ad-hoc скриптов в корне `hyperliquid_bot/` (audit_v2, cancel_orphan, full_audit, kraken_balance_check, etc.)
- 9 устаревших `.md` отчётов (OVERNIGHT_*, MORNING_REPORT, SCALING_50K, DD_ANALYSIS, TREND_DETECTION, MAINNET_DEPLOY, KRAKEN_LIQUIDITY_RESEARCH, claude_actions)
- `/root/kraken_margin_bot/` (мёртвый бот, service `inactive+disabled`, ~250K)
- `/root/hl_test/`, `/root/scripts/`, `/root/data/`, `/root/results/` (research-артефакты от 4-5 мая)

**Cron + systemd**:
- 2 cron-строки про `kraken_margin_bot` удалены
- `/etc/systemd/system/kraken-margin-bot.service` снят, `daemon-reload`

**Verify**: kraken-bot service active, journalctl без ошибок, торговый луп идёт.

**Не тронуто**: `/root/audit_full.py`, `/root/cleanup.py`, `/root/vps_audit.sh` (использует cron каждые 30 мин), `/root/venv/` (orphan, но не санкционировано), uncommitted локальные правки в `bot/` и `docs/log.md`.

**Pending**: premium tier dead code (`is_premium_signal`, `premium_risk_mult` в `oscillators.py:138` + `trader.py:324` + `.env`) — на HL уже снято 2026-05-07, на Kraken-VPS чистка не дошла. Требует сначала коммита текущих локальных правок.

См. `data/manual_ops/2026-05-07_kraken_vps_cleanup.md` для полного списка файлов.

## 2026-05-07 00:30 UTC: applied fixes #1 + #3 (юзерские "1 и 3")

После overnight глубокого анализа юзер выбрал применить fix №1 (env-var rename) и №3 (HIP-3 session-aware filter). Не выбрано: stop Nado service.

### Fix #1: env-var rename `MAX_MM_PCT` → `MAX_MARGIN_USED_PCT`

**Изменено**:
- `bot/trader.py:292`: `os.getenv("MAX_MM_PCT", "0.50")` → `os.getenv("MAX_MARGIN_USED_PCT", "0.50")`
- `bot/trader.py:286` (комментарий): `MAX_MM_PCT` → `MAX_MARGIN_USED_PCT`
- `bot/main.py:154`: `("MAX_MM_PCT", "0.50", float)` → `("MAX_MARGIN_USED_PCT", "0.50", float)`

**Effect** (после restart всех 3 ботов):
- HL: cap = 0.50 (из .env). До = default 0.50. Без изменений.
- KF: cap = 0.90 (из .env, юзер выставил это в commit 8818796). До = default 0.50. **Cap расширен**, но KF MM=3% — cap далеко.
- Nado: cap = 0.50 (из .env). Без изменений.

Бэкапы: `bot/trader.py.bak.20260507`, `bot/main.py.bak.20260507`.

### Fix #3: HIP-3 session-aware filter

**Добавлено** в `bot/trader.py:162` (после funding_blocked, перед cascade):

```python
if coin.startswith("xyz:") and os.getenv("HIP3_MARKET_HOURS_ONLY", "0") == "1":
    now = datetime.now(timezone.utc)
    is_weekend = now.weekday() >= 5
    in_us_hours = (
        (now.hour > 14 or (now.hour == 14 and now.minute >= 30))
        and now.hour < 21
    )
    if is_weekend or not in_us_hours:
        ts_str = now.strftime('%a %H:%M UTC')
        reason = f"HIP-3 outside US market hours ({ts_str}); after-hours = stale prices + instant-SL"
        insert_rejected(coin=coin, pattern=signal.pattern, timeframe=tf,
                        direction=signal.direction, rr=signal.rr, reason=reason)
        log.info("REJECT %s %s: %s", coin, signal.pattern, reason)
        return None
```

**.env**: `HIP3_MARKET_HOURS_ONLY=1` добавлено только на HL (KF/Nado не торгуют HIP-3). Бэкап `.env.bak.20260507`.

**Логика**: skip всех `xyz:*` сигналов вне Mon-Fri 14:30-21:00 UTC (NYSE hours). Backtest validation в HIP-3 audit от 2026-05-06: 6/6 fills вне US hours были instant-SL <1min, 100% loss rate.

### Deploy

- Code synced kraken-bot → hl-bot (общий codebase).
- Restart: `hyperliquid-bot.service` (hl-bot) + `kraken-bot.service` + `nado-bot.service` (kraken-bot). Все active, 0 tracebacks/exceptions за 2 минуты.
- HL первым же tick'ом подхватил `Account value: $7624.61`, прошёл concentration check (30 longs cap), без аномалий.

### Verify

```
Margin (после фикса):
| Бот | IM% | MM% | MAX_MARGIN_USED_PCT (.env) |
| HL  | 92% | 46% | 50% |  ← MM<cap, OK
| KF  | 29% | 3%  | 90% |  ← теперь с просторным cap'ом
```

HL продолжает быть в высокой leverage концентрации (IM 92%, MM 46%). Cap fix не открепляет напрямую — это safety mechanism, который сейчас близко к срабатыванию. Юзер должен решать стратегию: либо снижать MAX_MARGIN_USED_PCT (например до 0.30), либо вручную закрывать позиции, либо ничего не делать и ждать Mark-to-Market normalisation.

Не сделано:
- Stop Nado (юзер не выбрал, хотя 2/28 wins, MtM −$1098).
- Cosmetic rename `max_mm_pct` (local var) → `max_margin_used_pct` — не делал, имя локальной переменной семантически точное (MM cap), это не env-var.


## 2026-05-06 night: глубокий анализ — 4 находки

После запроса юзера «глубочайший анализ + не повторять ошибок» (после частичного отчёта днём) разобраны 4 проблемы:

### 1. 🚨 `MAX_MARGIN_USED_PCT` в .env не читается кодом

`.env` (на всех трёх ботах) содержит `MAX_MARGIN_USED_PCT=0.50/0.90`, но `bot/trader.py:292` и `bot/main.py:154` читают переменную с другим именем — `MAX_MM_PCT` (default 0.50). Семантически тоже разница: docs/strategy.md описывает cap как «used_maint + new_maint < cap × equity» (т.е. maintenance margin), а `MAX_MARGIN_USED_PCT` по имени должен быть initial margin. Но имени не совпадают → env-значение тихо игнорируется.

**Effect сейчас (2026-05-06 15:00 UTC)**:
- HL: IM/acct = 94%, MM/acct = 47%. Юзер думает что cap 50% IM — фактически только 50% MM cap (≈100% IM).
- KF: IM 28%, MM 3% (cap не близок).
- Все три бота работают на code-default 0.50 MM cap; .env не имеет влияния.

Не trivial fix — touches risk-логику. Юзер должен решить:
- (A) переименовать env в коде: `MAX_MM_PCT` → `MAX_MARGIN_USED_PCT`. Тогда KF=0.90 расширит cap (потенциально больше просадки).
- (B) добавить отдельный IM cap (separate env var) — safer.
- (C) удалить из .env и docs (decline cap).

См. memory `feedback_config_vs_code_drift.md`.

### 2. ⚠️ Nado: 2/27 wins (7%) all-time, MtM −35% от deposit'а

Все 5 паттернов и 11 из 13 coin'ов — отрицательные. AAVE 0/3 = −$111, BTC 0/3 = −$90, XMR 0/1 = −$63. Только DOGE 1/4 = +$56 и FARTCOIN 1/2 = −$30 имеют хотя бы одну прибыльную сделку. MtM all-time = −$1955 / $5562 deposits.

Бот живёт ~3 дня live, backtest baseline предсказывал edge → значит либо backtest методологически неверный (slippage assumptions vs Nado liquidity), либо Nado pattern detection / SL placement не работает на тонком стакане.

### 3. ⚠️ KF win rate 7d = 0/22 (0%)

Все 22 closed на KF за 7d — в минус. Realized 7d −$150 net. CHZ −$1197 на 1 trade — большая потеря. KF MtM +$10266 сидит на 13 открытых; если не отрабатывают → KF тоже уйдёт в минус.

### 4. ⚠️ HL cancelled rate 32% (50/154 за 7d)

Top: 35 «market_open did not confirm fill» (HL 429 rate limits) + 14 «aborted: update_leverage failed». Pattern прошёл risk-фильтр, а entry не получился из-за infra. Потеря edge.

---

## Действия

- **Не менял** risk-логику бота (MAX_MM_PCT остался как есть).
- **Расширил** `scripts/all_bots_snapshot.py` — теперь сразу показывает margin (IM/MM), cancel rate, win rate, realized vs unrealized split, top losers/winners. Drift `.env`-vs-code выводит как warning. Это ловушка против повторения сегодняшней ошибки (частичный отчёт без видимых rеd flag'ов).
- Memory: добавлен `feedback_config_vs_code_drift.md`, расширен `project_open_followups_2026_05_06.md` (пункты 8a–8d).

---

## 2026-05-06 — Execution quality bundle (commits `9665151`, `0392690`, `aced9d8`)

Юзер запросил проверку всех новых/закрытых сделок на ботах. По дороге обнаружились 4 issue, 3 пофиксены + задеплоены:

**Найденное при ручной проверке за 4ч**:
- HL: ENTRY XRP/FARTCOIN/LDO; CLOSE xyz:META (manual, отдельно зафиксирован).
- KF: ENTRY UNI; CLOSE XLM short id=36, AAVE short id=30 — оба `pnl_dollars=None` в DB.
- Nado: ENTRY FARTCOIN-PERP, BNB-PERP; CLOSE AAVE-PERP id=37 (pnl=-$52.06). Slip −1.22% на FARTCOIN — должен был насторожить, но первый отчёт прошёл мимо.

**Issue 1: KF realized PnL=None для долго-держимых сделок** ([commit `9665151`](https://github.com/takinanton/hyperliquid_bot/commit/9665151))
- Root: `compute_realized_pnl` ищет opens в окне `fetch_my_trades(limit=100)`. Для AAVE held 2.5d / XLM held 1.5d / CHZ held 1d opens вытеснены из окна → returns `(None, exit_px)`. trader.py писал "pnl unknown — нет fills" → DB `pnl_dollars=None`.
- Fix: в `bot/trader.py` после двух попыток `compute_realized_pnl` если pnl=None но `tr["entry"]` и `exit_px` доступны — компьютим `gross = (exit-entry) × size × sign`, fees ≈ 0.10% RT.
- Backfill (KF DB): AAVE id=30 −$34.90, XLM id=36 −$0.59, CHZ id=39 **−$1196.71** (тихо потерянный убыток).

**Issue 2: HIGH SLIP не флагилось на review** ([commit `0392690`](https://github.com/takinanton/hyperliquid_bot/commit/0392690))
- Юзер: "0.23224 (slip −1.22%) тебя почему не смутило?". Slip уровня 1.22% уходил в INFO-лог, не попадался на глаза.
- Fix: WARN-уровень `HIGH SLIP` если `|slippage_pct| ≥ ENTRY_SLIP_WARN_PCT` (default 0.5%).

**Issue 3: Nado FARTCOIN slip −1.22% — root cause анализ + 3 strategic gates** ([commit `aced9d8`](https://github.com/takinanton/hyperliquid_bot/commit/aced9d8))
- **Root**: Nado bot был сломан 04:28-06:43 UTC (`NameError: name 'os' is not defined` на ВСЕ coins в `_collect_signals`, старый код до коммита `a541e19`). Рестарт в 06:43:09. Первый сигнал в 06:43:44 firing'нул flag_long FARTCOIN с `entry=0.23512` — это close 1h-свечи 05:00-06:00, **43 мин stale**. К моменту fill (06:43:46) рынок упал на 1.22% до 0.23224. Bot купил **НИЖЕ breakout level** = инвалидированный flag-сетап (slip favorable по PnL, но trade стратегически не должен был открываться).
- Юзер дал "го" на стратегические гейты:
  1. **SIGNAL FRESHNESS GATE** (перед LIQUIDITY DRAG в `bot/trader.py`): `|mark - signal.entry| / entry > SIGNAL_MAX_DRIFT_PCT` (default 1.0%) ИЛИ direction-aware thesis_broken → REJECT. Ретро на FARTCOIN: drift 1.225% + thesis_broken=True → REJECT ✓.
  2. **SLIP CAP ABORT** (после fill): `|slip| ≥ MAX_FILL_SLIP_PCT` (default 1.5%) → `client.market_close` + mark cancelled. Срабатывает даже при favorable slip.
  3. **`slip_per_side` + `mark_price` на `KrakenClient`/`NadoClient_`**: раньше LIQUIDITY DRAG silently skip'ался на KF/Nado (`AttributeError → pass`). Теперь tier-based defaults (KF: 0.10%/0.20%/0.40%, Nado: 0.20%/0.40%/0.80% — Nado тоньше).

**Issue 4 (отложено явно)**: Nado orphan positions от 06:49 UTC bug 2026-05-05 — ONDO real=7250 vs DB open=2023 (id=15+id=21 ошибочно "closed"); kPEPE real=886000 vs DB open=405397 (id=19 orphan). SL placed по полной exchange szi (правильно) — защита целая. Pnl при close будет недосчитан для orphan долей. Backfill вручную после фактического закрытия.

**Не задеплоено** (явно отложено):
- `data/{exchange}_slippage.json` per-coin для Nado/KF — нужна historical fills история ≥100 trades per-coin per-bot.
- Per-pattern freshness override (некоторые EMA cross могут пережить 1% drift).

**Memory updates**: `feedback_check_trades_fix_issues.md` (slip ≥ 0.5% всегда flag даже favorable; long signal-to-fill gap watch); `feedback_strategy_deviation_deep_dive.md` (новый — любое отклонение → детальный root-cause разбор); `project_dd_halt_removed.md` (заметка про stale CB-логи от старых процессов).

**Manual ops files**: `data/manual_ops/2026-05-06_pnl_fallback_fix.actions.md`, `data/manual_ops/2026-05-06_freshness_slip_gates.actions.md` + journalctl snapshots per-bot.

---

## 2026-05-06 — HIP-3 reliability bundle (commits `1cca13d`, `1f3bff6`)

Юзер: "делаем бектест всех сделок на хип3, логах, памяти инструкций и пт на
предмет поиск ошибок. каждый раз что-то там с ним то баланс то стоп. сделай
все чтобы хип3 работал идеально. проверь так же изолейт хип3."

**Audit prod (HL DB, 26 HIP-3 trades)**:
- 20/26 cancelled (16× `update_leverage failed after retries`, 4× `market_open
  did not confirm fill`).
- 6/26 closed_vstop (instant SL за 35-55с).

**Root causes**:
1. **Partial fills**: market_open IOC отдавал 1-12% от intended size на тонких
   HIP-3 (xyz:AMZN intended 61.156 filled 0.633 = 1%). DB обновлялась под
   filled, но SL placed под tiny notional → spread movement = SL hit.
2. **Tight SL**: pattern detector ставит SL за свежий swing extremum. На
   thin/after-hours HIP-3 свечах swings плотные → SL distance 0.31-1.16% от
   entry. Entry slip 0.1-0.5% съедает headroom.
3. **Dead drag check**: `HLClient.slip_per_side` отсутствовал → `try ... except
   AttributeError: pass` молча skipped LIQUIDITY DRAG check на HL.
4. **Log spam**: xyz:BABA `BAD SL skipped` каждый цикл часами — паттерн с
   p4.price > entry для long.

**Fixes (env-tunable)**:
- `bot/trader.py`: HIP-3 MIN SL DISTANCE GUARD — env `HIP3_MIN_SL_PCT` (default
  1.5%). Pre-flight reject если SL distance < threshold. Of historical 26
  HIP-3 trades: filters 17 (65%), включая 5/6 instant-SL и 12/20 cancelled.
- `bot/trader.py`: PARTIAL FILL ABORT — env `HIP3_MIN_FILL_RATIO` (default
  0.5). После market_open, если filled_sz/intended < 50% → emergency
  market_close + cancelled.
- `bot/exchange.py`: `HLClient.slip_per_side(coin, notional)` — реализован.
  Грузит `data/hl_slippage.json`, fallback 0.005 для xyz:* / 0.001 для
  остальных. Активирует существующий drag check.
- `bot/patterns_v2.py`: BAD SL warning rate-limited 1/час per (coin, pattern).
- `data/hl_slippage.json` добавлен в repo (был только локально).

**Isolate audit**: OK. Бот всегда `is_cross=True` на open. `_check_liq_vs_sl_for_position`
ловит isolated и switching → cross (commits `784e49e`/`dd6c81c`). Orphan SL
cleanup для HIP-3 dexes уже есть (commit `b378ec2`). HIP-3 isolated в проде
сейчас нет.

**Verify post-deploy**: bot active, account_value $6810, no tracebacks. Открытые
30 longs + 0 HIP-3 — concentration cap не пускает новые HIP-3 пока не закроются
старые.

**Backtest validation** (`/root/results/hip3_filter_fast.txt` на kraken-bot,
top-25 HIP-3 пар × ~5 мес):

| Метрика | Без фильтра | С HIP3_MIN_SL_PCT=0.015 |
|---|---|---|
| n trades | 947 | 426 (filtered 521 = 55%) |
| sum_R | **−1430.78** | **−161.46** |
| avgR | −1.511 | −0.379 |

Filter saves **+1269R** убытков, avgR per slot улучшен ×4. xyz:JPY = paradigm
case: 59/59 trades с SL <1.5% (forex после-сессионно — тонкие swings),
filter режет всё → **−294R → 0R**. Только 3/25 пар (BRENTOIL/SNDK/PLTR)
теряют edge от фильтра (~21R total) — net win ×60.

Caveat: backtest c консервативным slippage 1% RT. Real prod ≈0.5-0.8%, но
фильтр всё равно decisively net-positive.

---

## 2026-05-06 — Premium tier revalidation: PREMIUM_RISK_MULT 2.5 → 1.0 deployed

Sweep `scripts/sweep_premium_revalidate.py` на top-100/27mo HL с **real costs** (per-coin slip × 1.5 margin).

**Premium trades**: 26/1490 = 1.7%.

| mult | $pnl | DD% | Sh | avgR_w | Q+ |
|---|---|---|---|---|---|
| 1.0× | +13250 | 17.0 | 5.0 | +0.889 | 10/10 |
| 2.5× (prod) | +13372 | 16.9 | 5.0 | +0.897 | 10/10 |
| 3.0× | +13413 | 16.9 | 5.0 | +0.900 | 10/10 |

Diff between 1.0× и 2.5× = +0.9% avgR / +1% ret — в пределах шума на n=26.

**Per-pattern (важное)**:

| pattern | prem n | prem avgR | nonprem n | nonprem avgR | ratio |
|---|---|---|---|---|---|
| 123_long/short, flag_long/short | 0 | — | — | — | — |
| triangle_long | 13 | +0.42 | 240 | +1.57 | **0.27×** ✗ |
| triangle_short | 13 | +0.20 | 236 | +0.45 | **0.45×** ✗ |

Selective whitelist (premium > nonprem) = **ПУСТ**. Premium-strict signal в текущем регрессионном поле — антифильтр качества.

**OVERNIGHT_REPORT_2 (2026-05-02)** показывал premium WR=64%, avgR=+1.51 — но это до Option B + ATR regime + MACD filter. Пул премиум-сигналов сменился, character стал отрицательным.

**Решение**: `PREMIUM_RISK_MULT=2.5 → 1.0` на всех 3 ботах (HL/KF/Nado). Effective disable premium boost.

**Reasoning**:
- Per-pattern data clear: premium quality < non-premium
- n=26 sample мизерный — нет статсилы держать 2.5×
- Capacity-aware view: real prod с `MAX_MARGIN_USED_PCT=0.50` теряет opportunity cost когда 2.5× буст сидит на underperforming trades
- Conservative default: "no boost" пока premium signal не покажет positive edge

**Deploy steps** (юзер 2026-05-06: стратегия changes — без подтверждения):
1. `sed -i 's/PREMIUM_RISK_MULT=2.5/PREMIUM_RISK_MULT=1.0/'` на HL/KF/Nado .env
2. `systemctl restart` каждого бота
3. Verify journalctl 60s — все 3 active, no errors.

Файлы: `/tmp/sweep_premium_reval.json`, `/tmp/sweep_prem.log`.

---

## 2026-05-06 — #2 Xnn POC: SCRAP (после fix fees/slippage)

POC `scripts/sweep_xnn_poc.py`: scaling-in на +1R. 3 итерации:

**v1 (бажная — leg-2 SL не enforced)**: 1.0× avgR +70% — оптимистично.
**v2 (fix leg-2 BE check)**: 1.0× avgR +36%, DD +81%. PASS Phase 1 criterion. Презентовал юзеру.
**v3 (после фидбэка юзера: per-coin slippage из `data/hl_slippage.json` × 1.5 margin)**:

| mode | n | ΣR | DD% | Sharpe | WR | avgR |
|---|---|---|---|---|---|---|
| baseline | 1490 | +1311 | 17.0 | 4.9 | 44% | +0.88 |
| xnn 0.25× | 1490 | +1330 | 22.1 | 4.3 | 40% | +0.89 (+1%) |
| xnn 0.5× | 1490 | +1350 | 26.7 | 3.8 | 38% | +0.91 (+3%) |
| xnn 1.0× | 1490 | +1388 | 34.6 | 3.0 | 33% | +0.93 (+6%) |

**Ключевой урок**: реалистичные fees/slippage cardinally меняют картину. Baseline avgR -34% (+1.33 → +0.88). Xnn 1.0× edge с +36% сжимается до +6% — фактически в пределах шума.

**Решение**: SCRAP. Phase 1 criterion ">30% improvement" — fail на всех variants. Sharpe падает. DD растёт.

**Bonus lesson** (saved в memory `feedback_fees_slippage_in_backtests.md`): backtest без per-coin slippage с запасом = false-positive edge на иллик-coins. Все будущие тесты — с реальной cost-моделью.

Файлы: `/tmp/sweep_xnn_poc.json`, `/tmp/sweep_xnn.log`.

---

## 2026-05-06 — Сессия roadmap: 4/4 SCRAP

Прошли все 4 кандидата по очереди после введения 2 правил оценки:
1. **avgR per trade** > ΣR/Sharpe (capacity capped)
2. **fees + per-coin slippage с запасом 1.5×** обязательно

| идея | результат |
|---|---|
| #1A RSI Divergence | Sharpe -11%, aligned-only avgR worse → SCRAP |
| #4A Multi-TF RSI | n=28, avgR -58% → SCRAP |
| #5A MACD Compression | avgR -46% (combined) → SCRAP |
| #2 Xnn (наивный) | avgR +6% при DD +103% (real costs) → SCRAP |

**Conclusion**: existing prod (Option B + ATR regime + MACD) уже well-tuned. Простые добавки edge не дают.

**Открытые задачи** для отдельных сессий:
1. Premium tier revalidation (#4A bonus): `PREMIUM_RISK_MULT=2.5` сейчас бустит avgR +0.57 trades vs +1.37 non-premium
2. STRUCT_BUFFER 0.003 → 0.007 (планово 2026-10-01): валидировать на актуальном конфиге
3. Vyacheslav-style Xnn proper (Fib EMA 21/55) — если стоит попробовать без наивного +1R

---

## 2026-05-06 — #5A MACD Compression: SCRAP

POC `scripts/sweep_macd_compression.py` (window=20, breakout-trigger), top-100/27mo HL.

| mode | n | ΣR | DD% | Sharpe | avgR | Q+ |
|---|---|---|---|---|---|---|
| baseline (existing) | 1481 | +2009 | 16.1 | 6.3 | **+1.36** | 10/10 |
| macd_comp standalone | 1584 | +270 | 19.5 | 4.5 | +0.17 | 7/10 |
| combined | 3065 | +2280 | 16.1 | 6.9 | +0.74 | 10/10 |

Изначально хотел сказать "deployable" — Sharpe +10%, ΣR +13%, DD same.

**Юзер скорректировал** (важная фидбек, сохранён в memory):
- ΣR/Sharpe растут от √n при добавлении trades — statistical artifact, не quality.
- avgR per trade — настоящая метрика. baseline +1.36 → combined +0.74 = **-46% качества**.
- Capacity capped (HL `MAX_NET_POSITIONS=30`, cd=12h). Каждый слот ценен — лучше 1481 trades по +1.36R чем 3065 по +0.74R.

**Решение — SCRAP** (даже несмотря на formal Sharpe-rule passing). Bug-fix detector (range_high включал текущий бар) был сделан, но финальная metrics всё равно хуже.

**Lesson для правил деплоя**: правило "Sharpe не падает + DD не растёт" из roadmap — для **subtractive** изменений (filters). Для **additive** (новые patterns) добавляется "avgR не падает значимо".

Файлы: `/tmp/sweep_macd_compression.json`, `/tmp/sweep_macd_comp.log`.

---

## 2026-05-06 — #4A Multi-TF RSI extremum: SCRAP + bonus finding

Sweep `scripts/sweep_multitf_rsi.py` на top-100/27mo HL, struct vstop, baseline 1482 trades. Тестировал 3 windows (±5/±10/±20 daily bars) для extremum check.

**Distribution**: ±5d=28 (1.9%), ±10d=0, ±20d=0 (wider window = harder to be exact min/max).

| mode | n | ΣR | DD% | Sharpe | avgR | Q+ |
|---|---|---|---|---|---|---|
| baseline | 1482 | +2004 | 16.1 | 6.24 | +1.35 | 10/10 |
| require_strict | 28 | +16 | 3.1 | 1.94 | +0.57 | 5/8 |

**Решение — SCRAP** как entry filter: n=28<<500, avgR хуже baseline, фейл logic-check.

⚠️ **Bonus finding (важно для прода)**:
- `premium_strict=True`: avgR **+0.57** (n=28)
- `premium_strict=False`: avgR **+1.37** (n=1454)

Premium tier (RSI extremum на 1d ±5d) даёт **ХУЖЕ** среднюю R чем не-premium в текущем регрессионном поле. Прод сейчас бустит риск 2.5× (`PREMIUM_RISK_MULT=2.5`) на этих сделках — потенциально **бустит менее прибыльные**.

OVERNIGHT_REPORT_2 (2026-05-02): premium WR=64%, avgR +1.51 → но это было ДО Option B + ATR regime + MACD filter. Премиум-пул сменился.

**Кандидат на отдельную задачу**: revalidate нужен ли premium 2.5× boost в текущей конфиге, или скрапнуть PREMIUM_RISK_MULT → 1.0.

Файлы: `/tmp/sweep_multitf_rsi.json`, `/tmp/sweep_multitf_rsi.log`.

---

## 2026-05-06 — #1A RSI Divergence: SCRAP

Roadmap: #1A entry-filter тест. Sweep `scripts/sweep_rsi_divergence.py` на top-100/27mo HL mainnet, struct vstop, baseline 1481 trades.

**Distribution**: neutral 74.1% / opposite 21.7% / aligned 3.4% / none 0.7%.

| mode | n | ΣR | DD% | Sharpe | avgR | Q+ |
|---|---|---|---|---|---|---|
| baseline | 1481 | +2002 | 16.1 | 6.24 | +1.35 | 10/10 |
| skip_opposite | 1160 | +1683 | 11.8 | 5.55 | +1.45 | 10/10 |
| require_aligned | 51 | +39 | 4.5 | 1.64 | +0.76 | 5/10 |

Per-divergence avgR: aligned +0.76 / neutral +1.47 / opposite +0.99 / none +3.29.

**Решение — SCRAP** (по правилам деплоя из roadmap):
- `require_aligned`: фейл logic-check (n=51 << 500, 5/10 кварталов, avgR хуже baseline)
- `skip_opposite`: Sharpe -11% (правило "Sharpe не падает" нарушено), хотя avgR +7% и DD -27%

**Контр-интуитивно**: aligned divergence (исходная Vyacheslav-логика) — **хуже** neutral'а в среднем (+0.76 vs +1.47). Opposite divergence тоже даёт +0.99R — режу позитивные сделки.

**Empirical observation**: filter "RSI divergence" не работает на этих данных как entry-улучшалка. Откат, идём на #4A.

Файл результатов: `/tmp/sweep_rsi_div.json`, лог: `/tmp/sweep_rsi_div.log`.

---

## 2026-05-06 — #0 Vstop param sweep: ЗАКРЫТ как нерелевантный

Roadmap #0 предполагал sweep `atr_mult × period` (1.5..4.0 × 10/14/20). Проверил конфиг прода:

```
EXIT_MODE=vstop_struct  (HL/KF/Nado all)
STRUCT_BUFFER_PCT=0.003
```

При EXIT_MODE=vstop_struct параметры atr_mult/period **игнорируются** в `bot/vstop.py:find_struct_vstop_exit`. Sweep даёт 0 ценности для прода.

**Что было свипано** (Block 4 OVERNIGHT_ANALYSIS, 30d/top-30): STRUCT_BUFFER_PCT × {0.001, 0.003, 0.005, 0.010}. Прод 0.003 рядом с оптимумом, 0.001 marginal +5% в шуме, 0.005 -11%, 0.010 wash, ATR -47%.

**Незакрытый вопрос**: плановое STRUCT_BUFFER 0.003→0.007 на 2026-10-01 — между 0.005 (хуже) и 0.010 (wash). Откуда 0.007 — проверить.

См. `docs/roadmap.md` секция "#0".

---

## 2026-05-06 — SUMMARY (за день)

**Юзер фидбэк сессии**: "каждый раз баги" + "ты стал терять инфо" + "каждый раз косяки с балансами"

### Bug fixes (по приоритету):
1. **CB peak-based** (`43fe2a0`) — все 3 бота имели сломанный CB (использовал stale initial_account_value). Nado торговал в DD 47% без защиты. **Откатано позже** (см. ниже DD removal).
2. **Nado min_size** (`c305742`) — бот отклонял ~12 trades с false reason (min=100 BTC вместо 0.005)
3. **Nado NO_PNL** (`b2fe983`) — все closed_vstop trades имели pnl=NULL. Backfilled 11/18 trades.
4. **HL NO_PNL** (`ce7036e`) — stale fills cache при close через 30-60 сек после open. Backfilled 28/35 trades.
5. **Cascade filter v2** (`e8e730c`) — over-aggressive (374 rejects/4h). Считаем теперь только losses.
6. **Nado config restore** (`c25c41b`) — `nado_subaccount` потеряно при git reset.
7. **CLAUDE.md restructure** (`c78d240`) — 2196 → 83 строк, остальное в docs/.
8. **DD filter removed entirely** (`9bfc58f`) — peak-based CB давал spam на HL (DD колебался 6.5–9.2% вокруг 7%). Юзер: "удали ДД фильтр, оценка и тд. потом снова что-то введем."
9. **`import os` missing in main.py** (`03a4187`) — surfaced при verify. Pre-existing с MACD filter deploy (`0eee45a`). 684 NameError за 6ч на KF — бот молча не собирал ни одного сигнала. **Critical regression latent for hours.**

### Earlier today:
- Coin whitelist daily refresh (`bf20070`) — 301 → 60 coins на HL
- MACD filter v2 deployed (`0eee45a`)
- Place-first SL pattern (`1647fae`)
- ASTER unprotected fix MIN_SL_NOTIONAL (`0eee45a`)
- HL frontend_open_orders (`ceed668`)
- Mark price all_mids cache (`7ad1fcc`)

### Bot state at end of day:
- HL: active, БЕЗ DD-стопа, $5,756, торгует
- KF: active, БЕЗ DD-стопа, $48,612, торгует
- Nado: active, БЕЗ DD-стопа, $2,772 (DD 50% от peak — раньше CB был tripped, теперь торгует)

### Открытые вопросы:
- DD-защита: что вернуть взамен (peak-based с буфером? consecutive-loss-only? трейлинг от high-water mark?). Юзер: "потом снова что-то введем"
- KF NO_PNL: 2 closed_vstop NULL за 48ч (XLM, CHZ) — тот же pattern что HL/Nado, но KrakenClient SDK ещё не пофикшен
- DB `bot_state.initial_account_value` мусор на всех 3 (HL=2000, KF=0.0397, Nado=997.65). Поле dead, не используется кодом, но засоряет.

---

## 2026-05-06: DD filter полностью удалён (commit `9bfc58f`)

**Контекст**: peak-based CB (commit `43fe2a0`, утром) сразу дал проблему — на HL account колебался $5,448 → $5,610 при peak $6,002 (DD 6.5–9.2%) при threshold 7%. CB триггерился каждый цикл при tick движении цены → spam Telegram.

**Юзер**: "удали ДД фильтр, оценка и тд. потом снова что-то введем"

**Удалено**:
- `bot/risk.py::circuit_breaker_tripped()` DD-логика → оставлен только consecutive_losses
- `bot/notifier.py::check_drawdown()`, `check_daily_drawdown()`
- `bot/main.py`: peak/daily equity tracking + helpers + Cat B DD checks
- `bot/config.py::Settings.max_drawdown_pct`
- `scripts/verify_config.py`: MAX_DRAWDOWN_PCT canonical
- README.md, all `.env.*.example`: doc references
- Файлы `data/peak_equity.txt`, `data/daily_equity.txt` удалены на 3 VPS

**Сохранено**:
- `MAX_CONSECUTIVE_LOSSES` (отдельный фильтр)
- Maintenance margin alerts (real liquidation risk @ 80%)
- DD как research-метрика в backtest.py / dd_analysis.py / whatif.py
- `.env` на VPS с MAX_DRAWDOWN_PCT — поле теперь игнорируется кодом, безопасно

**Verify post-deploy**: HL $5,756 / KF $48,612 / Nado $2,772 — все active, errors clean.

---

## 2026-05-06: `import os` missing в bot/main.py (commit `03a4187`)

**Surfaced**: при verify после DD removal — `journalctl` показал NameError на каждом `collect_signals`.

**Pre-existing с**: commit `0eee45a` (MACD filter deploy). MACD-filter код в `_collect_signals()` вызывал `os.getenv("MACD_FILTER_ENABLED", ...)`, но `import os` в `bot/main.py` отсутствовал.

**Impact**: 684 NameError за последние 6ч на KF. Бот **молча** не обрабатывал ни одного сигнала на любой паре — все signals collection фейлили в try/except. Никакой алёрт не выстрелил, потому что error logged через `log.error` (не critical).

**Lesson learned (зафиксировать в правила)**:
- При деплое нового кода с новыми функциями — **обязательно `journalctl --since` после restart** с фильтром `error|traceback|nameerror|importerror`. Не полагаться на "бот active = всё OK" — systemd `active` означает только что процесс жив, не что код работает корректно.
- Smoke import check на dev машине ловит такие баги до деплоя — `python -c "import bot.main"` достаточно.

---

## 2026-05-06: Nado min_size — count×inc, не base units (commit `c305742`)

**Симптом**: ~12 cancelled trades в DB на BTC/ETH/LTC/BNB парах с "below_min_size: size 0.07 < Nado min 100.0".

**Корень**: Nado API возвращает `min_size = 100×10^18` (= `100000000000000000000`) для ВСЕХ perps. Старый код:
```python
min_size_x18 = int(row.get("min_size", 0))
min_base = min_size_x18 / X18  # WRONG → 100 base units
```
Интерпретировал как 100 base units (100 BTC = $8M, 100 ETH = $300k). Реально это count of size_increments × 1e18 (Vertex-style encoding).

**Fix**: `min_base = (raw / X18) × size_increment_in_base`

| Pair | Old (wrong) | New (correct) |
|---|---|---|
| BTC | 100 BTC ($8M) | 0.005 BTC ($408) |
| ETH | 100 ETH ($300k) | 0.1 ETH ($300) |
| LTC | 100 LTC ($30k) | 2 LTC ($200) |
| BNB | 100 BNB ($60k) | 0.5 BNB ($300) |
| LINK | 100 LINK ($2k) | 10 LINK ($200) |
| SOL | 100 SOL ($20k) | 10 SOL ($2k) |

**Verify**: после deploy `c.asset('BTC-PERP').min_size = 0.005` ✓
**Affected trades** (cancelled by false reason): id=27,29,34,35,41,43,44,46,48,50,52 (BTC/ETH/BNB/LTC).

## 2026-05-06: Nado NO_PNL — m.timestamp=None (commit `b2fe983`)

**Симптом**: ВСЕ closed_vstop trades на Nado имели `pnl_dollars=NULL` в DB.

**Корень**: `m.timestamp` всегда None в Nado SDK (`nado_protocol.indexer_client`). Старый код:
```python
ts = int(m.timestamp) * 1000 if getattr(m, "timestamp", None) else 0
```
давал ts=0 для всех fills. Фильтр `t_ms >= open_ms` всегда False (0 < 1778...) → matching=[] → return None.

**Fix**:
1. `_fetch_matches_for_product` — использовать `submission_idx` как time proxy (monotonically increasing index, fills возвращаются descending order = newest first)
2. `compute_realized_pnl` переписан:
   - Phase 1: first batch closing-side fills с cum amount ≥ size×0.95 = close
   - Phase 2: next batch opening-side fills с cum amount ≥ size×0.95 = open

**Verify**: trade 47 ASTER long size=9548 entry=$0.67491 → pnl=**-$33.11**, exit_px=$0.6722 (≈ SL trigger) ✓

**Backfill**: 11/18 NULL trades восстановлены (7 skipped — fills уже rotated out of API limit 200):
- DOGE -$6.27, SOL -$55.63, XMR -$62.65, FARTCOIN +$12.61/-$42.12, AAVE -$29.68×2, XPL -$24.68/-$35.48, ASTER -$17.81/-$33.11

## 2026-05-06: КРИТИЧНЫЙ — CB сравнивал с initial, не peak (commit `43fe2a0`)

**Юзер**: "проверь есть ли новые сделки. каждый раз баги"

**Симптом**: Notifier на Nado логировал "Drawdown 47% от пика $5557 → $2925" каждые 30 сек, но CB **не срабатывал**, бот продолжал открывать новые позиции в DD 47% без защиты.

**Корень**: `circuit_breaker_tripped()` использовал `initial_account_value` из `bot_state` table (DB), которое set ОДИН раз при первом старте бота. Потом устаревало.

| Бот | initial (DB) | Peak | Current | Real DD | Old формула | CB трип? |
|---|---|---|---|---|---|---|
| HL | $2000 | $6002 | $5908 | 1.5% | "-195% profit" | ❌ нет (правильно) |
| KF | $0.04 (!!) | $48740 | $48341 | 0.8% | "-107M% profit" | ❌ нет (правильно) |
| **Nado** | **$997** | **$5557** | **$2925** | **47%** | **"-193% profit"** | **❌ нет (НЕПРАВИЛЬНО)** |

Nado стартовал с тестовым балансом $997, потом юзер пополнил до $5557, потом DD 47% до $2925 — формула давала фейковый "+193% profit".

**Fix**: CB теперь читает `data/peak_equity.txt` (тот же file что notifier использует для DD alerts). Consistent metric.

**Effect после deploy**:
- HL: DD 1.5% (<7%) → CB не триггерится, торгует ✓
- KF: DD 0.8% (<7%) → CB не триггерится, торгует ✓
- **Nado: DD 47% (>10%) → CB сработал**: `CIRCUIT BREAKER: drawdown 47.35% от пика $5,557.03 → $2,925.80 (>= 10.00%) — skip new opens (trail still works)` — новые позиции не открываются, trail SL работает на 10 существующих

## 2026-05-06: NO_PNL_RECORDED для HIP-3 (xyz) — stale fills cache (commit `ce7036e`)

**Юзер**: "проверь все закрытые сделки за последние 4 часа на всех биржах"

**Симптом**: 7/8 closed xyz: trades за 4ч имели `pnl_dollars = NULL` в DB. Log показывал "Position xyz:ORCL закрылась trade_id=150 (pnl unknown — нет fills)" хотя в API user_fills есть запись `xyz:ORCL Close Long -4.32866`.

**Корень**: `client.user_fills()` имеет 60s TTL cache. Position закрывалась SL trigger через 30-45 сек после открытия (HIP-3 thin liquidity → tiny fills → instant SL). При cycle где детектится close, `user_fills()` возвращал stale cache из предыдущего cycle БЕЗ close fill → matching=[] → pnl=None → DB NULL.

**Fix**:
- `user_fills(ttl_sec=0.0)` force fresh fetch
- Retry с sleep(2) если первый fetch не дал fills (handles SL fill propagation delay 1-2 сек)
- TypeError fallback для non-HL clients (Kraken/Nado без ttl_sec param)

**Verify**:
- Trade 150 xyz:ORCL long → pnl=**-$4.33**, exit_px=$184.19 ✓
- Trade 151 xyz:BIRD short → pnl=**-$1.82**, exit_px=$5.96 ✓

**Backfill**: 28/35 NULL trades восстановлены (7 skipped — fills rotated out of cache):
- xyz: ORCL -$4.33, BIRD -$1.82 + другие
- non-HIP3: ZEN +$33.13, REZ +$26.24×2, DOGE +$4.23, KAITO +$0.88, APT +$1.37, MOODENG +$1.95, ETC +$1.48 — winners
- losses: UNI, FET×2, kSHIB, CFX×2, XPL×3, STRK×2, XMR, и т.д.

## 2026-05-06: Cascade filter v2 — count losses, not all closes (commit `e8e730c`)

**Симптом**: 374 cascade rejects/4h на HL — over-aggressive. Bot stuck в cascade-block почти весь 4ч период.

**Корень**: V1 считал ВСЕ closes (threshold=2). Включая instant-SL HIP-3 trades с tiny fills (size mismatch -99% → tiny notional → SL быстро триггерится). Эти "instant closes" — артефакт thin liquidity, не реальный market cascade.

**Fix**:
- Считаем только LOSSES (`pnl_dollars < 0 AND IS NOT NULL`)
- Threshold 2 → 3
- Window 60m unchanged

**Verify**: post-deploy reject count в 30-минутном окне (ждём wakeup для measurement).

## 2026-05-06: Nado config restore — `nado_subaccount` в shared Settings (commit `c25c41b`)

**Симптом**: после каждого `git reset --hard` на nado_bot бот падал с `AttributeError: 'Settings' object has no attribute 'nado_subaccount'`. Юзер должен был руками re-applying fix.

**Корень**: `nado_subaccount` field был добавлен manually в config.py на nado_bot, но НЕ был commit'нут в shared repo. git reset перезаписывал и поле терялось.

**Fix**: добавлено поле в shared `Settings` dataclass:
```python
nado_subaccount: str = "default"  # default value, only used by NadoClient
```
В `from_env()`: `nado_subaccount=_get("NADO_SUBACCOUNT", "default")`. Для HL/KF поле просто игнорируется.

**Verify**: после deploy nado-bot active, не падает.

## 2026-05-06: CLAUDE.md restructure (commit `c78d240`)

**Юзер**: "ты стал терять инфо, забывать и хуже работать. совет друга: claude.md должен быть лаконичным"

**Проблема**: CLAUDE.md разросся до 2196 строк (incidents + правила + архитектура + roadmap всё в одном). При загрузке сессии Claude видит весь wall-of-text → теряет фокус.

**Fix**: split на 6 файлов:
- `CLAUDE.md` (83 строк): архитектура, критичные правила, pointers
- `docs/log.md`: dated incidents (этот файл)
- `docs/strategy.md`: production config, EMA results, HIP-3
- `docs/ops.md`: VPS, deploy, scripts, multiprocessing
- `docs/roadmap.md`: Xnn/осцилляторы/liquidity cycles
- `docs/rules.md`: расширенные правила

**Effect**: 2196 → 83 lines в CLAUDE.md (96% reduction). Детали грузятся по нужному файлу.

## 2026-05-06: Coin whitelist — daily refresh

**Юзер**: "массовый фильтр чтобы не пинговать пары которые нам не подходят" + "stакан к фильтру все равно добавляем"

**Filter (default)**:
- OI ≥ $2M
- Vol_24h ≥ $1M
- spread ≤ 0.1%
- depth at ±0.5% ≥ $5k (both sides)
- levels in ±0.5% band ≥ 3

**Effect**: 301 → 60 coins на HL, ~80% blocked. Файл `data/coin_whitelist.json`, TTL 24h, fallback → full universe если stale.

## 2026-05-06: MACD filter v2 deployed на всех ботах (commit `0eee45a`)

12/26/9 EMA. Long требует MACD histogram > 0, short требует < 0. EMA cross strategy игнорирует filter.

**Backtest**: HL +8% avgR, +38% CAGR.

## 2026-05-06: Place-first SL pattern (commit `1647fae`)

Trail SL раньше работал: cancel old → place new → еcли place fail, позиция голая. Now: place new → cancel old. Eliminates SL gap window.

## 2026-05-06: ASTER unprotected position — MIN_SL_NOTIONAL_USD

Бот открыл $13 notional на ASTER, но Nado min SL = $100 → SL не разместился → позиция голая.

**Fix (commit `0eee45a`)**: `MIN_SL_NOTIONAL_USD=100` default. effective_min = max(min_notional, min_sl_notional).

## 2026-05-06: HL `frontend_open_orders` для SL listing (commit `ceed668`)

`info.open_orders()` НЕ возвращает trigger fields (px, isTrigger). Нужен `info.frontend_open_orders()`.

## 2026-05-06: Mark price all_mids cache (commit `7ad1fcc`)

Раньше `mark_price()` звал `info.all_mids()` per-coin. all_mids возвращает ВСЕ coins одной операцией. 270× per-coin = 270× same data + 270× rate limit. Кэш 5s → 1 call вместо 270.

## 2026-05-05 (вечер): HIP-3 xyz возвращён

Юзер: "hip3 включаем" + "у нас прибыль по хип 3. конечно оставляем".

`HIP3_USDC_DEXES = ["xyz"]`, `INCLUDE_HIP3=true` default.

xyz содержит и crypto (HYPE, USEFUL), и stocks (BABA/META/AMZN), и forex (DXY, KRW). Quality filters (EMA align + vol ≥ $10k + has_open_trade_for) должны режить мусор.

## 2026-05-05 (вечер): HIP-3 root cause fix

Юзер: "ты уже чинил хип3. в чем причина что ты тогда не починил"

**Корень**: `Exchange()` создавался без `perp_dexs` → внутренний Info SDK не знал xyz pairs → `update_leverage("xyz:LLY")` падал с KeyError → trade cancelled.

**Fix (commit `38131dc`)**: `Exchange(..., perp_dexs=perp_dexs_list)`.

## 2026-05-05: Nado cancel_orders silent fail

`cancel_orders` работает только для regular orders, НЕ для triggers. SL не отменялся, multi-SL race condition.

**Fix (commit `5a303a6`)**: `cancel_trigger_orders` API. Verified DOGE 6→5 после cancel.

## 2026-05-05: Orphan position bug (Nado) — double-confirm

Nado SDK иногда отдаёт пустой positions из-за кеша/timeout → бот ошибочно закрывал DB записи, теряя позиции на бирже. Юзер обнаружил 4 orphan Nado positions (AAVE/ZEC/DOGE/SOL) которые DB пометила closed_externally в 06:49, но на бирже жили. Lost trail/SL management 14h.

**Fix (commit `b5e007e`)**: re-fetch positions через invalidate_cache + retry. Только если DOUBLE confirmation позиция отсутствует — закрываем.

## 2026-05-05: MM metric fix — `crossMaintenanceMarginUsed`

Юзер 4-й раз: "ММ 30% какие 70???"

Я путал initial margin (`marginSummary.totalMarginUsed`) с maintenance margin. UI показывает MAINTENANCE; бот считал INITIAL, выходило 2× больше.

**Fix (commit `beb932f`)**: использовать `state["crossMaintenanceMarginUsed"]` API field прямо. Самопал формула `1/(2*lev)` — только fallback.

## 2026-05-05: Kraken Futures isolated/cross — DOGE liquidation

DOGE 50x позиция partial-liquidated -1% adverse → forced 10% close ($0.62 cost).

**Корень**: KF Flex margin не имеет true cross в API. `set_leverage(0)` returns MAX_LEVERAGE_OUT_OF_BOUNDS. Каждая поза имеет maxFixedLeverage cap = isolated. При maxLev=50 → liq distance 2% → liquidation ДО stop=2.5%.

**Fix (commit `8818796`)**: `KRAKEN_LEVERAGE_CAP = 10` hard cap для всех KF пар. На существующих позах ручной `set_leverage(10, sym)` → 25/50→10x. `MAX_MARGIN_USED_PCT 0.50 → 0.90` в .env (0.90 isolated ≡ 0.50 cross).

## 2026-05-05: DB size != exchange filled size

Юзер: "Реальный slippage хуже backtest. срочно глубокий анализ"

**Находка**: PUMP DB size=17M, exchange=1.22M (14×). Bot записывал INTENDED size в DB; partial fill / liquidity cap → реальная позиция МЕНЬШЕ.

**Fix**: `update_trade_size(trade_id, filled_sz)` после market_open. Real slippage в норме (~0.1-0.8%), не главная проблема.

## 2026-05-05: Backtest data — из бота который торгует

Юзер: "до того как делать тест по какой-то биржи — бери данные из бота который прямо сейчас там торгует, ни откуда больше"

| Биржа | Где данные |
|---|---|
| KF | `rsync kraken-bot:/root/data/kf_4h_full/` |
| HL | `ssh hl-bot "sudo bash -c 'tar c -C /root/data hl_4h_full'" \| tar x` |
| Nado | `kraken-bot:/root/nado_bot/data/` |

## 2026-05-05: EMA crossover sweep — finished

**Победитель**: EMA50/200 Golden Cross + ATR×3 SL + 1d EMA200 trend filter

| Метрика | Pattern bot (current) | EMA winner |
|---|---|---|
| Avg R / trade | +0.214 | **+0.675** (×3.2) |
| CAGR (cmp) | ~+118%/y | **+241%/y** |
| Max DD | ~84% | **56%** |
| Calmar | ~1.4 | **4.31** |
| Trades/year | ~600 | ~218 |

**Status**: research только. Out-of-sample, walk-forward, live paper-trade 2-4 нед нужны прежде чем deploy.

## 2026-05-05: macOS crontab silently dropped deep_audit

**Корень**: macOS crontab silently drops entries требующие Full Disk Access (ssh из cron). Никаких ошибок. ScheduleWakeup session-only — не fire когда Mac в idle.

**Правило**: любая overnight задача → cron на VPS (kraken-bot/hl-bot), НЕ Mac. Linux cron надёжен.

## 2026-05-05: PRODUCTION CONFIG — Option B capital-efficient

См. `docs/strategy.md`.

## 2026-05-04: Kraken Futures Flex margin (DOGE incident)

См. выше + `docs/strategy.md` для leverage cap details.

## 2026-05-04: TS_Hyperliquid_v0.4 + Vyacheslav ТС

См. `docs/roadmap.md` (осцилляторы / Xnn / liquidity cycles).

## 2026-05-04: Nado bot подключён, mainnet

См. `docs/strategy.md` (per-bot config) и `docs/ops.md` (Nado-specific config).

## 2026-05-03: Production config Option B выбран

См. `docs/strategy.md`.

## 2026-05-03: Calmar > Sharpe для оценки edge

Юзер: "крутим стратегию только на качество — никакой косметики"
Нельзя крутить risk/MM для DD reduction (это пропорциональное масштабирование). Только quality filters (trend, breakeven, ADX, vol).

## 2026-05-02: SSH access настроен (hl-bot, kraken-bot)

См. `docs/ops.md`.
