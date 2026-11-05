"""Користувацькі команди: картки, «знав / не знав», квізи, статистика, налаштування, пошук."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from datetime import tzinfo as TzInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, PollAnswer

from database.db import Database
from database.models import KIND_TABLES, User
from services import srs
from services.cards import (
    ALL_SETTING_TOPICS,
    SUBJECT_ANY,
    SUBJECT_HIST,
    SUBJECT_LANG,
    SUBJECT_OTHER,
    TOPIC_ACCENTS,
    TOPIC_FOREIGN,
    TOPIC_MISTAKES,
    TOPIC_PHRASES,
    build_card_by_id,
    card_keyboard,
    is_valid_topic,
    render_back,
    send_card,
)
from services.quiz import handle_poll_answer, start_session
from services.reports import format_search, format_stats
from services.scheduler import ALLOWED_INTERVALS

logger = logging.getLogger(__name__)
router = Router(name="user")

EMPTY_TEXT = "📭 Для цього розділу в базі поки що немає матеріалів. Спробуйте інший або зазирніть пізніше."
NO_MISTAKES_TEXT = "🎉 Помилок для повторення немає — так тримати!"

TOPIC_RE = r"(hist|lang|any|oth|acc|for|phr|err|c\d+)"


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# ====================================================================== /now: отримати картку
async def subject_menu(db: Database) -> InlineKeyboardMarkup:
    rows = [
        [_btn("🏛 Історія України", f"pick:{SUBJECT_HIST}")],
        [_btn("🔤 Українська мова (усе)", f"pick:{SUBJECT_LANG}")],
        [_btn("🔊 Наголоси", f"pick:{TOPIC_ACCENTS}")],
        [_btn("🌐 Відповідники іншомовних слів", f"pick:{TOPIC_FOREIGN}")],
        [_btn("💬 Фразеологізми", f"pick:{TOPIC_PHRASES}")],
    ]
    for topic in (await db.custom_topics())[:8]:
        rows.append([_btn(f"📚 {topic['name']}", f"pick:c{topic['id']}")])
    rows += [
        [_btn("🔁 Повторити помилки", f"pick:{TOPIC_MISTAKES}")],
        [_btn("🎯 Квіз (опитування)", "qzmenu")],
        [_btn("🎲 Випадково", f"pick:{SUBJECT_ANY}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_card_for(bot: Bot, db: Database, user_id: int, topic: str) -> None:
    """Підбирає картку з урахуванням прогресу користувача й надсилає її."""
    user = await db.get_user(user_id)
    try:
        card = await srs.pick_card(db, user_id, topic, user.topic_set if user else None)
    except Exception:
        logger.exception("Помилка підбору картки (%s)", topic)
        await bot.send_message(user_id, "😔 Сталася помилка. Спробуйте ще раз пізніше.")
        return
    if card is None:
        await bot.send_message(user_id, NO_MISTAKES_TEXT if topic == TOPIC_MISTAKES else EMPTY_TEXT)
        return
    try:
        await send_card(bot, user_id, card, more=topic)
    except Exception:
        logger.exception("Не вдалося надіслати картку користувачу %s", user_id)


@router.message(Command("now"))
async def cmd_now(message: Message, db: Database) -> None:
    await message.answer("Що повторимо зараз?", reply_markup=await subject_menu(db))


@router.message(Command("mistakes"))
async def cmd_mistakes(message: Message, bot: Bot, db: Database) -> None:
    if message.from_user:
        await _send_card_for(bot, db, message.from_user.id, TOPIC_MISTAKES)


@router.callback_query(F.data.regexp(rf"^(now|pick):{TOPIC_RE}$"))
async def on_get_card(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    prefix, topic = (callback.data or "").split(":", 1)
    await callback.answer()
    if not is_valid_topic(topic):
        return
    # Меню вибору після вибору прибираємо, щоб не засмічувати чат
    if prefix == "pick" and isinstance(callback.message, Message):
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
    await _send_card_for(bot, db, callback.from_user.id, topic)


# ====================================================================== відповідь і самоперевірка
@router.callback_query(F.data.startswith("rev:"))
async def on_reveal(callback: CallbackQuery, db: Database) -> None:
    """«Показати відповідь»: редагує повідомлення, додаючи відповідь і кнопки «Знав / Не знав»."""
    try:
        parts = (callback.data or "").split(":")
        kind, raw_id = parts[1], parts[2]
        more = parts[3] if len(parts) > 3 else None  # тема для «Ще одну картку» (старі кнопки її не мають)
        item_id = int(raw_id)
    except (ValueError, IndexError):
        await callback.answer("Некоректні дані кнопки.", show_alert=True)
        return
    if kind not in KIND_TABLES:
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
    markup = card_keyboard(card, stage=1, more=more)
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


@router.callback_query(F.data.regexp(r"^kn:[a-z]:\d+:[01]:\S+$"))
async def on_known(callback: CallbackQuery, db: Database) -> None:
    """«Знав» / «Не знав»: зсуває картку в системі повторення й прибирає кнопки оцінки."""
    _, kind, raw_id, raw_ok, more = (callback.data or "").split(":", 4)
    card = await build_card_by_id(db, kind, int(raw_id)) if kind in KIND_TABLES else None
    if card is None:
        await callback.answer("Цю картку вже видалено з бази.", show_alert=True)
        return
    ok = raw_ok == "1"
    try:
        box = await db.record_review(callback.from_user.id, kind, card.item_id, ok)
    except Exception:
        logger.exception("Не вдалося записати відповідь")
        await callback.answer("Не вдалося зберегти відповідь.", show_alert=True)
        return
    await callback.answer(
        "✅ Зараховано! Повторимо пізніше." if ok else "🔁 Нічого, повторимо найближчим часом.",
    )
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=card_keyboard(card, stage=2, more=more))
        except TelegramBadRequest:
            pass
    logger.debug("Відповідь %s: %s#%s ok=%s box=%s", callback.from_user.id, kind, card.item_id, ok, box)


# ====================================================================== квізи
QUIZ_TOPICS = (
    (SUBJECT_HIST, "🏛 Історія України (дати)"),
    (SUBJECT_LANG, "🔤 Українська мова (усе)"),
    (TOPIC_ACCENTS, "🔊 Наголоси"),
    (TOPIC_FOREIGN, "🌐 Іншомовні слова"),
    (TOPIC_PHRASES, "💬 Фразеологізми"),
)


async def _quiz_menu(db: Database) -> InlineKeyboardMarkup:
    rows = [[_btn(label, f"qz:{topic}")] for topic, label in QUIZ_TOPICS]
    for topic in (await db.custom_topics())[:8]:
        rows.append([_btn(f"📚 {topic['name']}", f"qz:c{topic['id']}")])
    rows.append([_btn("🎲 Будь-яка тема", f"qz:{SUBJECT_ANY}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("quiz"))
async def cmd_quiz(message: Message, db: Database) -> None:
    await message.answer("🎯 <b>Квіз</b> — раунд із 10 питань. Обери тему:", reply_markup=await _quiz_menu(db))


@router.callback_query(F.data == "qzmenu")
async def cb_quiz_menu(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🎯 <b>Квіз</b> — раунд із 10 питань. Обери тему:", reply_markup=await _quiz_menu(db)
        )


@router.callback_query(F.data.regexp(rf"^qz:{TOPIC_RE}$"))
async def cb_quiz_start(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    topic = (callback.data or "").split(":", 1)[1]
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    started = await start_session(bot, db, user_id, topic, user.topic_set if user else None)
    if not started:
        await callback.answer("Для цієї теми поки недостатньо матеріалів для квізу.", show_alert=True)
        return
    await callback.answer("Поїхали! 10 питань 🎯")


@router.poll_answer()
async def on_poll_answer(poll_answer: PollAnswer, db: Database, bot: Bot) -> None:
    try:
        await handle_poll_answer(bot, db, poll_answer)
    except Exception:
        logger.exception("Помилка обробки відповіді на квіз")


# ====================================================================== статистика
@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, tz: TzInfo) -> None:
    if message.from_user is None:
        return
    now = datetime.now(timezone.utc)
    stats = await db.user_stats(message.from_user.id, now.isoformat(timespec="seconds"))
    await message.answer(format_stats(stats, tz, now))


# ====================================================================== пошук
@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject, db: Database) -> None:
    query = (command.args or "").strip()
    if len(query) < 2:
        await message.answer("🔍 Напишіть, що шукати, наприклад: <code>/find фартух</code> або <code>/find 988</code>")
        return
    found = await db.search(query, per_kind=5)
    await message.answer(format_search(found, query))


# ====================================================================== налаштування
TOPIC_LABELS = {
    "dat": "📅 Дати",
    "mon": "🏛 Пам'ятки",
    "acc": "🔤 Наголоси",
    "for": "🌐 Іншомовні слова",
    "phr": "💬 Фразеологізми",
    "oth": "📚 Інші предмети",
}
LIMIT_CHOICES = (0, 5, 10, 20, 30)


def _interval_label(hours: int) -> str:
    return "раз на добу" if hours == 24 else f"{hours} год"


async def settings_view(db: Database, user: User) -> tuple[str, InlineKeyboardMarkup]:
    has_custom = bool(await db.custom_topics())
    enabled = user.topic_set
    shown_topics = [t for t in ALL_SETTING_TOPICS if t != "oth" or has_custom]

    on = ", ".join(TOPIC_LABELS[t].split(" ", 1)[1] for t in shown_topics if t in enabled) or "—"
    limit = "без ліміту" if user.daily_limit == 0 else f"{user.daily_limit} на добу"
    text = (
        "⚙️ <b>Налаштування</b>\n\n"
        f"📚 Теми: {on}\n"
        f"⏱ Картки за розкладом: кожні <b>{_interval_label(user.interval_hours)}</b> "
        f"(відлік від {user.quiet_end:02d}:00)\n"
        f"🔢 Ліміт карток: <b>{limit}</b>\n"
        f"🌙 Тихий режим: {user.quiet_start:02d}:00–{user.quiet_end:02d}:00 (змінити: <code>/quiet 23 7</code>)\n"
        f"☀️ Слово дня: <b>{'увімкнено' if user.word_of_day else 'вимкнено'}</b>\n\n"
        "Натисни кнопку, щоб змінити."
    )
    rows: list[list[InlineKeyboardButton]] = []
    topic_buttons = [
        _btn(("✅ " if t in enabled else "☑️ ") + TOPIC_LABELS[t].split(" ", 1)[1], f"st:t:{t}") for t in shown_topics
    ]
    rows += [topic_buttons[i:i + 2] for i in range(0, len(topic_buttons), 2)]
    rows.append([_btn("⏱ Як часто надсилати:", "st:noop")])
    interval_buttons = [
        _btn(("✅ " if user.interval_hours == h else "") + _interval_label(h), f"st:i:{h}") for h in ALLOWED_INTERVALS
    ]
    rows += [interval_buttons[i:i + 4] for i in range(0, len(interval_buttons), 4)]
    rows.append([_btn("🔢 Ліміт карток на добу:", "st:noop")])
    rows.append([
        _btn(("✅ " if user.daily_limit == n else "") + ("без ліміту" if n == 0 else str(n)), f"st:l:{n}")
        for n in LIMIT_CHOICES
    ])
    rows.append([_btn(f"☀️ Слово дня: {'вимкнути' if user.word_of_day else 'увімкнути'}", "st:w")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("settings"))
async def cmd_settings(message: Message, db: Database) -> None:
    if message.from_user is None:
        return
    user = await db.get_user(message.from_user.id)
    if user is None:
        await message.answer("Спочатку натисніть /start 🙂")
        return
    text, markup = await settings_view(db, user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^st:(noop|w|t:[a-z]{3}|i:\d{1,2}|l:\d{1,2})$"))
async def cb_settings(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    user = await db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Спочатку натисніть /start", show_alert=True)
        return
    action = parts[1]
    if action == "noop":
        await callback.answer()
        return
    if action == "t":
        topic = parts[2]
        if topic not in ALL_SETTING_TOPICS:
            await callback.answer()
            return
        topics = user.topic_set
        if topic in topics:
            if len(topics - {topic}) == 0:
                await callback.answer("Потрібна хоча б одна тема.", show_alert=True)
                return
            topics.discard(topic)
        else:
            topics.add(topic)
        await db.update_user(user.user_id, topics=",".join(t for t in ALL_SETTING_TOPICS if t in topics))
    elif action == "i":
        hours = int(parts[2])
        if hours not in ALLOWED_INTERVALS:
            await callback.answer()
            return
        await db.update_user(user.user_id, interval_hours=hours)
    elif action == "l":
        limit = int(parts[2])
        if limit not in LIMIT_CHOICES:
            await callback.answer()
            return
        await db.update_user(user.user_id, daily_limit=limit)
    elif action == "w":
        await db.update_user(user.user_id, word_of_day=not user.word_of_day)

    await callback.answer("Збережено ✅")
    user = await db.get_user(user.user_id)
    if user is not None and isinstance(callback.message, Message):
        text, markup = await settings_view(db, user)
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("Не вдалося оновити налаштування: %s", exc)


# ====================================================================== тихий режим, пауза
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
