"""Побудова та відправка навчальних карток (спільно для /now та планувальника)."""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from html import escape as _escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.db import Database
from database.models import (
    KIND_ACCENT,
    KIND_CUSTOM,
    KIND_DATE,
    KIND_FOREIGN,
    KIND_MNEMONIC,
    KIND_MONUMENT,
    KIND_PHRASE,
    Accent,
    CustomCard,
    ForeignWord,
    HistoryDate,
    Mnemonic,
    Monument,
    Phrase,
)

logger = logging.getLogger(__name__)


def escape(text: str) -> str:
    """HTML-екранування без перетворення лапок (Telegram їх не потребує)."""
    return _escape(text, quote=False)

SUBJECT_HIST = "hist"   # Історія України
SUBJECT_LANG = "lang"   # Українська мова
SUBJECT_ANY = "any"     # випадковий предмет
SUBJECT_OTHER = "oth"   # інші предмети (довільні завдання, додані адміном)
TOPIC_MISTAKES = "err"  # повторення помилок

# Теми всередині предмета «Українська мова» (для /now)
TOPIC_ACCENTS = "acc"   # наголоси та фрази-підказки
TOPIC_FOREIGN = "for"   # відповідники іншомовних слів
TOPIC_PHRASES = "phr"   # фразеологізми
ALL_TOPICS = (
    SUBJECT_HIST, SUBJECT_LANG, SUBJECT_ANY, SUBJECT_OTHER,
    TOPIC_ACCENTS, TOPIC_FOREIGN, TOPIC_PHRASES, TOPIC_MISTAKES,
)

# «Теми» з налаштувань користувача -> типи карток
SETTING_TOPICS: dict[str, list[str]] = {
    "dat": [KIND_DATE],
    "mon": [KIND_MONUMENT],
    "acc": [KIND_ACCENT, KIND_MNEMONIC],
    "for": [KIND_FOREIGN],
    "phr": [KIND_PHRASE],
    "oth": [KIND_CUSTOM],
}
# Предмет -> теми з налаштувань, що до нього належать
SUBJECT_SETTINGS: dict[str, list[str]] = {
    SUBJECT_HIST: ["dat", "mon"],
    SUBJECT_LANG: ["acc", "for", "phr"],
    SUBJECT_OTHER: ["oth"],
}
SUBJECT_ORDER = (SUBJECT_HIST, SUBJECT_LANG, SUBJECT_OTHER)  # порядок чергування предметів
ALL_SETTING_TOPICS = tuple(SETTING_TOPICS)


def is_valid_topic(topic: str | None) -> bool:
    """Тема — одна зі стандартних або «c<id>» (тема з інших предметів)."""
    return bool(topic) and (topic in ALL_TOPICS or re.fullmatch(r"c\d+", topic) is not None)


def split_topic(topic: str) -> tuple[str, int | None]:
    """'c12' -> ('oth', 12); решта без змін."""
    match = re.fullmatch(r"c(\d+)", topic)
    return (SUBJECT_OTHER, int(match.group(1))) if match else (topic, None)


def topic_kinds(topic: str, enabled: set[str] | None = None) -> list[str]:
    """Типи карток теми. `enabled` — теми з налаштувань користувача (None = усі).

    Для предметів (hist/lang/oth/any) беруться лише увімкнені теми; для окремих тем (acc/for/phr)
    налаштування ігноруються — користувач обрав їх явно.
    """
    base, _ = split_topic(topic)
    allowed = set(ALL_SETTING_TOPICS) if enabled is None else set(enabled)

    def kinds_of(settings: list[str]) -> list[str]:
        return [k for s in settings if s in allowed for k in SETTING_TOPICS[s]]

    if base == SUBJECT_ANY:
        return [k for sub in SUBJECT_ORDER for k in kinds_of(SUBJECT_SETTINGS[sub])]
    if base in SUBJECT_SETTINGS:
        return kinds_of(SUBJECT_SETTINGS[base])
    if base == TOPIC_ACCENTS:
        return list(SETTING_TOPICS["acc"])
    if base == TOPIC_FOREIGN:
        return list(SETTING_TOPICS["for"])
    if base == TOPIC_PHRASES:
        return list(SETTING_TOPICS["phr"])
    if base == TOPIC_MISTAKES:
        return [k for kinds in SETTING_TOPICS.values() for k in kinds]
    return []


