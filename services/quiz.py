"""Квізи у вигляді нативних опитувань Telegram (type=quiz) з автоматично підібраними хибними варіантами."""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, PollAnswer

from database.db import Database
from database.models import KIND_ACCENT, KIND_CUSTOM, KIND_DATE, KIND_FOREIGN, KIND_PHRASE
from services.cards import (
    TOPIC_MISTAKES,
    accent_variants,
    all_stress_marks,
    ordered_kinds,
    split_note_qualifier,
    split_topic,
    topic_kinds,
)

logger = logging.getLogger(__name__)

QUIZ_KINDS = {KIND_DATE, KIND_ACCENT, KIND_FOREIGN, KIND_PHRASE, KIND_CUSTOM}
SESSION_LENGTH = 10
MAX_QUESTION, MAX_OPTION, MAX_EXPLANATION = 300, 100, 200  # ліміти Telegram для опитувань


@dataclass(frozen=True, slots=True)
class QuizSpec:
    kind: str
    item_id: int
    question: str
    options: list[str]
    correct: int
    explanation: str


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _spec(kind: str, item_id: int, question: str, correct: str, wrong: list[str], explanation: str) -> QuizSpec | None:
    """Збирає варіанти: правильний + до 3 хибних (унікальні, перемішані)."""
    seen = {correct.casefold()}
    options = [correct]
    for value in wrong:
        if value.casefold() not in seen:
            seen.add(value.casefold())
            options.append(value)
        if len(options) == 4:
            break
    if len(options) < 2:
        return None
    random.shuffle(options)
    return QuizSpec(
        kind=kind,
        item_id=item_id,
        question=_clip(question, MAX_QUESTION),
        options=[_clip(o, MAX_OPTION) for o in options],
        correct=options.index(correct),
        explanation=_clip(explanation, MAX_EXPLANATION),
    )


async def _quiz_date(db: Database) -> QuizSpec | None:
    rows = await db.random_rows("d", "length(event) <= 270 AND length(year) <= 100", n=1)
    if not rows:
        return None
    item = rows[0]
    others = await db.random_rows("d", "length(year) <= 100", n=15)
    wrong = [o.year for o in others if o.year.casefold() != item.year.casefold()]
    if len({w.casefold() for w in wrong}) < 3:
        return None
    return _spec("d", item.id, f"Коли це сталося?\n{item.event}", item.year, wrong, f"Відповідь: {item.year}")


async def _quiz_accent(db: Database) -> QuizSpec | None:
    for item in await db.random_rows("a", n=12):
        valid = accent_variants(item.word)
        wrong = [v for v in all_stress_marks(item.word) if v not in valid]
        if not wrong:
            continue
        random.shuffle(wrong)
        qualifier, note = split_note_qualifier(item.note)
        hint = f" ({qualifier})" if qualifier else ""
        explanation = " або ".join(valid) + (f". {note}" if note else "")
        return _spec("a", item.id, f"Де наголос у слові «{item.word.lower()}»{hint}?", valid[0], wrong, explanation)
    return None


async def _quiz_foreign(db: Database) -> QuizSpec | None:
    rows = await db.random_rows("f", "length(synonym) <= 100", n=1)
    if not rows:
        return None
    item = rows[0]
    others = await db.random_rows("f", "length(synonym) <= 100", n=20)
    wrong = [o.synonym for o in others if o.synonym.casefold() != item.synonym.casefold()]
    return _spec("f", item.id, f"Який український відповідник до слова «{item.word}»?", item.synonym, wrong,
                 f"{item.word} — {item.synonym}")


async def _quiz_phrase(db: Database) -> QuizSpec | None:
    rows = await db.random_rows("r", "length(meaning) <= 100", n=1)
    if not rows:
        return None
    item = rows[0]
    others = await db.random_rows("r", "length(meaning) <= 100", n=20)
    wrong = [o.meaning for o in others if o.meaning.casefold() != item.meaning.casefold()]
    return _spec("r", item.id, f"Що означає фразеологізм «{item.phrase}»?", item.meaning, wrong, item.meaning)


