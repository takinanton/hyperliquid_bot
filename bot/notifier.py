"""Telegram notifier — Cat B catastrophe alerts only.

По правилам Self-Fix Mode (CLAUDE.md):
- Cat A (silent self-fix) — Claude правит, юзеру НЕ пишет
- Cat B (catastrophe) — пишет в Telegram юзеру
- Cat C (digest) — daily/weekly summary раз в день

Юзер явно сказал: "не дёргать на каждое движение, только на серьёзное".

Использование:
    from bot.notifier import Notifier
    n = Notifier()
    n.critical("Bot down >30min, не могу перезапустить")  # Cat B
    n.daily_summary("...")  # Cat C
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

log = logging.getLogger(__name__)


class Notifier:
    """Шлёт сообщения в Telegram. Cat B (urgent) и Cat C (digest)."""

    def __init__(self) -> None:
        self.token = os.environ.get("TG_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TG_CHAT_ID", "").strip()
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            log.warning(
                "Notifier disabled — TG_BOT_TOKEN/TG_CHAT_ID не заданы в env"
            )
        # Анти-спам: одно и то же critical сообщение шлём максимум раз в час.
        self._last_sent: dict[str, float] = {}
        self._dedup_ttl = 3600.0  # 1h

    # ---------- Низкоуровневая отправка ----------
    def _post(self, text: str, parse_mode: Optional[str] = None) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            r = requests.post(url, data=payload, timeout=10)
            data = r.json()
            if not data.get("ok"):
                log.error("Telegram send failed: %s", data)
                return False
            return True
        except Exception as e:
            log.error("Telegram exception: %s", e)
            return False

    # ---------- Категория B — catastrophe ----------
    def critical(self, message: str, dedup_key: Optional[str] = None) -> bool:
        """NO-OP (user 2026-05-04: only equity_monitor sends TG for DD>15%/24h)."""
        import logging
        logging.getLogger(__name__).info('Notifier (silent): %s', message[:150])
        return False

    def daily_summary(self, body: str) -> bool:
        """NO-OP (user 2026-05-04: only equity_monitor sends TG)."""
        import logging
        logging.getLogger(__name__).info('Notifier.daily_summary (silent): %s', str(body)[:150])
        return False

    def info(self, message: str) -> bool:
        """NO-OP (user 2026-05-04: only equity_monitor sends TG)."""
        import logging
        logging.getLogger(__name__).info('Notifier.info (silent): %s', str(message)[:150])
        return False

    def ping(self) -> bool:
        """Проверка коннекта (использовать в смоук-тестах)."""
        return self._post("🤖 ping ok")


def check_maintenance_margin(*args, **kwargs) -> bool:
    """NO-OP stub (Ripped 2026-05-16). Live MM% alert ушёл в scripts/mm_monitor.py.

    Каркас оставлен чтобы bot.main:34 import не сломать. Старая логика
    самопальной MM-формулы (1/(2*lev)) была неточной и всё равно звала
    notifier.critical() который NO-OP с 2026-05-04.
    """
    return False


