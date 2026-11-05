"""Реєстрація користувача, привітання та довідка."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from database.db import Database

logger = logging.getLogger(__name__)
router = Router(name="start")

HELP_TEXT = (
    "📚 <b>Бот для підготовки до НМТ</b>\n\n"
    "Я надсилаю короткі картки з Історії України та Української мови — за розкладом "
    "(за замовчуванням кожні 2 години з 08:00 до 22:00 за київським часом) і щодня «слово дня».\n\n"
    "<b>Команди:</b>\n"
    "/now — картка зараз (обери тему)\n"
    "/quiz — квіз: раунд із 10 питань-опитувань\n"
    "/mistakes — повторити те, що не вдалося\n"
    "/stats — твій прогрес і серія днів\n"
    "/find <code>слово</code> — пошук по всій базі\n"
    "/settings — теми, як часто надсилати, ліміт, слово дня\n"
    "/quiet <code>22 8</code> — години тихого режиму\n"
    "/stop — призупинити розсилку (поновити: /start)\n\n"
    "Під карткою: <b>«Показати відповідь»</b>, потім <b>«Знав» / «Не знав»</b> — бот запам'ятає і "
    "частіше повторюватиме те, що дається важче."
)


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    user = message.from_user
    if user is None:
        return
    try:
        is_new = await db.upsert_user(user.id)
    except Exception:
        logger.exception("Не вдалося зареєструвати користувача %s", user.id)
        await message.answer("😔 Не вдалося зареєструвати вас. Спробуйте, будь ласка, пізніше.")
        return

    logger.info("/start від %s (%s)", user.id, "новий" if is_new else "повторно")
    greeting = f"👋 Вітаю, {user.first_name}!" if user.first_name else "👋 Вітаю!"
    await message.answer(f"{greeting}\n\n{HELP_TEXT}\n\nЗроблено для найкоханішої людинки ❤️")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)
