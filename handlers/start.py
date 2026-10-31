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
    "Кожні 2 години (з 08:00 до 22:00 за київським часом) я надсилаю коротку картку, "
    "а предмети чергуються: <b>Історія України → Українська мова → Історія України → …</b>\n\n"
    "<b>Команди:</b>\n"
    "/now — отримати картку просто зараз (обери предмет або випадкову)\n"
    "/quiet <code>22 8</code> — змінити години тихого режиму (початок і кінець)\n"
    "/stop — призупинити розсилку (поновити: /start)\n"
    "/help — ця довідка\n\n"
    "Під кожною карткою є кнопки: <b>«Показати відповідь»</b> для самоперевірки "
    "та <b>«Ще одну картку»</b>. Спочатку спробуй пригадати відповідь сам 😉"
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
    await message.answer(f"{greeting}\n\n{HELP_TEXT}")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)