async def _quiz_custom(db: Database, topic_id: int | None) -> QuizSpec | None:
    rows = await db.random_rows("q", "length(answer) <= 100 AND length(question) <= 280", n=1, topic_id=topic_id)
    if not rows:
        return None
    item = rows[0]
    others = await db.random_rows("q", "length(answer) <= 100", n=20, topic_id=item.topic_id)
    wrong = [o.answer for o in others if o.answer.casefold() != item.answer.casefold()]
    return _spec("q", item.id, item.question, item.answer, wrong, f"Відповідь: {item.answer}")


async def build_quiz(db: Database, topic: str, enabled: set[str] | None = None) -> QuizSpec | None:
    """Одне питання-квіз з теми. None — якщо матеріалів для квізу недостатньо."""
    if topic == TOPIC_MISTAKES:
        return None
    kinds = [k for k in topic_kinds(topic, enabled) if k in QUIZ_KINDS]
    _, custom_topic = split_topic(topic)
    for kind in ordered_kinds(kinds, topic):
        if kind == KIND_DATE:
            spec = await _quiz_date(db)
        elif kind == KIND_ACCENT:
            spec = await _quiz_accent(db)
        elif kind == KIND_FOREIGN:
            spec = await _quiz_foreign(db)
        elif kind == KIND_PHRASE:
            spec = await _quiz_phrase(db)
        else:
            spec = await _quiz_custom(db, custom_topic)
        if spec is not None:
            return spec
    return None


async def send_quiz(bot: Bot, db: Database, chat_id: int, spec: QuizSpec, session_id: int | None = None) -> Message:
    """Надсилає опитування-квіз і запам'ятовує правильну відповідь для poll_id."""
    message = await bot.send_poll(
        chat_id=chat_id,
        question=spec.question,
        options=spec.options,
        type="quiz",
        correct_option_id=spec.correct,
        is_anonymous=False,
        explanation=spec.explanation or None,
    )
    await db.add_poll(message.poll.id, chat_id, spec.kind, spec.item_id, spec.correct, session_id)
    return message


async def start_session(
    bot: Bot, db: Database, user_id: int, topic: str, enabled: set[str] | None = None, total: int = SESSION_LENGTH
) -> bool:
    """Починає раунд із `total` питань. False — якщо матеріалів немає."""
    spec = await build_quiz(db, topic, enabled)
    if spec is None:
        return False
    session_id = await db.create_session(user_id, topic, total)
    await send_quiz(bot, db, user_id, spec, session_id)
    return True


def _again_markup(topic: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎯 Ще раунд", callback_data=f"qz:{topic}")]])


async def handle_poll_answer(bot: Bot, db: Database, answer: PollAnswer) -> None:
    """Відповідь на квіз: фіксуємо результат (для прогресу й статистики) і йдемо до наступного питання."""
    if not answer.option_ids:  # користувач відкликав голос — нічого не робимо
        return
    row = await db.pop_poll(answer.poll_id)
    if row is None:
        return
    ok = answer.option_ids[0] == row["correct_option"]
    user_id = row["user_id"]
    await db.record_review(user_id, row["kind"], row["item_id"], ok, source="quiz")

    session_id = row["session_id"]
    if session_id is None:
        return
    session = await db.advance_session(session_id, ok)
    if session is None:
        return
    if session["finished"]:
        total, correct = session["total"], session["correct"]
        percent = round(100 * correct / total) if total else 0
        await bot.send_message(
            user_id,
            f"🏁 Раунд завершено: <b>{correct} з {total}</b> ({percent}%). "
            + ("Чудово! 🎉" if percent >= 80 else "Помилки автоматично потраплять у повторення 🔁"),
            reply_markup=_again_markup(session["topic"]),
        )
        return

    user = await db.get_user(user_id)
    spec = await build_quiz(db, session["topic"], user.topic_set if user else None)
    if spec is None:
        await db.finish_session(session_id)
        await bot.send_message(user_id, "📭 Матеріали для квізу закінчилися.")
        return
    await send_quiz(bot, db, user_id, spec, session_id)