def ordered_kinds(kinds: list[str], topic: str) -> list[str]:
    """Випадковий порядок типів; для історії пам'ятки трапляються рідше за дати."""
    kinds = random.sample(kinds, len(kinds))
    if KIND_MONUMENT in kinds and KIND_DATE in kinds:
        first = KIND_MONUMENT if random.random() < MONUMENT_CHANCE else KIND_DATE
        kinds = [first] + [k for k in kinds if k != first]
    base, _ = split_topic(topic)
    if base == SUBJECT_ANY:
        # «будь-який предмет»: порядок предметів випадковий
        groups = [[k for s in SUBJECT_SETTINGS[sub] for k in SETTING_TOPICS[s] if k in kinds] for sub in SUBJECT_ORDER]
        random.shuffle(groups)
        kinds = [k for group in groups for k in random.sample(group, len(group))]
    return kinds

SUBJECT_TITLES = {
    SUBJECT_HIST: "Історія України",
    SUBJECT_LANG: "Українська мова",
}

# Імовірність того, що картка з історії буде пам'яткою (якщо в базі є пам'ятки)
MONUMENT_CHANCE = 0.35

VOWELS_UPPER = "АЕИІОУЮЯЄЇ"
ACUTE = "\u0301"  # комбінований знак наголосу


# ---------------------------------------------------------------------- наголоси
def normalize_accent_word(raw: str) -> str | None:
    """Приводить слово до формату «алфавІт» (наголошена голосна — велика).

    Приймає й слова зі знаком наголосу (алфаві́т). Повертає None, якщо формат некоректний.
    """
    word = raw.strip()
    if ACUTE in word:
        chars: list[str] = []
        for ch in word:
            if ch == ACUTE:
                if chars:
                    chars[-1] = chars[-1].upper()
            else:
                chars.append(ch)
        word = "".join(chars)

    if not 2 <= len(word) <= 40 or " " in word:
        return None
    stressed = [ch for ch in word if ch in VOWELS_UPPER]
    if not 1 <= len(stressed) <= 2:  # 2 наголоси = допустимі обидва варіанти
        return None
    if any(ch.isupper() and ch not in VOWELS_UPPER for ch in word):
        return None
    return word


def split_note_qualifier(note: str | None) -> tuple[str, str]:
    """«(користь) Наголос на 1-му складі.» -> ('користь', 'Наголос на 1-му складі.')."""
    match = re.match(r"^\(([^)]+)\)\s*(.*)$", note or "", re.S)
    return (match.group(1), match.group(2)) if match else ("", note or "")


def accent_variants(word: str) -> list[str]:
    """Усі допустимі варіанти наголосу слова зі знаком наголосу.

    «алфАвІт» -> ['алфа́віт', 'алфаві́т'];  «бАрмен» -> ['ба́рмен'].
    """
    positions = [i for i, ch in enumerate(word) if ch in VOWELS_UPPER]
    variants = []
    for pos in positions:
        variants.append("".join(
            (ch.lower() + ACUTE if i == pos else ch.lower()) if ch in VOWELS_UPPER else ch
            for i, ch in enumerate(word)
        ))
    return variants


def all_stress_marks(word: str) -> list[str]:
    """Слово з наголосом на кожній голосній по черзі (для квізу): усі можливі розстановки."""
    positions = [i for i, ch in enumerate(word) if ch.lower() in "аеєиіїоуюя"]
    return [
        "".join((ch.lower() + ACUTE if i == pos else ch.lower()) if ch.lower() in "аеєиіїоуюя" else ch
                for i, ch in enumerate(word))
        for pos in positions
    ]


def to_marked(word: str) -> str:
    """алфавІт -> алфаві́т (для гарного відображення)."""
    return "".join(ch.lower() + ACUTE if ch in VOWELS_UPPER else ch for ch in word)


