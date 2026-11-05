"""Точка входу: Telegram-бот для підготовки до НМТ (aiogram 3 + aiosqlite + APScheduler)."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytz
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, ErrorEvent, User
from dotenv import load_dotenv

from database.db import Database
from handlers import admin, start, user
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from services.backup import send_backup
from services.notify import AdminNotifier, announce_start, heartbeat
from services.scheduler import schedule_catch_up, setup_scheduler

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

logger = logging.getLogger("nmt-bot")


def setup_logging(level_name: str, log_file: Path) -> None:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("Файловий лог недоступний (%s) — пишу лише в консоль", exc)

    logging.getLogger("apscheduler").setLevel(logging.INFO)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def resolve_db_path() -> Path:
    path = Path(os.getenv("DB_PATH", "storage/bot.db"))
    return path if path.is_absolute() else BASE_DIR / path


async def wait_for_telegram(bot: Bot) -> User:
    """Чекає, поки з'явиться інтернет (після відключення світла роутер часто вмикається пізніше за ноутбук).

    Без цього бот падав би при старті й «крутився» у перезапусках Docker.
    """
    attempt = 0
    while True:
        try:
            return await bot.get_me()
        except TelegramUnauthorizedError:
            raise  # невірний токен — чекати немає сенсу
        except Exception as exc:
            attempt += 1
            delay = min(60, 5 * attempt)
            logger.warning(
                "Telegram недоступний (%s: %s). Спроба %d, наступна через %d с",
                type(exc).__name__, exc, attempt, delay,
            )
            await asyncio.sleep(delay)


async def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    db_path = resolve_db_path()
    setup_logging(os.getenv("LOG_LEVEL", "INFO"), db_path.parent / "bot.log")

    # ---- конфігурація
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or "replace_with" in token:
        logger.critical("BOT_TOKEN не задано. Скопіюйте .env.example у .env та заповніть його.")
        sys.exit(1)
    try:
        admin_id = int(os.getenv("ADMIN_ID", ""))
    except ValueError:
        logger.critical("ADMIN_ID має бути числом (Telegram ID адміністратора).")
        sys.exit(1)
    tz_name = os.getenv("TIMEZONE", "Europe/Kyiv")
    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        logger.critical("Невідомий часовий пояс у TIMEZONE: %s", tz_name)
        sys.exit(1)

    # ---- база даних
    db = Database(db_path)
    await db.connect()
    await db.init(DATA_DIR)

    # ---- бот і диспетчер (db та admin_id стають доступними в хендлерах і фільтрах за іменем)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = AdminNotifier(bot, admin_id)
    dp = Dispatcher(storage=MemoryStorage(), db=db, admin_id=admin_id, tz=tz)
    dp.include_router(admin.router)  # першим: щоб FSM-стани адміна мали пріоритет
    dp.include_router(start.router)
    dp.include_router(user.router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        logger.error("Необроблена помилка в апдейті: %s", event.exception, exc_info=event.exception)
        await notifier.error(event.exception, "апдейт")  # адмін дізнається про серйозні збої (не частіше раз на 10 хв)
        return True  # помилку оброблено, бот продовжує працювати

    scheduler, broadcaster = setup_scheduler(bot, db, tz)
    dp.workflow_data["broadcaster"] = broadcaster  # потрібен адмін-панелі для розсилок

    try:
        me = await wait_for_telegram(bot)

        try:
            user_commands = [
                BotCommand(command="start", description="Реєстрація та інструкція"),
                BotCommand(command="now", description="Отримати картку зараз"),
                BotCommand(command="quiz", description="Квіз: 10 питань-опитувань"),
                BotCommand(command="mistakes", description="Повторити помилки"),
                BotCommand(command="stats", description="Моя статистика"),
                BotCommand(command="find", description="Пошук по базі"),
                BotCommand(command="settings", description="Налаштування"),
                BotCommand(command="quiet", description="Налаштувати тихий режим"),
                BotCommand(command="stop", description="Призупинити розсилку"),
                BotCommand(command="help", description="Довідка"),
            ]
            await bot.set_my_commands(user_commands)
            await bot.set_my_commands(
                user_commands + [BotCommand(command="admin", description="Адмін-панель")],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            logger.exception("Не вдалося встановити меню команд (не критично)")

        # «пульс» кожні 10 хвилин: за ним після відключення світла видно тривалість паузи
        scheduler.add_job(heartbeat, IntervalTrigger(minutes=10), args=[db, tz], id="heartbeat", replace_existing=True)
        if os.getenv("AUTO_BACKUP", "1") != "0":
            # щоденна копія бази адміну в Telegram (вимкнути: AUTO_BACKUP=0)
            scheduler.add_job(
                send_backup, CronTrigger(hour=4, minute=5, timezone=tz),
                args=[bot, db, admin_id, tz, "💾 Щоденна резервна копія"], id="auto_backup", replace_existing=True,
            )
        scheduler.start()
        schedule_catch_up(scheduler, broadcaster, tz)  # якщо була пауза — надішле пропущені картки
        await announce_start(notifier, db, tz)
        logger.info("Бот @%s запущено. Адмін: %s, часовий пояс: %s", me.username, admin_id, tz_name)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Зупинка бота...")
        await heartbeat(db, tz)
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот зупинено")
