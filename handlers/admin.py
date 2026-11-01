"""Адмін-панель (лише для ADMIN_ID): додавання, перегляд, редагування та видалення матеріалів."""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from database.db import Database
from database.models import (
    KIND_ACCENT,
    KIND_DATE,
    KIND_FOREIGN,
    KIND_MNEMONIC,
    KIND_MONUMENT,
    KIND_PHRASE,
    KIND_TABLES,
)
from services.cards import (
    accent_variants,
    escape,
    highlight_stress,
    normalize_accent_word,
    normalize_mnemonic,
    to_marked,
)

logger = logging.getLogger(__name__)
router = Router(name="admin")


async def is_admin(event: TelegramObject, admin_id: int) -> bool:
    """Фільтр: пропускає лише ADMIN_ID (значення приходить із workflow_data диспетчера)."""
    user = getattr(event, "from_user", None)
    return user is not None and user.id == admin_id


router.message.filter(is_admin)
router.callback_query.filter(is_admin)

KINDS = "[damnfr]"  # літери типів записів у callback_data (див. database.models)


# ====================================================================== перевірка полів
# Кожен валідатор повертає (значення, "") або (None, текст помилки).
Result = tuple["str | None", str]


def v_year(raw: str) -> Result:
    year = raw.strip()
    if not re.fullmatch(r"\d{3,4}(\s*[–—-]\s*\d{3,4})?", year):
        return None, "⚠️ Некоректний рік. Приклад: <code>988</code> або <code>1932–1933</code>."
    return re.sub(r"\s*[—-]\s*", "–", year), ""


def v_event(raw: str) -> Result:
    event = raw.strip()
    if not 5 <= len(event) <= 600:
        return None, "⚠️ Опис події має містити від 5 до 600 символів."
    return event, ""


def v_word(raw: str) -> Result:
    word = normalize_accent_word(raw)
    if word is None:
        return None, (
            "⚠️ Потрібне одне слово з одним наголосом: наголошена голосна — велика, решта літер — малі "
            "(приклад: <code>каталОг</code>). Якщо допустимі два варіанти — дві великі голосні "
            "(<code>мАбУть</code>)."
        )
    return word, ""


def v_note(raw: str) -> Result:
    note = raw.strip()
    if note == "-":
        note = ""
    if len(note) > 500:
        return None, "⚠️ Підказка задовга (максимум 500 символів)."
    return note, ""


def v_title(raw: str) -> Result:
    title = raw.strip()
    if not 2 <= len(title) <= 150:
        return None, "⚠️ Назва має містити від 2 до 150 символів."
    return title, ""


def v_description(raw: str) -> Result:
    description = raw.strip()
    if not 5 <= len(description) <= 700:
        return None, "⚠️ Опис має містити від 5 до 700 символів."
    return description, ""


def v_foreign_word(raw: str) -> Result:
    word = " ".join(raw.split())
    if not 2 <= len(word) <= 60:
        return None, "⚠️ Іншомовне слово — від 2 до 60 символів."
    return word, ""


def v_synonym(raw: str) -> Result:
    synonym = " ".join(raw.split())
    if not 2 <= len(synonym) <= 150:
        return None, "⚠️ Український відповідник — від 2 до 150 символів."
    return synonym, ""


def v_phrase(raw: str) -> Result:
    phrase = " ".join(raw.split()).strip("«»\"")
    if not 2 <= len(phrase) <= 120:
        return None, "⚠️ Фразеологізм — від 2 до 120 символів."
    return phrase, ""


def v_meaning(raw: str) -> Result:
    meaning = " ".join(raw.split())
    if not 3 <= len(meaning) <= 500:
        return None, "⚠️ Значення — від 3 до 500 символів."
    return meaning, ""


def v_category(raw: str) -> Result:
    category = " ".join(raw.split())
    if not 2 <= len(category) <= 40:
        return None, "⚠️ Назва категорії — від 2 до 40 символів."
    return category, ""


def v_mnemonic(raw: str) -> Result:
    text = normalize_mnemonic(raw)
    if text is None:
        return None, (
            "⚠️ У фразі має бути хоча б один наголос, довжина — від 5 до 300 символів. "
            "Наголос: велика голосна всередині слова (<code>піцЕрія</code>) або знак наголосу."
        )
    return text, ""


