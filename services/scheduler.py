"""Планувальник розсилки.

Щогодини (о :00 за київським часом) бот перевіряє кожного користувача:
  • тихий режим — нічого не надсилаємо;
  • власний інтервал (1/2/3/4/6/12/24 год, відлік від кінця тихого режиму);
  • добовий ліміт карток;
  • чергування предметів (індивідуально, серед увімкнених тем);
  • «слово дня» — раз на добу.
Також містить розсилки для адміна (картки, квізи, оголошення) — незалежно від часу й тихого режиму.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from datetime import tzinfo as TzInfo
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from database.db import Database
from database.models import User
from services.cards import (
    SUBJECT_LANG,
    SUBJECT_ORDER,
    SUBJECT_SETTINGS,
    Card,
    build_card,
    card_keyboard,
    render_word_of_day,
    send_card,
)
from services.quiz import QuizSpec, send_quiz
from services.srs import pick_card

logger = logging.getLogger(__name__)

SEND_DELAY = 0.05                                 # пауза між повідомленнями (ліміт Telegram ≈ 30 повідомлень/с)
CATCH_UP_SLACK = timedelta(minutes=10)            # запізнення, після якого картка вважається пропущеною
MIN_GAP_TO_NEXT_TICK = timedelta(minutes=20)      # не догоняти, якщо планова перевірка вже скоро
CATCH_UP_DELAY = 45                               # секунд після старту: дати мережі піднятися
ALLOWED_INTERVALS = (1, 2, 3, 4, 6, 12, 24)


# ---------------------------------------------------------------------- чисті функції (легко тестувати)
def is_quiet_hour(hour: int, start: int, end: int) -> bool:
    """Чи потрапляє година в тихий режим [start, end). Підтримує перехід через північ."""
    if start == end:
        return False
    if start > end:  # напр. 22 -> 8
        return hour >= start or hour < end
    return start <= hour < end


def next_tick(now: datetime) -> datetime:
    """Найближча планова перевірка — початок наступної години."""
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def slot_matches(user: User, moment: datetime) -> bool:
    """Чи припадає година `moment` на «слот» користувача (відлік від кінця тихого режиму)."""
    return (moment.hour - user.quiet_end) % max(1, user.interval_hours) == 0


def limit_reached(user: User, today: str) -> bool:
    return user.daily_limit > 0 and user.sent_date == today and user.sent_today >= user.daily_limit


def enabled_subjects(user: User) -> list[str]:
    topics = user.topic_set
    return [s for s in SUBJECT_ORDER if any(t in topics for t in SUBJECT_SETTINGS[s])]


def rotation(user: User) -> list[str]:
    """Предмети в порядку спроб: спершу той, що йде ПІСЛЯ останнього надісланого (строге чергування)."""
    subjects = enabled_subjects(user)
    if user.last_subject in subjects:
        i = subjects.index(user.last_subject)
        return subjects[i + 1:] + subjects[: i + 1]
    return subjects


def wod_hour(user: User) -> int:
    return min(user.quiet_end + 1, 23)


def wod_due(user: User, now: datetime, today: str) -> bool:
    return (
        user.word_of_day
        and user.last_wod_date != today
        and not is_quiet_hour(now.hour, user.quiet_start, user.quiet_end)
        and now.hour >= wod_hour(user)
    )


SendFn = Callable[[int], Awaitable[Any]]


class Broadcaster:
    def __init__(self, bot: Bot, db: Database, tz: TzInfo) -> None:
        self.bot = bot
        self.db = db
        self.tz = tz
        self._lock = asyncio.Lock()  # щоб планові перевірки не перетиналися

    # ------------------------------------------------------------------ планова розсилка
    async def tick(self, now: datetime | None = None) -> None:
        await self._run("tick", now)

    async def catch_up(self, now: datetime | None = None) -> None:
        """Після простою (відключення світла) надсилає тим, хто пропустив свій слот, по ОДНІЙ картці."""
        await self._run("catch_up", now)

    async def _run(self, mode: str, now: datetime | None) -> None:
        async with self._lock:
            now = now or datetime.now(self.tz)
            try:
                users = await self.db.get_active_users()
            except Exception:
                logger.exception("Не вдалося отримати користувачів")
                return
            sent = 0
            for user in users:
                try:
                    if await self._process_user(user, now, mode):
                        sent += 1
                except Exception:  # помилка одного користувача не повинна зупиняти решту
                    logger.exception("Помилка обробки користувача %s", user.user_id)
            logger.info("Розсилка (%s) %s: активних %d, надіслано карток %d",
                        mode, now.strftime("%Y-%m-%d %H:%M"), len(users), sent)

    def _card_due(self, user: User, now: datetime, mode: str) -> bool:
        if mode == "tick":
            return slot_matches(user, now)
        # catch_up: був слот, який пропустили (з останньої картки минув інтервал + запас)
        if not user.last_sent_at:
            return False
        try:
            last = datetime.fromisoformat(user.last_sent_at)
        except ValueError:
            return False
        if now - last < timedelta(hours=user.interval_hours) + CATCH_UP_SLACK:
            return False
        upcoming = next_tick(now)
        # якщо вже за кілька хвилин планова перевірка саме для цього користувача — нехай вона й надішле
        return not (slot_matches(user, upcoming) and upcoming - now < MIN_GAP_TO_NEXT_TICK)

    async def _process_user(self, user: User, now: datetime, mode: str) -> bool:
        if is_quiet_hour(now.hour, user.quiet_start, user.quiet_end):
            return False
        today = now.strftime("%Y-%m-%d")
        if wod_due(user, now, today):
            await self._send_word_of_day(user, today)
        if not self._card_due(user, now, mode) or limit_reached(user, today):
            return False
        return await self._send_scheduled_card(user, now, today)

    async def _send_scheduled_card(self, user: User, now: datetime, today: str) -> bool:
        utc_now = now.astimezone(timezone.utc)
        for subject in rotation(user):
            card = await pick_card(self.db, user.user_id, subject, user.topic_set, utc_now)
            if card is None:
                continue  # у цьому предметі немає матеріалів — пробуємо наступний
            ok = await self._deliver(user.user_id, lambda uid=user.user_id, c=card, s=subject: send_card(self.bot, uid, c, more=s))
            if ok:
                await self.db.mark_sent(user.user_id, subject, now.isoformat(), today)
                await asyncio.sleep(SEND_DELAY)
            return ok
        logger.warning("Для користувача %s немає матеріалів в увімкнених темах", user.user_id)
        return False

    async def _send_word_of_day(self, user: User, today: str) -> None:
        card = await build_card(self.db, SUBJECT_LANG, None)  # «слово дня» — з усієї мови, незалежно від тем
        await self.db.mark_wod(user.user_id, today)  # навіть якщо матеріалів немає — не повторюємо цілий день
        if card is None:
            return
        text = render_word_of_day(card)
        markup = card_keyboard(card, stage=2, more=SUBJECT_LANG)
        await self._deliver(user.user_id, lambda uid=user.user_id: self.bot.send_message(uid, text, reply_markup=markup))
        await asyncio.sleep(SEND_DELAY)

    # ------------------------------------------------------------------ надсилання з обробкою помилок
    async def _deliver(self, user_id: int, send: Callable[[], Awaitable[Any]]) -> bool:
        for attempt in (1, 2):
            try:
                await send()
                return True
            except TelegramRetryAfter as exc:
                logger.warning("Flood control: чекаю %s с (користувач %s)", exc.retry_after, user_id)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramForbiddenError:
                logger.info("Користувач %s заблокував бота — деактивую", user_id)
                await self.db.set_user_active(user_id, False)
                return False
            except TelegramBadRequest as exc:
                if "chat not found" in str(exc).lower():
                    logger.info("Чат %s не знайдено — деактивую", user_id)
                    await self.db.set_user_active(user_id, False)
                else:
                    logger.error("BadRequest для %s: %s", user_id, exc)
                return False
            except TelegramAPIError as exc:
                logger.error("Telegram API помилка для %s (спроба %d): %s", user_id, attempt, exc)
                await asyncio.sleep(1)
            except Exception:
                logger.exception("Непередбачена помилка при відправці користувачу %s", user_id)
                return False
        return False

    # ------------------------------------------------------------------ розсилки адміна (незалежно від часу)
    async def send_to_all(self, send_fn: SendFn) -> tuple[int, int]:
        """Надсилає всім активним користувачам: ігнорує тихий режим, інтервали й ліміти. -> (успішно, помилок)."""
        async with self._lock:
            users = await self.db.get_active_users()
            sent = failed = 0
            for user in users:
                ok = await self._deliver(user.user_id, lambda uid=user.user_id: send_fn(uid))
                sent += ok
                failed += not ok
                await asyncio.sleep(SEND_DELAY)
            logger.info("Розсилка адміна: успішно %d, помилок %d", sent, failed)
            return sent, failed

    async def send_card_to_all(self, card: Card, more: str | None = None) -> tuple[int, int]:
        return await self.send_to_all(lambda uid: send_card(self.bot, uid, card, more=more))

    async def send_quiz_to_all(self, spec: QuizSpec) -> tuple[int, int]:
        return await self.send_to_all(lambda uid: send_quiz(self.bot, self.db, uid, spec))

    async def send_text_to_all(self, html: str, photo: str | None = None) -> tuple[int, int]:
        if photo:
            return await self.send_to_all(lambda uid: self.bot.send_photo(uid, photo, caption=html))
        return await self.send_to_all(lambda uid: self.bot.send_message(uid, html))


def setup_scheduler(bot: Bot, db: Database, tz: TzInfo) -> tuple[AsyncIOScheduler, Broadcaster]:
    """Створює планувальник. Запускати (scheduler.start()) потрібно вже всередині event loop."""
    scheduler = AsyncIOScheduler(timezone=tz)
    broadcaster = Broadcaster(bot, db, tz)
    # Щогодини, о :00 за Києвом: хто саме отримує картку — вирішують налаштування кожного користувача
    scheduler.add_job(
        broadcaster.tick,
        CronTrigger(hour="*", minute=0, timezone=tz),
        id="broadcast",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
    )
    logger.info("Планувальник налаштовано: щогодинна перевірка розсилки (%s)", tz)
    return scheduler, broadcaster


def schedule_catch_up(scheduler: AsyncIOScheduler, broadcaster: Broadcaster, tz: TzInfo) -> None:
    """Одноразова перевірка «чи не пропустили картку» через кілька секунд після старту бота."""
    run_at = datetime.now(tz) + timedelta(seconds=CATCH_UP_DELAY)
    scheduler.add_job(
        broadcaster.catch_up,
        DateTrigger(run_date=run_at, timezone=tz),
        id="catch_up",
        replace_existing=True,
        misfire_grace_time=300,
    )
