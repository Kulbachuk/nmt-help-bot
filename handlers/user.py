"""Користувацькі команди: /now, кнопки самоперевірки, /quiet, /stop."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.db import Database
from database.models import KIND_TABLES
from services.cards import (
    SUBJECT_ANY,
    SUBJECT_HIST,
    SUBJECT_LANG,
    TOPIC_ACCENTS,
    TOPIC_FOREIGN,
    TOPIC_PHRASES,
    ALL_TOPICS,
    build_card,
    build_card_by_id,
    card_keyboard,
    render_back,
    send_card,
)

logger = logging.getLogger(__name__)
router = Router(name="user")

SUBJECT_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🏛 Історія України", callback_data=f"pick:{SUBJECT_HIST}")],
    [InlineKeyboardButton(text="🔤 Українська мова (усе)", callback_data=f"pick:{SUBJECT_LANG}")],
    [InlineKeyboardButton(text="🔊 Наголоси", callback_data=f"pick:{TOPIC_ACCENTS}")],
    [InlineKeyboardButton(text="🌐 Відповідники іншомовних слів", callback_data=f"pick:{TOPIC_FOREIGN}")],
    [InlineKeyboardButton(text="💬 Фразеологізми", callback_data=f"pick:{TOPIC_PHRASES}")],
    [InlineKeyboardButton(text="🎲 Випадково", callback_data=f"pick:{SUBJECT_ANY}")],
])

EMPTY_TEXT = "📭 Для цього предмета в базі поки що немає матеріалів. Спробуйте інший або зазирніть пізніше."


@router.message(Command("now"))
async def cmd_now(message: Message) -> None:
    await message.answer("Який предмет повторимо зараз?", reply_markup=SUBJECT_MENU)


@router.callback_query(F.data.regexp(r"^(now|pick):(hist|lang|any|acc|for|phr)$"))
async def on_get_card(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    prefix, subject = (callback.data or "").split(":", 1)
    await callback.answer()

    try:
        card = await build_card(db, subject)
    except Exception:
        logger.exception("Помилка побудови картки (%s)", subject)
        await bot.send_message(callback.from_user.id, "😔 Сталася помилка. Спробуйте ще раз пізніше.")
        return

    if card is None:
        await bot.send_message(callback.from_user.id, EMPTY_TEXT)
        return

    # Меню вибору предмета після вибору прибираємо, щоб не засмічувати чат
    if prefix == "pick" and isinstance(callback.message, Message):
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass

    try:
        await send_card(bot, callback.from_user.id, card, more=subject)
    except Exception:
        logger.exception("Не вдалося надіслати картку користувачу %s", callback.from_user.id)


@router.callback_query(F.data.startswith("rev:"))
async def on_reveal(callback: CallbackQuery, db: Database) -> None:
    """«Показати відповідь»: редагує повідомлення, додаючи відповідь."""
    try:
        parts = (callback.data or "").split(":")
        kind, raw_id = parts[1], parts[2]
        more = parts[3] if len(parts) > 3 else None  # тема для «Ще одну картку» (старі кнопки її не мають)
        item_id = int(raw_id)
    except ValueError:
        await callback.answer("Некоректні дані кнопки.", show_alert=True)
        return
    if kind not in KIND_TABLES:
        # напр. картки зі щойно прибраного розділу «прислів'я та правила»
        await callback.answer("Цю картку вже видалено з бази.", show_alert=True)
        return

    card = await build_card_by_id(db, kind, item_id)
    if card is None:
        await callback.answer("Цю картку вже видалено з бази.", show_alert=True)
        return

    message = callback.message
    if not isinstance(message, Message):
        await callback.answer("Повідомлення вже недоступне.", show_alert=True)
        return

    text = render_back(card)
    markup = card_keyboard(card, revealed=True, more=more)
    try:
        if message.photo:
            await message.edit_caption(caption=text, reply_markup=markup)
        else:
            await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.error("Не вдалося показати відповідь: %s", exc)
            await callback.answer("Не вдалося показати відповідь.", show_alert=True)
            return
    await callback.answer()


@router.message(Command("quiet"))
async def cmd_quiet(message: Message, command: CommandObject, db: Database) -> None:
    user = message.from_user
    if user is None:
        return
    current = await db.get_user(user.id)
    if current is None:
        await message.answer("Спочатку натисніть /start 🙂")
        return

    args = (command.args or "").strip()
    match = re.fullmatch(r"(\d{1,2})\s+(\d{1,2})", args)
    if not match:
        await message.answer(
            f"🌙 Тихий режим зараз: з <b>{current.quiet_start:02d}:00</b> до <b>{current.quiet_end:02d}:00</b> "
            "(за київським часом).\n\nЩоб змінити, надішліть, наприклад: <code>/quiet 23 7</code>"
        )
        return

    start, end = int(match.group(1)), int(match.group(2))
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        await message.answer("⚠️ Вкажіть дві різні години від 0 до 23, наприклад: <code>/quiet 22 8</code>")
        return

    await db.set_quiet_hours(user.id, start, end)
    logger.info("Користувач %s змінив тихий режим: %02d-%02d", user.id, start, end)
    await message.answer(f"✅ Тихий режим: з {start:02d}:00 до {end:02d}:00 (за київським часом).")


@router.message(Command("stop"))
async def cmd_stop(message: Message, db: Database) -> None:
    if message.from_user is None:
        return
    await db.set_user_active(message.from_user.id, False)
    logger.info("Користувач %s призупинив розсилку", message.from_user.id)
    await message.answer("⏸ Розсилку призупинено. Щоб поновити — натисніть /start.")


@router.message(Command("admin"))
async def admin_denied(message: Message) -> None:
    # Сюди потрапляють лише ті, кого відфільтрував admin-роутер
    await message.answer("⛔ Ця команда доступна лише адміністратору.")
