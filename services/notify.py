"""Сповіщення адміну в Telegram: запуск бота, серйозні помилки (з обмеженням частоти)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from datetime import tzinfo as TzInfo

from aiogram import Bot

from database.db import Database
from services.cards import escape

logger = logging.getLogger(__name__)

META_ALIVE = "last_alive_at"          # «пульс» бота: коли востаннє точно працював
ERROR_THROTTLE_SECONDS = 600          # однакову помилку надсилаємо не частіше, ніж раз на 10 хвилин
DOWNTIME_NOTICE_AFTER = timedelta(minutes=30)


class AdminNotifier:
    def __init__(self, bot: Bot, admin_id: int) -> None:
        self.bot = bot
        self.admin_id = admin_id
        self._last_error: dict[str, float] = {}

    async def send(self, text: str) -> bool:
        try:
            await self.bot.send_message(self.admin_id, text)
            return True
        except Exception as exc:  # сповіщення не повинні ламати роботу бота
            logger.warning("Не вдалося надіслати сповіщення адміну: %s", exc)
            return False

    async def error(self, exc: BaseException, where: str = "") -> bool:
        """Повідомляє про помилку. Однакові помилки (тип + місце) не частіше ніж раз на 10 хвилин."""
        key = f"{type(exc).__name__}:{where}"
        now = time.monotonic()
        if now - self._last_error.get(key, -ERROR_THROTTLE_SECONDS) < ERROR_THROTTLE_SECONDS:
            return False
        self._last_error[key] = now
        details = escape(f"{type(exc).__name__}: {exc}")[:500]
        place = f" ({escape(where)})" if where else ""
        return await self.send(f"🔴 <b>Помилка в боті</b>{place}\n<code>{details}</code>\nПодробиці — у логах.")


async def heartbeat(db: Database, tz: TzInfo) -> None:
    """Записує мітку «бот живий» — за нею після відключення світла видно, скільки тривала пауза."""
    try:
        await db.set_meta(META_ALIVE, datetime.now(tz).isoformat(timespec="seconds"))
    except Exception:
        logger.exception("Не вдалося записати пульс бота")


async def announce_start(notifier: AdminNotifier, db: Database, tz: TzInfo) -> None:
    """«Бот запущено» + інформація про паузу (якщо була)."""
    now = datetime.now(tz)
    note = ""
    previous = await db.get_meta(META_ALIVE)
    if previous:
        try:
            gap = now - datetime.fromisoformat(previous)
            if gap >= DOWNTIME_NOTICE_AFTER:
                hours, minutes = divmod(int(gap.total_seconds()) // 60, 60)
                since = datetime.fromisoformat(previous).strftime("%d.%m %H:%M")
                note = f"\n⏸ Перерва: приблизно {hours} год {minutes} хв (востаннє бачили бота {since})."
        except ValueError:
            pass
    users = await db.count_users(only_active=True)
    total = await db.count_users()
    await heartbeat(db, tz)
    await notifier.send(
        f"🟢 <b>Бот запущено</b> · {now.strftime('%d.%m.%Y %H:%M')}\n"
        f"Користувачів: {users} активних із {total}.{note}"
    )
