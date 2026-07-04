#!/usr/bin/env python3
"""Watchdog для kraken-bot — exchange-agnostic.

Каждые 5 мин (cron):
1. Получает все открытые позиции на бирже
2. Получает все open orders (стопы)
3. Для каждой позы — есть ли SL trigger order?
4. Если нет — берёт current_stop из БД trades.notes, ставит SL
5. Если в БД нет — ALERT (нечего ставить, нужен ручной разбор)

Запуск:
  python3 watchdog_kraken.py                # auto-detect EXCHANGE из .env
  python3 watchdog_kraken.py --dry-run      # только посмотреть
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from bot.config import Settings
from bot.exchange_factory import get_exchange_client

LOG_FILE = os.environ.get("WATCHDOG_LOG", "/var/log/kraken-watchdog.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
log = logging.getLogger("watchdog")
log.addHandler(logging.StreamHandler(sys.stderr))

DB_PATH = PROJECT_ROOT / "data" / "trades.db"


def get_open_trade_for_coin(coin: str) -> dict | None:
    """Returns most recent open trade record for this coin from journal."""
    if not DB_PATH.exists():
        return None
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM trades WHERE coin=? AND status='open' ORDER BY id DESC LIMIT 1",
        (coin,),
    ).fetchall()
    con.close()
    return dict(rows[0]) if rows else None


def parse_current_stop(notes_json: str | None, fallback_sl: float) -> float:
    if not notes_json:
        return fallback_sl
    try:
        d = json.loads(notes_json)
        return float(d.get("current_stop", fallback_sl))
    except Exception:
        return fallback_sl


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_env()
    log.info("Watchdog cycle starting on %s", settings.exchange)

    try:
        client = get_exchange_client(settings)
    except Exception as e:
        log.error("Cannot init client: %s", e)
        _alert_critical(f"client init failed на {settings.exchange}: {str(e)[:150]}")
        return 1

    # Get open positions
    try:
        positions = client.open_positions()
    except Exception as e:
        log.error("open_positions failed: %s", e)
        _alert_critical(f"open_positions failed на {settings.exchange}: {str(e)[:150]}")
        return 1

    if not positions:
        log.info("No open positions — nothing to watch")
        return 0

    # Get open orders
    try:
        orders = client.exchange.fetch_open_orders()
    except Exception as e:
        log.error("fetch_open_orders failed: %s", e)
        orders = []

    # Build map: coin → has trigger order (reduceOnly stop)
    has_sl_for: dict[str, bool] = {}
    for o in orders:
        sym = o.get("symbol")
        otype = o.get("type", "").lower()
        reduce = o.get("reduceOnly", False) or o.get("info", {}).get("reduceOnly")
        if not sym:
            continue
        if "stop" in otype or otype in ("trigger", "stop_loss", "stop-loss-limit"):
            has_sl_for[sym] = True

    # Check each position
    placed = 0
    failed = 0
    naked = []
    for coin, pos in positions.items():
        if has_sl_for.get(coin):
            log.info("✓ %s has SL", coin)
            continue
        # NO SL — need to recover
        sz = abs(float(pos.get("szi", 0)))
        if sz <= 0:
            continue
        is_long = float(pos.get("szi", 0)) > 0
        log.warning("⚠️ %s position WITHOUT SL (size=%s)", coin, sz)
        naked.append(coin)

        # Find SL price from journal
        trade = get_open_trade_for_coin(coin)
        if not trade:
            log.error("%s: position на бирже но НЕТ open trade в БД — пропускаю", coin)
            failed += 1
            continue
        trigger_px = parse_current_stop(trade.get("notes"), float(trade.get("stop_loss", 0)))
        if trigger_px <= 0:
            log.error("%s: trigger_px invalid (%s) — пропускаю", coin, trigger_px)
            failed += 1
            continue

        if args.dry_run:
            log.info("DRY: would place SL for %s @ %.6f size=%s", coin, trigger_px, sz)
            continue

        # Place SL
        is_buy_to_close = not is_long
        try:
            resp = client.trigger_sl(coin, is_buy=is_buy_to_close, sz=sz, trigger_px=trigger_px)
            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "error" in statuses[0]:
                log.error("%s SL placement rejected: %s", coin, statuses[0]["error"])
                failed += 1
            else:
                log.warning("✓ %s SL recovered @ %.6f", coin, trigger_px)
                placed += 1
        except Exception as e:
            log.exception("%s SL placement failed: %s", coin, e)
            failed += 1

    log.warning(
        "Watchdog cycle done: placed=%d failed=%d (positions=%d, naked=%s)",
        placed, failed, len(positions), naked,
    )
    # Telegram alert если был хоть один naked OR failed
    if naked or failed > 0:
        try:
            from bot.notifier import Notifier
            n = Notifier()
            if n.enabled:
                bot_name = settings.exchange
                msg = (f"⚠️ {bot_name} watchdog: naked={naked} failed_recovery={failed} "
                       f"placed={placed} (cycle done)")
                n.critical(msg, dedup_key=f"watchdog_naked_{bot_name}")
        except Exception:
            pass
    return 0 if failed == 0 else 1


def _alert_critical(msg: str):
    """Standalone Telegram alert for catastrophic watchdog failures."""
    try:
        from bot.notifier import Notifier
        n = Notifier()
        if n.enabled:
            n.critical(f"🚨 WATCHDOG ITSELF FAILED: {msg}", dedup_key="watchdog_self_fail")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
