"""Планувальник розсилки: кожні 2 години, строге чергування предметів, тихий режим."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import tzinfo as TzInfo

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database.db import Database
from database.models import User
from services.cards import SUBJECT_HIST, SUBJECT_LANG, Card, build_card, send_card

logger = logging.getLogger(__name__)

META_LAST_SUBJECT = "last_subject"
SEND_DELAY = 0.05  # пауза між повідомленнями (ліміт Telegram ≈ 30 повідомлень/с)


def is_quiet_hour(hour: int, start: int, end: int) -> bool:
    """Чи потрапляє година в тихий режим [start, end). Підтримує перехід через північ."""
    if start == end:
        return False
    if start > end:  # напр. 22 -> 8
        return hour >= start or hour < end
    return start <= hour < end


def next_subject(last: str | None) -> str:
    """Строге чергування: історія -> мова -> історія -> ..."""
    return SUBJECT_LANG if last == SUBJECT_HIST else SUBJECT_HIST


class Broadcaster:
    def __init__(self, bot: Bot, db: Database, tz: TzInfo) -> None:
        self.bot = bot
        self.db = db
        self.tz = tz
        self._lock = asyncio.Lock()  # щоб два запуски не перетиналися

    async def broadcast(self) -> None:
        async with self._lock:
            try:
                await self._broadcast()
            except Exception:  # планувальник не повинен падати від помилки одного запуску
                logger.exception("Помилка під час розсилки")

    async def _broadcast(self) -> None:
        now = datetime.now(self.tz)
        users = await self.db.get_active_users()
        recipients = [u for u in users if not is_quiet_hour(now.hour, u.quiet_start, u.quiet_end)]
        logger.info(
            "Розсилка %s: активних %d, поза тихим режимом %d",
            now.strftime("%Y-%m-%d %H:%M %Z"), len(users), len(recipients),
        )
        if not recipients:
            # Черга НЕ зсувається, щоб чергування предметів залишалося строгим
            return

        subject = next_subject(await self.db.get_meta(META_LAST_SUBJECT))
        card = await build_card(self.db, subject)
        if card is None:
            logger.warning("Немає матеріалів для предмета «%s» — розсилку пропущено", subject)
            return

        sent = failed = 0
        for user in recipients:
            if await self._send_to_user(user, card):
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(SEND_DELAY)

        # Зберігаємо стан чергування в БД — він переживе перезапуск
        await self.db.set_meta(META_LAST_SUBJECT, subject)
        logger.info(
            "Розсилку завершено: предмет=%s, картка=%s#%s, успішно=%d, помилок=%d",
            subject, card.kind, card.item_id, sent, failed,
        )

    async def _send_to_user(self, user: User, card: Card) -> bool:
        for attempt in (1, 2):
            try:
                await send_card(self.bot, user.user_id, card)
                return True
            except TelegramRetryAfter as exc:
                logger.warning("Flood control: чекаю %s с (користувач %s)", exc.retry_after, user.user_id)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramForbiddenError:
                logger.info("Користувач %s заблокував бота — деактивую", user.user_id)
                await self.db.set_user_active(user.user_id, False)
                return False
            except TelegramBadRequest as exc:
                if "chat not found" in str(exc).lower():
                    logger.info("Чат %s не знайдено — деактивую", user.user_id)
                    await self.db.set_user_active(user.user_id, False)
                else:
                    logger.error("BadRequest для %s: %s", user.user_id, exc)
                return False
            except TelegramAPIError as exc:
                logger.error("Telegram API помилка для %s (спроба %d): %s", user.user_id, attempt, exc)
                await asyncio.sleep(1)
            except Exception:
                logger.exception("Непередбачена помилка при відправці користувачу %s", user.user_id)
                return False
        return False


def setup_scheduler(bot: Bot, db: Database, tz: TzInfo) -> AsyncIOScheduler:
    """Створює планувальник. Запускати (scheduler.start()) потрібно вже всередині event loop."""
    scheduler = AsyncIOScheduler(timezone=tz)
    broadcaster = Broadcaster(bot, db, tz)
    # Кожні 2 години за годинником (00:00, 02:00, ... 22:00 за Києвом) —
    # розклад не «з'їжджає» після перезапуску, на відміну від interval-тригера.
    scheduler.add_job(
        broadcaster.broadcast,
        CronTrigger(hour="*/2", minute=0, timezone=tz),
        id="broadcast",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
    )
    logger.info("Планувальник налаштовано: розсилка кожні 2 години (%s)", tz)
    return scheduler