# Велика голосна ВСЕРЕДИНІ слова (після літери) = позначка наголосу: «піцЕрія»
_STRESS_UPPER = re.compile(r"(?<=[^\W\d_])([АЕИІОУЮЯЄЇ])")
_MARKED_VOWEL = re.compile(r"(.)\u0301")


def normalize_mnemonic(raw: str) -> str | None:
    """Приводить фразу до формату зі знаками наголосу (U+0301).

    Приймає наголос і як знак (піце́рія), і як велику голосну всередині слова (піцЕрія).
    Повертає None, якщо наголосів немає або довжина некоректна.
    """
    text = " ".join(raw.split())
    text = _STRESS_UPPER.sub(lambda m: m.group(1).lower() + ACUTE, text)
    if ACUTE not in text or not 5 <= len(text) <= 300:
        return None
    return text


def strip_stress(text: str) -> str:
    """Прибирає знаки наголосу (для лицьової сторони картки)."""
    return text.replace(ACUTE, "")


def highlight_stress(text: str) -> str:
    """HTML: екранує текст і виділяє жирним наголошені голосні."""
    return _MARKED_VOWEL.sub(lambda m: f"<b>{m.group(1)}{ACUTE}</b>", escape(text))


# ---------------------------------------------------------------------- модель картки
@dataclass(frozen=True, slots=True)
class Card:
    kind: str                 # d / a / p / m
    item_id: int
    subject: str              # hist / lang
    header: str               # заголовок (жирним)
    question: str             # лицьова сторона (HTML)
    answer: str               # відповідь (HTML), показується за кнопкою
    photo: str | None = None  # file_id фото (для пам'яток)
    topic_code: str = ""      # тема для кнопки «Ще одну картку» (напр. «c3»); порожньо = предмет картки


def make_card(kind: str, item: object) -> Card:
    if kind == KIND_DATE:
        assert isinstance(item, HistoryDate)
        return Card(
            kind=kind,
            item_id=item.id,
            subject=SUBJECT_HIST,
            header="📜 Історія України · Дата",
            question=f"Коли це сталося?\n\n<i>{escape(item.event)}</i>",
            answer=f"✅ <b>{escape(item.year)}</b>",
        )
    if kind == KIND_MONUMENT:
        assert isinstance(item, Monument)
        description = escape(item.description[:700]) or "Опис відсутній."
        return Card(
            kind=kind,
            item_id=item.id,
            subject=SUBJECT_HIST,
            header="🏛 Історія України · Пам'ятка",
            question="Що це за пам'ятка? Спробуй пригадати назву, епоху та значення.",
            answer=f"✅ <b>{escape(item.title[:150])}</b>\n\n{description}",
            photo=item.photo_file_id,
        )
    if kind == KIND_ACCENT:
        assert isinstance(item, Accent)
        # Підказка може починатися з уточнення в дужках: «(користь) Наголос на 1-му складі.»
        qualifier, note_text = split_note_qualifier(item.note)
        hint = f" (<i>{escape(qualifier)}</i>)" if qualifier else ""
        variants = " і ".join(f"<b>{escape(v)}</b>" for v in accent_variants(item.word))
        note = f"\n{escape(note_text)}" if note_text else ""
        return Card(
            kind=kind,
            item_id=item.id,
            subject=SUBJECT_LANG,
            header="🔤 Українська мова · Наголос",
            question=f"Де наголос у слові <b>{escape(item.word.lower())}</b>{hint}?",
            answer=f"✅ {variants}{note}",
        )
    if kind == KIND_PHRASE:
        assert isinstance(item, Phrase)
        return Card(
            kind=kind,
            item_id=item.id,
            subject=SUBJECT_LANG,
            header="💬 Українська мова · Фразеологізм",
            question=f"Що означає фразеологізм <b>«{escape(item.phrase)}»</b>?",
            answer=f"✅ {escape(item.meaning)}",
        )
    if kind == KIND_CUSTOM:
        assert isinstance(item, CustomCard)
        return Card(
            kind=kind,
            item_id=item.id,
            subject=SUBJECT_OTHER,
            header=f"📚 {escape(item.topic)}",
            question=escape(item.question),
            answer=f"✅ {escape(item.answer)}",
            topic_code=f"c{item.topic_id}",
        )
    if kind == KIND_FOREIGN:
        assert isinstance(item, ForeignWord)
        return Card(
            kind=kind,
            item_id=item.id,
            subject=SUBJECT_LANG,
            header="🌐 Українська мова · Відповідник іншомовного слова",
            question=f"Який український відповідник має слово <b>{escape(item.word)}</b>?",
            answer=f"✅ <b>{escape(item.word)}</b> — {escape(item.synonym)}",
        )
    if kind == KIND_MNEMONIC:
        assert isinstance(item, Mnemonic)
        category = f" · {escape(item.category)}" if item.category else ""
        return Card(
            kind=kind,
            item_id=item.id,
            subject=SUBJECT_LANG,
            header=f"🧠 Українська мова · Запам'ятай наголос{category}",
            question=f"Спробуй розставити наголоси у фразі:\n\n<i>{escape(strip_stress(item.text))}</i>",
            answer=f"✅ {highlight_stress(item.text)}",
        )
    raise ValueError(f"Невідомий тип картки: {kind}")