@dataclass(frozen=True, slots=True)
class Field:
    code: str                              # літера в callback_data
    column: str                            # колонка в БД
    label: str                             # назва поля для кнопки
    prompt: str                            # що написати адміну при редагуванні
    validator: Callable[[str], Result] | None  # None => поле-фото


FIELDS: dict[str, tuple[Field, ...]] = {
    KIND_DATE: (
        Field("y", "year", "Рік", "Введіть новий <b>рік</b> (наприклад, <code>988</code> або <code>1932–1933</code>):", v_year),
        Field("e", "event", "Подію", "Введіть нову <b>подію</b> (без року в тексті — рік є відповіддю):", v_event),
    ),
    KIND_ACCENT: (
        Field("w", "word", "Слово", "Введіть слово з наголосом (наголошена голосна — <b>велика</b>, напр. <code>алфавІт</code>):", v_word),
        Field("n", "note", "Підказку", "Введіть нову <b>підказку</b> (або «-», щоб прибрати її):", v_note),
    ),
    KIND_MONUMENT: (
        Field("t", "title", "Назву", "Введіть нову <b>назву</b> пам'ятки:", v_title),
        Field("d", "description", "Опис", "Введіть новий <b>опис</b> (до 700 символів):", v_description),
        Field("p", "photo_file_id", "Фото", "Надішліть нове <b>фото</b> (саме як фото, не файл):", None),
    ),
    KIND_PHRASE: (
        Field("p", "phrase", "Фразеологізм", "Введіть новий <b>фразеологізм</b> (без лапок):", v_phrase),
        Field("m", "meaning", "Значення", "Введіть нове <b>значення</b>:", v_meaning),
    ),
    KIND_FOREIGN: (
        Field("w", "word", "Іншомовне слово", "Введіть нове <b>іншомовне слово</b>:", v_foreign_word),
        Field("s", "synonym", "Відповідник", "Введіть новий <b>український відповідник</b> (кілька — через кому):", v_synonym),
    ),
    KIND_MNEMONIC: (
        Field("c", "category", "Категорію", "Введіть нову <b>категорію</b>:", v_category),
        Field("x", "text", "Фразу", "Введіть нову <b>фразу</b>. Наголос: велика голосна всередині слова (<code>піцЕрія</code>) або знак наголосу (піце́рія):", v_mnemonic),
    ),
}


def _field(kind: str, code: str) -> Field | None:
    return next((f for f in FIELDS.get(kind, ()) if f.code == code), None)


# ====================================================================== стани
class AddDate(StatesGroup):
    year = State()
    event = State()


class AddAccent(StatesGroup):
    word = State()
    note = State()


class AddMonument(StatesGroup):
    photo = State()
    title = State()
    description = State()


class AddMnemonic(StatesGroup):
    category = State()
    text = State()


class AddPhrase(StatesGroup):
    phrase = State()
    meaning = State()


class AddForeign(StatesGroup):
    word = State()
    synonym = State()


class EditValue(StatesGroup):
    value = State()


# ====================================================================== клавіатури та тексти
KIND_LABELS = {
    KIND_DATE: ("📅", "Дати"),
    KIND_MONUMENT: ("🏛", "Пам'ятки"),
    KIND_ACCENT: ("🔤", "Наголоси"),
    KIND_MNEMONIC: ("🧠", "Фрази для наголосів"),
    KIND_FOREIGN: ("🌐", "Відповідники іншомовних слів"),
    KIND_PHRASE: ("💬", "Фразеологізми"),
}

NEED_TEXT = "⚠️ Очікую текстове повідомлення (не команду). Або натисніть /cancel, щоб скасувати."
PAGE_SIZE = 8
PAGE_SIZES = {KIND_FOREIGN: 15, KIND_PHRASE: 15, KIND_ACCENT: 15}  # у цих розділах записів багато


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


MENU_KB = InlineKeyboardMarkup(inline_keyboard=[
    [_btn("📅 Додати дату", "adm:date")],
    [_btn("🔤 Додати наголос", "adm:accent")],
    [_btn("🧠 Додати фразу для наголосів", "adm:mnemonic")],
    [_btn("🌐 Додати відповідник іншомовного слова", "adm:foreign")],
    [_btn("💬 Додати фразеологізм", "adm:phrase")],
    [_btn("🏛 Додати пам'ятку з фото", "adm:monument")],
    [_btn("📋 Переглянути / редагувати / видалити", "adm:list")],
    [_btn("📊 Статистика", "adm:stats")],
])
CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Скасувати", "adm:cancel")]])
MENU_TEXT = "🛠 <b>Адмін-панель</b>\nОберіть дію:"


