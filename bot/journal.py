"""SQLite журнал сделок и состояния."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from bot.config import DB_PATH

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with _conn(db_path) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                coin TEXT NOT NULL,
                pattern TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                entry REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profits TEXT NOT NULL,
                size REAL NOT NULL,
                risk_dollars REAL NOT NULL,
                rr REAL NOT NULL,
                higher_tf_trend TEXT,
                funding_at_entry REAL,
                status TEXT NOT NULL DEFAULT 'open',
                closed_at TEXT,
                pnl_dollars REAL,
                pnl_pct REAL,
                notes TEXT,
                pyramid_level INTEGER NOT NULL DEFAULT 0,
                parent_trade_id INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_trades_coin_pattern_tf
                ON trades(coin, pattern, timeframe, detected_at);

            CREATE TABLE IF NOT EXISTS rejected_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                coin TEXT NOT NULL,
                pattern TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT NOT NULL,
                rr REAL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # Migrate existing DBs that pre-date the pyramid_level / parent_trade_id columns.
        existing_cols = {r["name"] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
        if "pyramid_level" not in existing_cols:
            con.execute("ALTER TABLE trades ADD COLUMN pyramid_level INTEGER NOT NULL DEFAULT 0")
            log.info("DB migrated: trades.pyramid_level added")
        if "parent_trade_id" not in existing_cols:
            con.execute("ALTER TABLE trades ADD COLUMN parent_trade_id INTEGER")
            log.info("DB migrated: trades.parent_trade_id added")
    log.info("DB initialized at %s", db_path)


# ---------- Bot state ----------
def get_state(key: str, default: str | None = None) -> str | None:
    with _conn() as con:
        row = con.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO bot_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------- Trades ----------
def already_traded(coin: str, pattern: str, timeframe: str, detected_at: str) -> bool:
    """Защита от повторной торговли. Игнорирует dry-run записи."""
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM trades WHERE coin = ? AND pattern = ? AND timeframe = ? "
            "AND detected_at = ? AND (notes IS NULL OR notes != 'dry-run') LIMIT 1",
            (coin, pattern, timeframe, detected_at),
        ).fetchone()
        return row is not None


def has_open_trade_for_tf_dir(coin: str, timeframe: str, direction: str) -> bool:
    """Block opening duplicate trade на (coin, TF, direction) — разные паттерны
    на одной паре в одну сторону на одном TF — это один и тот же setup, не дубль.

    Cross-TF разрешён: 4h BTC long + 1h BTC long → ОК (разные TFs, разные стратегии).
    Same-TF дубль: 1h BTC flag_long + 1h BTC 123_long → блокируем.
    """
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM trades WHERE coin=? AND timeframe=? AND direction=? AND status='open' "
            "AND (notes IS NULL OR notes != 'dry-run') LIMIT 1",
            (coin, timeframe, direction),
        ).fetchone()
        return row is not None


def open_levels_for_tf_dir(coin: str, timeframe: str, direction: str) -> list[dict[str, Any]]:
    """Return all open trades on (coin, tf, dir) ordered by created_at ASC.
    Used by pyramid trigger logic (last entry == levels[-1]; level count = len()).
    """
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades WHERE coin=? AND timeframe=? AND direction=? AND status='open' "
            "AND (notes IS NULL OR notes != 'dry-run') ORDER BY created_at ASC",
            (coin, timeframe, direction),
        ).fetchall()
        return [dict(r) for r in rows]


def insert_trade(
    *,
    coin: str,
    pattern: str,
    timeframe: str,
    direction: str,
    detected_at: str,
    entry: float,
    stop_loss: float,
    take_profits: list[float],
    size: float,
    risk_dollars: float,
    rr: float,
    higher_tf_trend: str | None,
    funding_at_entry: float | None,
    notes: str | None = None,
    pyramid_level: int = 0,
    parent_trade_id: int | None = None,
) -> int:
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO trades(
                created_at, coin, pattern, timeframe, direction, detected_at,
                entry, stop_loss, take_profits, size, risk_dollars, rr,
                higher_tf_trend, funding_at_entry, status, notes,
                pyramid_level, parent_trade_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
            (
                _now_iso(),
                coin,
                pattern,
                timeframe,
                direction,
                detected_at,
                entry,
                stop_loss,
                json.dumps(take_profits),
                size,
                risk_dollars,
                rr,
                higher_tf_trend,
                funding_at_entry,
                notes,
                pyramid_level,
                parent_trade_id,
            ),
        )
        return int(cur.lastrowid)


def insert_rejected(
    *, coin: str, pattern: str, timeframe: str, direction: str, rr: float | None, reason: str
) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO rejected_signals(created_at, coin, pattern, timeframe, direction, rr, reason)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (_now_iso(), coin, pattern, timeframe, direction, rr, reason),
        )


def update_trade_status(
    trade_id: int,
    status: str,
    *,
    pnl_dollars: float | None = None,
    pnl_pct: float | None = None,
    notes: str | None = None,
) -> None:
    with _conn() as con:
        con.execute(
            """UPDATE trades SET status = ?, closed_at = ?, pnl_dollars = ?, pnl_pct = ?,
               notes = COALESCE(?, notes) WHERE id = ?""",
            (status, _now_iso(), pnl_dollars, pnl_pct, notes, trade_id),
        )


def update_trade_size(trade_id: int, new_size: float) -> None:
    """Update DB size to match actual filled size from exchange.

    Bug fix 2026-05-06: bot записывал intended size в DB; при partial fill /
    liquidity cap, exchange имел меньше → planned_risk считался от DB и врал.
    Теперь после market_open реальный filled_sz пишется в DB.
    """
    with _conn() as con:
        con.execute(
            "UPDATE trades SET size = ? WHERE id = ?",
            (new_size, trade_id),
        )


def update_trade_risk_dollars(trade_id: int, new_risk_dollars: float) -> None:
    """Update DB risk_dollars to match actual filled size & entry.

    Bug fix 2026-05-07 (D3): размерная reconciliation 2026-05-06 обновляла
    `size` но не `risk_dollars` — DB показывала risk от $0.93 до $442 для
    одного бота. Этот хелпер используется по парам с update_trade_size.
    """
    with _conn() as con:
        con.execute(
            "UPDATE trades SET risk_dollars = ? WHERE id = ?",
            (new_risk_dollars, trade_id),
        )


def open_trades_for(coin: str) -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades WHERE coin = ? AND status = 'open'", (coin,)
        ).fetchall()
        return [dict(r) for r in rows]