async def build_card(db: Database, topic: str, enabled: set[str] | None = None) -> Card | None:
    """Випадкова картка теми. None — якщо матеріалів немає.

    `enabled` — увімкнені теми користувача (для предметів); None = усі.
    """
    kinds = topic_kinds(topic, enabled)
    if topic == TOPIC_MISTAKES or not kinds:
        return None  # помилки обирає services.srs (потрібен користувач)
    _, custom_topic = split_topic(topic)
    for kind in ordered_kinds(kinds, topic):
        item = await db.random_item(kind, topic_id=custom_topic if kind == KIND_CUSTOM else None)
        if item is not None:
            return make_card(kind, item)
    return None


async def build_card_by_id(db: Database, kind: str, item_id: int) -> Card | None:
    item = await db.get_item(kind, item_id)
    return make_card(kind, item) if item is not None else None


# ---------------------------------------------------------------------- відображення
def render_front(card: Card) -> str:
    return f"<b>{card.header}</b>\n\n{card.question}"


def render_back(card: Card) -> str:
    return f"{render_front(card)}\n\n{card.answer}"


def card_keyboard(card: Card, stage: int = 0, more: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки під карткою.

    stage 0 — питання: «Показати відповідь»; 1 — відповідь: «Знав» / «Не знав»; 2 — оцінено: лише «Ще одну».
    `more` — тема для «Ще одну картку» (за замовчуванням тема картки).
    """
    more = more if is_valid_topic(more) else (card.topic_code or card.subject)
    rows: list[list[InlineKeyboardButton]] = []
    if stage == 0:
        rows.append([
            InlineKeyboardButton(text="👁 Показати відповідь", callback_data=f"rev:{card.kind}:{card.item_id}:{more}")
        ])
    elif stage == 1:
        rows.append([
            InlineKeyboardButton(text="✅ Знав", callback_data=f"kn:{card.kind}:{card.item_id}:1:{more}"),
            InlineKeyboardButton(text="❌ Не знав", callback_data=f"kn:{card.kind}:{card.item_id}:0:{more}"),
        ])
    rows.append([InlineKeyboardButton(text="🔄 Ще одну картку", callback_data=f"now:{more}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_word_of_day(card: Card) -> str:
    """Текст «слова дня»: питання та одразу відповідь."""
    return f"☀️ <b>Слово дня</b>\n\n{card.header}\n\n{card.question}\n\n{card.answer}"


async def send_card(bot: Bot, chat_id: int, card: Card, more: str | None = None) -> Message:
    """Надсилає картку. Якщо фото недоступне — надсилає текстову версію."""
    markup = card_keyboard(card, more=more)
    if card.photo:
        try:
            return await bot.send_photo(chat_id, card.photo, caption=render_front(card), reply_markup=markup)
        except TelegramBadRequest as exc:
            logger.error("Не вдалося надіслати фото пам'ятки #%s: %s — надсилаю текстом", card.item_id, exc)
    return await bot.send_message(chat_id, render_front(card), reply_markup=markup)