def _text(message: Message) -> str | None:
    """Текст повідомлення; None, якщо це не текст або це команда."""
    text = (message.text or "").strip()
    return text if text and not text.startswith("/") else None


async def _show_menu(message: Message) -> None:
    await message.answer(MENU_TEXT, reply_markup=MENU_KB)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _preview(kind: str, item) -> str:
    """Короткий рядок для списку (без HTML-екранування)."""
    if kind == KIND_DATE:
        return f"{item.year} — {item.event}"
    if kind == KIND_ACCENT:
        return " і ".join(accent_variants(item.word))
    if kind == KIND_MNEMONIC:
        return item.text
    if kind == KIND_PHRASE:
        return f"{item.phrase} — {item.meaning}"
    if kind == KIND_FOREIGN:
        return f"{item.word} — {item.synonym}"
    return item.title


def _item_text(kind: str, item) -> str:
    """Повний опис запису (HTML)."""
    icon, label = KIND_LABELS[kind]
    head = f"{icon} <b>{label}</b> · №{item.id}\n\n"
    if kind == KIND_DATE:
        return head + f"Рік: <b>{escape(item.year)}</b>\nПодія: {escape(item.event)}"
    if kind == KIND_ACCENT:
        note = f"\nПідказка: {escape(item.note)}" if item.note else "\nПідказка: —"
        shown = " і ".join(accent_variants(item.word))
        return head + f"Слово: <b>{escape(shown)}</b> ({escape(item.word)}){note}"
    if kind == KIND_PHRASE:
        return head + f"Фразеологізм: <b>{escape(item.phrase)}</b>\nЗначення: {escape(item.meaning)}"
    if kind == KIND_FOREIGN:
        return head + f"Іншомовне слово: <b>{escape(item.word)}</b>\nВідповідник: {escape(item.synonym)}"
    if kind == KIND_MNEMONIC:
        return head + f"Категорія: <b>{escape(item.category or '—')}</b>\n\n{highlight_stress(item.text)}"
    return head + f"<b>{escape(item.title)}</b>\n\n{escape(item.description)}"


def _item_markup(kind: str, item_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✏️ Редагувати", f"adm:ef:{kind}:{item_id}:{page}")],
        [_btn("🗑 Видалити", f"adm:dl:{kind}:{item_id}:{page}")],
        [_btn("↩️ До списку", f"adm:ls:{kind}:{page}")],
    ])


def _photo_of(kind: str, item) -> str | None:
    return item.photo_file_id if kind == KIND_MONUMENT else None


