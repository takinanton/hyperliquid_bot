"""Конфигурация бота. Все настройки из .env + константы."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Загружаем .env из корня проекта
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = PROJECT_ROOT / ".env"


def _warn_duplicate_env_keys(path: Path) -> None:
    if not path.exists():
        return
    seen: dict[str, int] = {}
    dups: list[tuple[str, int, int]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for ln, raw in enumerate(f, 1):
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k = s.split("=", 1)[0].strip()
            if k in seen:
                dups.append((k, seen[k], ln))
            else:
                seen[k] = ln
    if dups:
        import sys
        for k, first, last in dups:
            print(
                f"[config] WARN duplicate env key '{k}' in {path} "
                f"at lines {first} and {last} — dotenv last-wins; "
                f"keep ONE line (line {last} overrides line {first})",
                file=sys.stderr, flush=True,
            )


_warn_duplicate_env_keys(_ENV_PATH)
load_dotenv(_ENV_PATH)


def _get(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Не задана переменная окружения: {key}")
    return val if val is not None else ""


def _get_float(key: str, default: float) -> float:
    val = os.getenv(key)
    return float(val) if val else default


def _get_int(key: str, default: int) -> int:
    val = os.getenv(key)
    return int(val) if val else default


@dataclass(frozen=True)
class Settings:
    # --- Биржа ---
    exchange: str  # 'hyperliquid' | 'kraken'

    # --- Сеть и ключи (HL) ---
    network: str
    agent_private_key: str
    account_address: str

    # --- Kraken Futures ---
    kraken_api_key: str
    kraken_api_secret: str

    # --- Стратегия ---
    risk_per_trade: float
    min_rr: float
    min_rr_countertrend: float
    leverage_mode: str
    slippage: float  # max slippage tolerance for SDK market_open (HL/Nado)

    # --- Цикл ---
    loop_interval_sec: int

    # --- Funding ---
    funding_block_threshold: float

    # --- Exit mode (vstop_struct) ---
    struct_buffer_pct: float  # Структурный буфер: 0.003 = stop на 0.3% за swing

    # --- Nado (Vertex) ---
    # Nado subaccount name (обычно "default"). Используется только NadoClient,
    # но поле в shared Settings чтобы git pull не ломал nado_bot.
    nado_subaccount: str = "default"

    # --- Pacifica (Solana perp DEX) ---
    pacifica_private_key: str = ""           # Solana keypair base58 (signs requests)
    pacifica_agent_private_key: str = ""     # Agent keypair (alternative signing key)
    pacifica_account_address: str = ""       # Main wallet pubkey (account in payloads)

    # --- Extended (Starknet perp DEX, ex-X10) ---
    extended_api_key: str = ""               # X-Api-Key header value
    extended_stark_private: str = ""         # Stark L2 private key hex (signs every write)
    extended_stark_public: str = ""          # Stark L2 public key hex (sanity / order ref)
    extended_vault_id: int = 0               # l2_vault from onboarding — обязательно для StarkPerpetualAccount
    extended_account_id: str = ""            # OnBoardedAccount.account.id (для логов / API)
    extended_eth_address: str = ""           # Public ETH wallet address (для логов; private seed offline у юзера)

    # --- Interactive Brokers ---
    ib_gateway_host: str = "127.0.0.1"       # IB Gateway TCP host (default localhost)
    ib_gateway_port: int = 4002              # 4002 = paper, 4001 = live
    ib_client_id: int = 1                    # IB API clientId (unique per connection)
    ib_account: str = ""                     # IB account code (DUxxx for paper, Uxxx live); blank = first

    # --- Filters & dynamic sizing (used by IB; opt-in for crypto bots) ---
    liquidity_min_4h_usd: float = 0.0        # 0 = disabled. >0 = skip signal if last 4h volume × close < threshold
    vix_sizing_enabled: bool = False         # if True: pos size × 0.5 (VIX>25), × 1.5 (VIX<14), else × 1.0
    vix_high_threshold: float = 25.0
    vix_low_threshold: float = 14.0
    vix_size_high: float = 0.5
    vix_size_low: float = 1.5
    # long_only/long_only_exempt_sec_types удалены 2026-05-12 — IB long-only
    # теперь hardcoded в bot/main.py (exchange == "ib"); FX rip убрал необходимость
    # в CFD exempt mechanism. Backup: data/manual_ops/2026-05-12_ib_rip_fx_shorts/.

    # --- Pyramiding (add-on entries on the same coin/dir while in profit) ---
    # Backtest 2026-05-09: HL +21% net_R / +65% DD; KF +20% net_R / +47% DD.
    # Defaults sized after that backtest: total levels capped at 3 (initial + 2),
    # trigger requires +1.0R unrealized vs LAST level, flat 1% risk per level.
    pyramid_enabled: bool = False
    pyramid_trigger_r: float = 1.0           # unrealized R threshold from last open level
    max_pyramid_levels: int = 3              # total levels per (coin, tf, dir), 1 = no add-ons
    max_drawdown_pct_halt: float = 0.0       # 0 = disabled. >0 = halt new entries when equity_dd >= X%.

    @classmethod
    def from_env(cls) -> "Settings":
        exch = _get("EXCHANGE", "hyperliquid").lower()
        # HL keys required only if exchange=hyperliquid
        hl_required = (exch == "hyperliquid")
        # Kraken Futures keys required only if exchange=kraken
        kraken_required = (exch == "kraken")
        # Pacifica keys required only if exchange=pacifica
        pacifica_required = (exch == "pacifica")
        # Extended keys required only if exchange=extended
        extended_required = (exch == "extended")
        return cls(
            exchange=exch,
            network=_get("HYPERLIQUID_NETWORK" if hl_required else "NETWORK", "testnet").lower(),
            agent_private_key=_get("HYPERLIQUID_AGENT_PRIVATE_KEY", "", required=hl_required),
            account_address=_get("HYPERLIQUID_ACCOUNT_ADDRESS", "", required=hl_required),
            kraken_api_key=_get("KRAKEN_API_KEY", "", required=kraken_required),
            kraken_api_secret=_get("KRAKEN_API_SECRET", "", required=kraken_required),
            risk_per_trade=_get_float("RISK_PER_TRADE", 0.01),
            min_rr=_get_float("MIN_RR", 1.5),
            min_rr_countertrend=_get_float("MIN_RR_COUNTERTREND", 2.0),
            leverage_mode=_get("LEVERAGE_MODE", "max").lower(),
            slippage=_get_float("SLIPPAGE", 0.01),
            loop_interval_sec=_get_int("LOOP_INTERVAL_SEC", 300),
            funding_block_threshold=_get_float("FUNDING_BLOCK_THRESHOLD", 0.0005),
            struct_buffer_pct=_get_float("STRUCT_BUFFER_PCT", 0.003),
            nado_subaccount=_get("NADO_SUBACCOUNT", "default"),
            pacifica_private_key=_get("PACIFICA_PRIVATE_KEY", "", required=pacifica_required),
            pacifica_agent_private_key=_get("PACIFICA_AGENT_PRIVATE_KEY", ""),
            pacifica_account_address=_get("PACIFICA_ACCOUNT_ADDRESS", "", required=pacifica_required),
            extended_api_key=_get("EXTENDED_API_KEY", "", required=extended_required),
            extended_stark_private=_get("EXTENDED_STARK_PRIVATE", "", required=extended_required),
            extended_stark_public=_get("EXTENDED_STARK_PUBLIC", "", required=extended_required),
            extended_vault_id=_get_int("EXTENDED_VAULT_ID", 0),
            extended_account_id=_get("EXTENDED_ACCOUNT_ID", ""),
            extended_eth_address=_get("EXTENDED_ETH_ADDRESS", ""),
            ib_gateway_host=_get("IB_GATEWAY_HOST", "127.0.0.1"),
            ib_gateway_port=_get_int("IB_GATEWAY_PORT", 4002),
            ib_client_id=_get_int("IB_CLIENT_ID", 1),
            ib_account=_get("IB_ACCOUNT", ""),
            liquidity_min_4h_usd=_get_float("LIQUIDITY_MIN_4H_USD", 0.0),
            vix_sizing_enabled=_get("VIX_SIZING_ENABLED", "false").lower() in ("true", "1", "yes"),
            vix_high_threshold=_get_float("VIX_HIGH_THRESHOLD", 25.0),
            vix_low_threshold=_get_float("VIX_LOW_THRESHOLD", 14.0),
            vix_size_high=_get_float("VIX_SIZE_HIGH", 0.5),
            vix_size_low=_get_float("VIX_SIZE_LOW", 1.5),
            pyramid_enabled=_get("PYRAMID_ENABLED", "false").lower() in ("true", "1", "yes"),
            pyramid_trigger_r=_get_float("PYRAMID_TRIGGER_R", 1.0),
            max_pyramid_levels=_get_int("MAX_PYRAMID_LEVELS", 3),
            max_drawdown_pct_halt=_get_float("MAX_DRAWDOWN_PCT_HALT", 0.0),
        )


# --- Константы стратегии MVP ---

def _coins_from_env() -> list[str]:
    """Возвращает coin list:
    COIN_FILTER:
      'none' (default) → ВСЕ доступные main + xyz HIP-3, без cap (~300+).
                         Фильтрация только в момент pre-trade (live).
      'liquidity'      → startup-фильтр по OI + 24h volume + xyz bypass.
      'volume'         → top-N по 24h volume (legacy).

    Override: COINS env var → точный список через запятую.
    """
    raw = os.getenv("COINS", "").strip()
    if raw:
        # Bug fix 2026-05-05: Nado использует lowercase k-prefix для 1000x токенов
        # (kPEPE-PERP, kBONK-PERP). Сохраняем case если символ начинается с 'k'+upper.
        out = []
        for c in raw.split(","):
            c = c.strip()
            if not c:
                continue
            # k-prefix (Nado 1000x): kPEPE, kBONK — НЕ апперкейсим первую букву
            if len(c) >= 2 and c[0] == "k" and c[1].isupper():
                out.append(c)
            else:
                out.append(c.upper())
        return out

    coin_filter = os.getenv("COIN_FILTER", "none").lower()
    # 2026-05-05 (вечер): юзер вернул HIP-3 xyz обратно. Default INCLUDE_HIP3=true.
    # Содержит и акции (BABA/META/AMZN), и crypto. Quality filters (EMA align +
    # vol min) должны режить мусор. Если в проде акции не открываются — добавить
    # explicit crypto-whitelist для xyz subset.
    include_hip3 = os.getenv("INCLUDE_HIP3", "true").lower() in ("true", "1", "yes")
    hip3_whitelist = {"xyz"} if include_hip3 else set()

    # Kraken Futures: использовать ccxt markets (HL liquidity API не работает для KF)
    exchange = os.getenv("EXCHANGE", "hyperliquid").lower()
    if exchange == "kraken" and coin_filter in ("none", "liquidity"):
        try:
            import ccxt
            ex = ccxt.krakenfutures({"enableRateLimit": True})
            ex.load_markets()
            coins = sorted(s for s, m in ex.markets.items()
                           if m.get("swap") and m.get("linear"))
            if coins:
                return coins
        except Exception:
            pass

    # IB: auto-universe из JSON-файла (built by scripts/build_ib_universe.py).
    # Bot никогда не enumerate в env; universe = всё что прошло crypto-фильтр.
    # Live filter LIQUIDITY_MIN_4H_USD далее режет неликвид per-scan.
    if exchange == "ib":
        coins_file = os.getenv("COINS_FILE", "data/ib_universe.json")
        try:
            import json as _json
            from pathlib import Path as _Path
            p = _Path(coins_file)
            if p.exists():
                data = _json.loads(p.read_text())
                coins = [x["sym"] for x in data.get("symbols", [])]
                if coins:
                    return coins
            else:
                # silent — main.py will warn if universe is empty
                pass
        except Exception:
            pass

    if coin_filter == "none":
        # Все пары — фильтр только pre-trade (более точно, отражает live state)
        try:
            from bot.liquidity import select_all_pairs
            coins = select_all_pairs(hip3_whitelist=hip3_whitelist)
            if coins:
                return coins
        except Exception:
            pass

    top_n_str = os.getenv("COINS_TOP_N", "").strip()
    n = 100
    if top_n_str:
        try:
            n = int(top_n_str)
        except ValueError:
            pass

    if coin_filter == "liquidity":
        try:
            from bot.liquidity import select_liquid_coins
            min_oi = float(os.getenv("MIN_OI_USD", "200000"))
            min_vol = float(os.getenv("MIN_VOLUME_24H_USD", "500000"))
            coins = select_liquid_coins(
                n=n, min_oi_usd=min_oi, min_vol_24h_usd=min_vol,
                hip3_whitelist=hip3_whitelist,
            )
            if coins:
                return coins
        except Exception:
            pass

    # Legacy fallback на top-N by 24h volume
    if coin_filter == "volume" and top_n_str:
        try:
            return _fetch_top_coins(n, include_hip3=include_hip3)
        except (ValueError, RuntimeError):
            pass

    return ["BTC", "ETH", "SOL", "BNB", "DOGE"]


def _fetch_top_coins(n: int, include_hip3: bool = True) -> list[str]:
    """Тянет топ-N пар mainnet HL по 24h volume. Включает HIP-3 dexes если флаг."""
    import requests as _r
    url = "https://api.hyperliquid.xyz/info"
    pairs: list[tuple[str, float]] = []
    try:
        ctxs = _r.post(url, json={"type": "metaAndAssetCtxs"}, timeout=15).json()
        if isinstance(ctxs, list) and len(ctxs) >= 2:
            for i, a in enumerate(ctxs[0].get("universe", [])):
                if i >= len(ctxs[1]):
                    continue
                vol = float(ctxs[1][i].get("dayNtlVlm", 0) or 0)
                pairs.append((a["name"], vol))
    except Exception:
        pass

    if include_hip3:
        # Whitelist: только HIP-3 dexes с USDC margin (xyz).
        # cash/flx/vntl/hyna/km/abcd/para используют другие collateral-токены —
        # бот не может их торговать без соответствующего токена в кошельке.
        HIP3_USDC_DEXES = {"xyz"}
        try:
            dexs = _r.post(url, json={"type": "perpDexs"}, timeout=15).json()
            if isinstance(dexs, list):
                for d in dexs:
                    if d is None:
                        continue
                    dex_name = d.get("name") if isinstance(d, dict) else None
                    if not dex_name or dex_name not in HIP3_USDC_DEXES:
                        continue
                    try:
                        cts = _r.post(url, json={"type": "metaAndAssetCtxs", "dex": dex_name}, timeout=15).json()
                        if isinstance(cts, list) and len(cts) >= 2:
                            for i, a in enumerate(cts[0].get("universe", [])):
                                if i >= len(cts[1]):
                                    continue
                                vol = float(cts[1][i].get("dayNtlVlm", 0) or 0)
                                pairs.append((a["name"], vol))
                    except Exception:
                        continue
        except Exception:
            pass

    pairs.sort(key=lambda x: -x[1])
    return [name for name, vol in pairs if vol > 0][:n]


COINS: list[str] = _coins_from_env()


def _pyramid_excluded_patterns_from_env() -> set[str]:
    """Patterns where pyramid add-ons are disabled (initial entry still allowed).
    Backtest 2026-05-09: triangle_short and 123_short showed avgR drop on add-ons
    (HL: triangle_short 0.15→-0.01; KF: 123_short 0.86→0.66). Override via env
    PYRAMID_EXCLUDED_PATTERNS=pat1,pat2.
    """
    raw = os.getenv("PYRAMID_EXCLUDED_PATTERNS", "").strip()
    if raw:
        return {p.strip() for p in raw.split(",") if p.strip()}
    return {"triangle_short", "123_short"}


PYRAMID_EXCLUDED_PATTERNS: set[str] = _pyramid_excluded_patterns_from_env()

# Ручной override тренда от пользователя.
# auto:    обычная логика (swings + EMA filter)
# up:      разрешены ТОЛЬКО longs (игнор детектора)
# down:    разрешены ТОЛЬКО shorts
# neutral: пауза, новых не открывает (управление существующими через Vstop остаётся)
FORCE_TREND: str = _get("FORCE_TREND", "auto").lower()


def _coins_set_from_env(key: str) -> set[str]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return set()
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


# Per-coin trend bias (юзер фундаментально верит в тренд по этим парам).
# Для пар в FORCE_LONG_COINS:
#   - higher_trend форсится "up" (детектор не блокирует longs из-за EMA/swings)
#   - shorts на этих парах НЕ открываются
#   - используется обычный (не countertrend) MIN_RR
# Для FORCE_SHORT_COINS — симметрично.
# Пример .env:
#   FORCE_LONG_COINS=VIRTUAL,TAO
#   FORCE_SHORT_COINS=
FORCE_LONG_COINS: set[str] = _coins_set_from_env("FORCE_LONG_COINS")
FORCE_SHORT_COINS: set[str] = _coins_set_from_env("FORCE_SHORT_COINS")


def _parse_pattern_rr(env_str: str) -> dict[str, float]:
    """Parse 'flag_short:1.5,triangle_short:1.7' → {'flag_short': 1.5, 'triangle_short': 1.7}"""
    raw = os.getenv(env_str, "").strip()
    out = {}
    if not raw:
        return out
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        k, v = pair.split(":", 1)
        try:
            out[k.strip()] = float(v.strip())
        except ValueError:
            continue
    return out


# Per-pattern R/R floor (overrides tier-based MIN_RR if higher).
# Юзер 03-05-2026: flag_short → 1.5 (audit показал avg +0.13R vs +0.08R baseline).
# triangle_short — НЕ trogat (audit показал что строже R/R хуже).
# Format env: PATTERN_MIN_RR=flag_short:1.5,123_short:1.2
PATTERN_MIN_RR: dict[str, float] = _parse_pattern_rr("PATTERN_MIN_RR") or {
    "flag_short": 1.5,
}

# Max selectivity filters (юзер просил "макс эффективность" 03-05-2026).
# Бот без них открывает 6+ trades/день при первой возможности → margin забит.
# С этими — берём только топ-3 signal по R/R за день, max 1 за cycle.
MAX_OPENS_PER_DAY: int = _get_int("MAX_OPENS_PER_DAY", 3)
MAX_OPENS_PER_CYCLE: int = _get_int("MAX_OPENS_PER_CYCLE", 1)

# Coin blacklist — ПО УМОЛЧАНИЮ ПУСТОЙ.
# ⛔ НЕ добавляй сюда пары на основании прошлой производительности.
# Юзер явно (дважды) сказал не убирать пары вручную: режим у пары меняется,
# вчерашний лузер завтра может быть winner. Доверяемся RR + tier + caps.
# Используй ТОЛЬКО если юзер ЯВНО просит исключить конкретную пару.
COIN_BLACKLIST: set[str] = _coins_set_from_env("COIN_BLACKLIST")

# Adaptive R/R по leverage: пары с низким max_leverage сжигают много margin
# per slot, требуем их только при сильно положительном R/R.
#
# Реальные max_leverage на HL (проверено 02-05-2026):
#   3x:  REZ, ORDI и пр. свежие/илликвидные крипто-листинги (НЕ HIP-3 stocks!)
#   5x:  FET, STRK, ZEN и пр. альтсы среднего ранга
#   10x: DOGE, DOT, UNI; HIP-3 stocks (TSLA, GOOGL, MSFT, AMZN, META, etc.)
#   20x: SOL, NVDA (HIP-3), AAPL (HIP-3), GOLD (HIP-3)
#   25x: ETH, SILVER (HIP-3)
#   30x: XYZ100 (HIP-3 S&P futures)
#   40x: BTC
#   50x: JPY/EUR (HIP-3 forex)
#
# То есть HIP-3 stocks НЕ low-leverage — они почти все 10-25x.
# Реально 3x фильтрит мелкие alt-крипто (REZ/ORDI). 5x — средние альты (FET).
#
# Двухуровневый фильтр (default):
#   leverage ≤ 3  → R/R ≥ 1.5 (REZ, ORDI и т.п.)
#   leverage ≤ 5  → R/R ≥ 1.1 (FET, STRK, ZEN)
#   leverage ≥ 6  → standard MIN_RR (=0.9)
LOW_LEV_THRESHOLD: int = _get_int("LOW_LEV_THRESHOLD", 3)
LOW_LEV_MIN_RR: float = _get_float("LOW_LEV_MIN_RR", 1.5)
MID_LEV_THRESHOLD: int = _get_int("MID_LEV_THRESHOLD", 5)
MID_LEV_MIN_RR: float = _get_float("MID_LEV_MIN_RR", 1.1)

WORKING_TF: str = _get("WORKING_TF", "1h")
HIGHER_TF: str = _get("HIGHER_TF", "4h")

# Multi-TF support (2026-05-08): WORKING_TFS=4h,1h в .env → бот сканирует
# каждый coin на КАЖДОМ из перечисленных TFs за один цикл. higher_TF выбирается
# из WORKING_HIGHER_PAIRS (или auto-mapping снизу).
# Если WORKING_TFS пустой — backward-compat: используется единственный WORKING_TF.
def _parse_tfs(s: str) -> list[str]:
    return [t.strip() for t in s.split(",") if t.strip()]

WORKING_TFS: list[str] = _parse_tfs(_get("WORKING_TFS", "")) or [WORKING_TF]

# Higher-TF mapping. Default: 1h→4h, 4h→1d, 8h→1d, 1d→1w. Override via env:
# WORKING_HIGHER_PAIRS=4h:1d,1h:4h
_DEFAULT_HIGHER_FOR = {"15m": "1h", "30m": "2h", "1h": "4h", "2h": "8h", "4h": "1d", "8h": "1d", "12h": "1d", "1d": "1w"}

def _parse_pair_map(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for kv in s.split(","):
        kv = kv.strip()
        if ":" in kv:
            k, v = kv.split(":", 1)
            out[k.strip()] = v.strip()
    return out

HIGHER_TF_MAP: dict[str, str] = _parse_pair_map(_get("WORKING_HIGHER_PAIRS", ""))
# Fill in defaults for any working TF without explicit higher mapping
for _tf in WORKING_TFS:
    if _tf not in HIGHER_TF_MAP:
        HIGHER_TF_MAP[_tf] = _DEFAULT_HIGHER_FOR.get(_tf, HIGHER_TF)
# Ensure legacy WORKING_TF also has higher mapping (backward compat)
if WORKING_TF not in HIGHER_TF_MAP:
    HIGHER_TF_MAP[WORKING_TF] = HIGHER_TF

CANDLES_LIMIT: int = 200

EMA_LENGTH: int = 200

# Соответствие TF в миллисекундах для запросов
TF_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}

# Путь к БД журнала
DB_PATH: Path = PROJECT_ROOT / "data" / "trades.db"