async def _show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup, photo: str | None = None) -> None:
    """Показує екран, по можливості редагуючи поточне повідомлення (текстове чи з фото)."""
    msg = callback.message
    if not isinstance(msg, Message):
        return
    try:
        if photo is None and not msg.photo:
            await msg.edit_text(text, reply_markup=markup)
            return
        if photo is not None and msg.photo:
            await msg.edit_caption(caption=text, reply_markup=markup)
            return
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning("Не вдалося відредагувати повідомлення, надсилаю нове: %s", exc)

    # Тип повідомлення змінюється (текст <-> фото): старе видаляємо, нове надсилаємо
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    if photo is not None:
        try:
            await msg.answer_photo(photo, caption=text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            logger.error("Фото недоступне (%s) — показую без нього", exc)
    await msg.answer(text, reply_markup=markup)


async def _answer_item(message: Message, db: Database, kind: str, item_id: int, page: int) -> None:
    """Надсилає картку запису новим повідомленням (після редагування через FSM)."""
    item = await db.get_item(kind, item_id)
    if item is None:
        await _show_menu(message)
        return
    text, markup, photo = _item_text(kind, item), _item_markup(kind, item_id, page), _photo_of(kind, item)
    if photo:
        try:
            await message.answer_photo(photo, caption=text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            logger.error("Фото недоступне (%s) — показую без нього", exc)
    await message.answer(text, reply_markup=markup)


# ====================================================================== меню, скасування, статистика
@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_menu(message)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Немає активної дії.")
        return
    await state.clear()
    await message.answer("❌ Дію скасовано.")
    await _show_menu(message)


@router.callback_query(F.data == "adm:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Скасовано")
    if isinstance(callback.message, Message):
        await callback.message.answer("❌ Дію скасовано.")
        await _show_menu(callback.message)


@router.callback_query(F.data == "adm:menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _show(callback, MENU_TEXT, MENU_KB)


@router.callback_query(F.data == "adm:noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "adm:stats")
async def cb_stats(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    counts = await db.content_counts()
    total = await db.count_users()
    active = await db.count_users(only_active=True)
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Користувачі: <b>{active}</b> підписаних (усього зареєстровано: {total})\n\n"
        "📚 <b>Матеріали в базі</b>\n"
        f"📅 Дати: <b>{counts.get('history_dates', 0)}</b>\n"
        f"🏛 Пам'ятки: <b>{counts.get('monuments', 0)}</b>\n"
        f"🔤 Наголоси: <b>{counts.get('accents', 0)}</b>\n"
        f"🧠 Фрази для наголосів: <b>{counts.get('mnemonics', 0)}</b>\n"
        f"🌐 Відповідники іншомовних слів: <b>{counts.get('foreign_words', 0)}</b>\n"
        f"💬 Фразеологізми: <b>{counts.get('phrases', 0)}</b>"
    )
    if isinstance(callback.message, Message):
        await callback.message.answer(text)


# ====================================================================== додавання дати: рік -> подія
@router.callback_query(F.data == "adm:date")
async def cb_add_date(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddDate.year)
    if isinstance(callback.message, Message):
        await callback.message.answer(FIELDS[KIND_DATE][0].prompt.replace("новий ", ""), reply_markup=CANCEL_KB)


@router.message(AddDate.year)
async def add_date_year(message: Message, state: FSMContext) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    year, error = v_year(raw)
    if year is None:
        await message.answer(error)
        return
    await state.update_data(year=year)
    await state.set_state(AddDate.event)
    await message.answer(
        f"Рік: <b>{escape(year)}</b>\nТепер введіть <b>подію</b> (без року в тексті, адже рік буде відповіддю):",
        reply_markup=CANCEL_KB,
    )


@router.message(AddDate.event)
async def add_date_event(message: Message, state: FSMContext, db: Database) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    event, error = v_event(raw)
    if event is None:
        await message.answer(error)
        return
    data = await state.get_data()
    try:
        item_id = await db.add_history_date(data["year"], event)
    except Exception:
        logger.exception("Не вдалося зберегти дату")
        await message.answer("😔 Помилка збереження в базу. Подробиці — у логах.")
        return
    await state.clear()
    logger.info("Адмін додав дату #%s (%s)", item_id, data["year"])
    await message.answer(f"✅ Дату додано (№{item_id}): <b>{escape(data['year'])}</b> — {escape(event)}")
    await _show_menu(message)


# ====================================================================== додавання наголосу: слово -> підказка
@router.callback_query(F.data == "adm:accent")
async def cb_add_accent(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddAccent.word)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🔤 Введіть слово з наголосом: наголошену голосну напишіть <b>великою</b> літерою "
            "(наприклад, <code>алфавІт</code>) або зі знаком наголосу (алфаві́т):",
            reply_markup=CANCEL_KB,
        )


@router.message(AddAccent.word)
async def add_accent_word(message: Message, state: FSMContext) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    word, error = v_word(raw)
    if word is None:
        await message.answer(error)
        return
    await state.update_data(word=word)
    await state.set_state(AddAccent.note)
    await message.answer(
        f"Слово: <b>{escape(to_marked(word))}</b>\nТепер введіть <b>підказку або правило</b> "
        "(або надішліть «-», щоб пропустити):",
        reply_markup=CANCEL_KB,
    )


@router.message(AddAccent.note)
async def add_accent_note(message: Message, state: FSMContext, db: Database) -> None:
    # Тут допустимий і рядок «-», тому команди відсіюємо окремо
    raw = (message.text or "").strip()
    if not raw or raw.startswith("/"):
        await message.answer(NEED_TEXT)
        return
    note, error = v_note(raw)
    if note is None:
        await message.answer(error)
        return
    data = await state.get_data()
    try:
        item_id = await db.add_accent(data["word"], note)
    except Exception:
        logger.exception("Не вдалося зберегти наголос")
        await message.answer("😔 Помилка збереження в базу. Подробиці — у логах.")
        return
    await state.clear()
    logger.info("Адмін додав наголос #%s (%s)", item_id, data["word"])
    await message.answer(f"✅ Наголос додано (№{item_id}): <b>{escape(to_marked(data['word']))}</b>")
    await _show_menu(message)


# ====================================================================== додавання фрази: категорія -> текст
@router.callback_query(F.data == "adm:mnemonic")
async def cb_add_mnemonic(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await callback.answer()
    cats = await db.list_categories()
    await state.set_state(AddMnemonic.category)
    await state.update_data(cats=cats)
    rows = [[_btn(cat, f"adm:mc:{i}")] for i, cat in enumerate(cats)]
    rows.append([_btn("✖️ Скасувати", "adm:cancel")])
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🧠 Оберіть <b>категорію</b> кнопкою або надішліть назву нової категорії:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


async def _ask_mnemonic_text(message: Message, state: FSMContext, category: str) -> None:
    await state.update_data(category=category)
    await state.set_state(AddMnemonic.text)
    await message.answer(
        f"Категорія: <b>{escape(category)}</b>\n\nТепер введіть <b>фразу</b>. Наголос позначайте великою голосною "
        "всередині слова (<code>піцЕрія — феєрія смаку</code>) або знаком наголосу (піце́рія). "
        "Якщо наголос на першій літері слова, використайте знак наголосу.",
        reply_markup=CANCEL_KB,
    )


@router.callback_query(AddMnemonic.category, F.data.regexp(r"^adm:mc:(\d+)$"))
async def cb_mnemonic_category(callback: CallbackQuery, state: FSMContext) -> None:
    index = int((callback.data or "").split(":")[2])
    cats = (await state.get_data()).get("cats", [])
    if index >= len(cats):
        await callback.answer("Категорію не знайдено, введіть її текстом.", show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await _ask_mnemonic_text(callback.message, state, cats[index])


@router.message(AddMnemonic.category)
async def add_mnemonic_category(message: Message, state: FSMContext) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    category, error = v_category(raw)
    if category is None:
        await message.answer(error)
        return
    await _ask_mnemonic_text(message, state, category)


@router.message(AddMnemonic.text)
async def add_mnemonic_text(message: Message, state: FSMContext, db: Database) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    text, error = v_mnemonic(raw)
    if text is None:
        await message.answer(error)
        return
    data = await state.get_data()
    try:
        item_id = await db.add_mnemonic(data["category"], text)
    except Exception:
        logger.exception("Не вдалося зберегти фразу")
        await message.answer("😔 Помилка збереження в базу. Подробиці — у логах.")
        return
    await state.clear()
    logger.info("Адмін додав фразу #%s (%s)", item_id, data["category"])
    await message.answer(f"✅ Фразу додано (№{item_id}):\n{highlight_stress(text)}")
    await _show_menu(message)


# ====================================================================== додавання фразеологізму: фраза -> значення
@router.callback_query(F.data == "adm:phrase")
async def cb_add_phrase(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddPhrase.phrase)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "💬 Введіть <b>фразеологізм</b> (наприклад, <code>Авгієві стайні</code>):", reply_markup=CANCEL_KB
        )


@router.message(AddPhrase.phrase)
async def add_phrase_text(message: Message, state: FSMContext) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    phrase, error = v_phrase(raw)
    if phrase is None:
        await message.answer(error)
        return
    await state.update_data(phrase=phrase)
    await state.set_state(AddPhrase.meaning)
    await message.answer(
        f"Фразеологізм: <b>{escape(phrase)}</b>\nТепер введіть його <b>значення</b>:", reply_markup=CANCEL_KB
    )


@router.message(AddPhrase.meaning)
async def add_phrase_meaning(message: Message, state: FSMContext, db: Database) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    meaning, error = v_meaning(raw)
    if meaning is None:
        await message.answer(error)
        return
    data = await state.get_data()
    try:
        item_id = await db.add_phrase(data["phrase"], meaning)
    except Exception:
        logger.exception("Не вдалося зберегти фразеологізм")
        await message.answer("😔 Помилка збереження в базу. Подробиці — у логах.")
        return
    await state.clear()
    logger.info("Адмін додав фразеологізм #%s (%s)", item_id, data["phrase"])
    await message.answer(f"✅ Додано (№{item_id}): <b>{escape(data['phrase'])}</b> — {escape(meaning)}")
    await _show_menu(message)


# ====================================================================== додавання відповідника: слово -> відповідник
@router.callback_query(F.data == "adm:foreign")
async def cb_add_foreign(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddForeign.word)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🌐 Введіть <b>іншомовне слово</b> (наприклад, <code>абзац</code>):", reply_markup=CANCEL_KB
        )


@router.message(AddForeign.word)
async def add_foreign_word(message: Message, state: FSMContext) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    word, error = v_foreign_word(raw)
    if word is None:
        await message.answer(error)
        return
    await state.update_data(word=word)
    await state.set_state(AddForeign.synonym)
    await message.answer(
        f"Слово: <b>{escape(word)}</b>\nТепер введіть <b>український відповідник</b> (кілька — через кому):",
        reply_markup=CANCEL_KB,
    )


@router.message(AddForeign.synonym)
async def add_foreign_synonym(message: Message, state: FSMContext, db: Database) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    synonym, error = v_synonym(raw)
    if synonym is None:
        await message.answer(error)
        return
    data = await state.get_data()
    try:
        item_id = await db.add_foreign_word(data["word"], synonym)
    except Exception:
        logger.exception("Не вдалося зберегти відповідник")
        await message.answer("😔 Помилка збереження в базу. Подробиці — у логах.")
        return
    await state.clear()
    logger.info("Адмін додав відповідник #%s (%s)", item_id, data["word"])
    await message.answer(f"✅ Додано (№{item_id}): <b>{escape(data['word'])}</b> — {escape(synonym)}")
    await _show_menu(message)


# ====================================================================== додавання пам'ятки: фото -> назва -> опис
@router.callback_query(F.data == "adm:monument")
async def cb_add_monument(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddMonument.photo)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🏛 Надішліть <b>фото</b> пам'ятки (саме як фото, а не файл):", reply_markup=CANCEL_KB
        )


@router.message(AddMonument.photo, F.photo)
async def add_monument_photo(message: Message, state: FSMContext) -> None:
    # Telegram надсилає кілька розмірів; останній — найбільший
    file_id = message.photo[-1].file_id  # type: ignore[index]
    await state.update_data(photo_file_id=file_id)
    await state.set_state(AddMonument.title)
    logger.info("Адмін надіслав фото пам'ятки, file_id=%s", file_id)
    await message.answer("Фото отримано ✅\nТепер введіть <b>назву</b> пам'ятки:", reply_markup=CANCEL_KB)


@router.message(AddMonument.photo)
async def add_monument_photo_invalid(message: Message) -> None:
    await message.answer("⚠️ Очікую саме фото. Надішліть зображення або натисніть /cancel.")


@router.message(AddMonument.title)
async def add_monument_title(message: Message, state: FSMContext) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    title, error = v_title(raw)
    if title is None:
        await message.answer(error)
        return
    await state.update_data(title=title)
    await state.set_state(AddMonument.description)
    await message.answer(
        "Тепер введіть <b>опис</b> (епоха, автор, значення; до 700 символів):", reply_markup=CANCEL_KB
    )


@router.message(AddMonument.description)
async def add_monument_description(message: Message, state: FSMContext, db: Database) -> None:
    raw = _text(message)
    if raw is None:
        await message.answer(NEED_TEXT)
        return
    description, error = v_description(raw)
    if description is None:
        await message.answer(error)
        return
    data = await state.get_data()
    try:
        item_id = await db.add_monument(data["title"], data["photo_file_id"], description)
    except Exception:
        logger.exception("Не вдалося зберегти пам'ятку")
        await message.answer("😔 Помилка збереження в базу. Подробиці — у логах.")
        return
    await state.clear()
    logger.info("Адмін додав пам'ятку #%s (%s)", item_id, data["title"])
    await message.answer(f"✅ Пам'ятку додано (№{item_id}): <b>{escape(data['title'])}</b>")
    await _show_menu(message)


# ====================================================================== перегляд: розділи та сторінки
async def _show_kinds(callback: CallbackQuery, db: Database) -> None:
    counts = await db.content_counts()
    rows = [
        [_btn(f"{icon} {label} ({counts.get(KIND_TABLES[kind][0], 0)})", f"adm:ls:{kind}:0")]
        for kind, (icon, label) in KIND_LABELS.items()
    ]
    rows.append([_btn("↩️ До меню", "adm:menu")])
    await _show(callback, "📋 <b>Матеріали в базі</b>\nОберіть розділ:", InlineKeyboardMarkup(inline_keyboard=rows))


async def _show_list(callback: CallbackQuery, db: Database, kind: str, page: int) -> None:
    size = PAGE_SIZES.get(kind, PAGE_SIZE)
    total = await db.count(KIND_TABLES[kind][0])
    pages = max(1, math.ceil(total / size))
    page = min(max(page, 0), pages - 1)
    items = await db.list_items(kind, size, page * size)
    icon, label = KIND_LABELS[kind]

    rows: list[list[InlineKeyboardButton]] = []
    if items:
        lines = [f"№{i.id} · {escape(_clip(_preview(kind, i), 90))}" for i in items]
        text = (
            f"{icon} <b>{label}</b> — усього {total}, сторінка {page + 1}/{pages}\n\n"
            + "\n".join(lines)
            + "\n\nОберіть номер, щоб відкрити запис, відредагувати або видалити його:"
        )
        buttons = [_btn(f"№{i.id}", f"adm:it:{kind}:{i.id}:{page}") for i in items]
        rows += [buttons[n:n + 5] for n in range(0, len(buttons), 5)]
    else:
        text = f"{icon} <b>{label}</b>\n\nУ цьому розділі записів немає."

    if pages > 1:
        nav = []
        if pages > 5 and page > 0:
            nav.append(_btn("⏮", f"adm:ls:{kind}:0"))
        if page > 0:
            nav.append(_btn("⬅️", f"adm:ls:{kind}:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{pages}", "adm:noop"))
        if page < pages - 1:
            nav.append(_btn("➡️", f"adm:ls:{kind}:{page + 1}"))
        if pages > 5 and page < pages - 1:
            nav.append(_btn("⏭", f"adm:ls:{kind}:{pages - 1}"))
        rows.append(nav)
    rows.append([_btn("↩️ До розділів", "adm:list")])
    await _show(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "adm:list")
async def cb_list_kinds(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await callback.answer()
    await _show_kinds(callback, db)


@router.callback_query(F.data.regexp(rf"^adm:ls:({KINDS}):(\d+)$"))
async def cb_list_page(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    _, _, kind, raw_page = (callback.data or "").split(":")
    await callback.answer()
    await _show_list(callback, db, kind, int(raw_page))


@router.callback_query(F.data.regexp(rf"^adm:it:({KINDS}):(\d+):(\d+)$"))
async def cb_item(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    _, _, kind, raw_id, raw_page = (callback.data or "").split(":")
    page = int(raw_page)
    item = await db.get_item(kind, int(raw_id))
    if item is None:
        await callback.answer("Цього запису вже не існує.", show_alert=True)
        await _show_list(callback, db, kind, page)
        return
    await callback.answer()
    await _show(callback, _item_text(kind, item), _item_markup(kind, item.id, page), _photo_of(kind, item))


# ====================================================================== редагування запису
@router.callback_query(F.data.regexp(rf"^adm:ef:({KINDS}):(\d+):(\d+)$"))
async def cb_edit_choose(callback: CallbackQuery, db: Database) -> None:
    """Вибір поля, яке треба змінити."""
    _, _, kind, raw_id, raw_page = (callback.data or "").split(":")
    page = int(raw_page)
    item = await db.get_item(kind, int(raw_id))
    if item is None:
        await callback.answer("Цього запису вже не існує.", show_alert=True)
        await _show_list(callback, db, kind, page)
        return
    await callback.answer()
    rows = [
        [_btn(f"✏️ Змінити: {field.label.lower()}", f"adm:ed:{kind}:{item.id}:{page}:{field.code}")]
        for field in FIELDS[kind]
    ]
    rows.append([_btn("↩️ Назад", f"adm:it:{kind}:{item.id}:{page}")])
    text = _item_text(kind, item) + "\n\n✏️ <b>Що змінити?</b>"
    await _show(callback, text, InlineKeyboardMarkup(inline_keyboard=rows), _photo_of(kind, item))


@router.callback_query(F.data.regexp(rf"^adm:ed:({KINDS}):(\d+):(\d+):([a-z])$"))
async def cb_edit_field(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    _, _, kind, raw_id, raw_page, code = (callback.data or "").split(":")
    field = _field(kind, code)
    item = await db.get_item(kind, int(raw_id))
    if field is None or item is None:
        await callback.answer("Запис або поле вже не існує.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(EditValue.value)
    await state.update_data(kind=kind, item_id=item.id, page=int(raw_page), code=code)

    text = field.prompt
    if field.validator is not None:
        current = getattr(item, field.column)
        shown = to_marked(current) if kind == KIND_ACCENT and field.column == "word" else current
        text += f"\n\nПоточне значення:\n<code>{escape(shown) if shown else '—'}</code>"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✖️ Скасувати", f"adm:ec:{kind}:{item.id}:{raw_page}")]
    ])
    if isinstance(callback.message, Message):
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.regexp(rf"^adm:ec:({KINDS}):(\d+):(\d+)$"))
async def cb_edit_cancel(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    _, _, kind, raw_id, raw_page = (callback.data or "").split(":")
    item = await db.get_item(kind, int(raw_id))
    await callback.answer("Скасовано")
    if item is None:
        await _show_list(callback, db, kind, int(raw_page))
        return
    await _show(callback, _item_text(kind, item), _item_markup(kind, item.id, int(raw_page)), _photo_of(kind, item))


@router.message(EditValue.value)
async def edit_value(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    kind, item_id, page = data.get("kind"), data.get("item_id"), data.get("page", 0)
    field = _field(kind, data.get("code", "")) if kind else None
    if field is None or item_id is None:
        await state.clear()
        await message.answer("⚠️ Стан редагування втрачено. Почніть заново з /admin.")
        return

    if field.validator is None:  # поле-фото
        if not message.photo:
            await message.answer("⚠️ Очікую саме фото. Надішліть зображення або натисніть /cancel.")
            return
        value = message.photo[-1].file_id
    else:
        # для підказки допустимий рядок «-» (очистити), команди відсіюємо
        raw = (message.text or "").strip()
        if not raw or raw.startswith("/"):
            await message.answer(NEED_TEXT)
            return
        value, error = field.validator(raw)
        if value is None:
            await message.answer(error)
            return

    try:
        updated = await db.update_field(kind, item_id, field.column, value)
    except Exception:
        logger.exception("Не вдалося оновити %s#%s.%s", kind, item_id, field.column)
        await message.answer("😔 Помилка збереження в базу. Подробиці — у логах.")
        return

    await state.clear()
    if not updated:
        await message.answer("⚠️ Запис уже не існує (його могли видалити).")
        await _show_menu(message)
        return
    logger.info("Адмін змінив %s#%s.%s", kind, item_id, field.column)
    await message.answer("✅ Збережено.")
    await _answer_item(message, db, kind, item_id, page)


# ====================================================================== видалення запису
@router.callback_query(F.data.regexp(rf"^adm:dl:({KINDS}):(\d+):(\d+)$"))
async def cb_delete_ask(callback: CallbackQuery, db: Database) -> None:
    """Крок підтвердження перед видаленням."""
    _, _, kind, raw_id, raw_page = (callback.data or "").split(":")
    page = int(raw_page)
    item = await db.get_item(kind, int(raw_id))
    if item is None:
        await callback.answer("Цього запису вже не існує.", show_alert=True)
        await _show_list(callback, db, kind, page)
        return
    await callback.answer()
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        _btn("✅ Так, видалити", f"adm:dy:{kind}:{item.id}:{page}"),
        _btn("✖️ Ні", f"adm:it:{kind}:{item.id}:{page}"),
    ]])
    text = "⚠️ <b>Видалити цей запис?</b> Дію не можна скасувати.\n\n" + _item_text(kind, item)
    await _show(callback, text, markup, _photo_of(kind, item))


@router.callback_query(F.data.regexp(rf"^adm:dy:({KINDS}):(\d+):(\d+)$"))
async def cb_delete_confirm(callback: CallbackQuery, db: Database) -> None:
    _, _, kind, raw_id, raw_page = (callback.data or "").split(":")
    try:
        deleted = await db.delete_item(kind, int(raw_id))
    except Exception:
        logger.exception("Не вдалося видалити запис %s#%s", kind, raw_id)
        await callback.answer("Помилка видалення. Подробиці — у логах.", show_alert=True)
        return
    logger.info("Адмін видалив запис %s#%s (існував: %s)", kind, raw_id, deleted)
    await callback.answer("🗑 Видалено" if deleted else "Запис уже було видалено")
    await _show_list(callback, db, kind, int(raw_page))
