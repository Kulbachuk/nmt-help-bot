"""Перевірка й нормалізація введених адміном (або імпортованих) значень.

Кожен валідатор повертає (значення, "") або (None, текст помилки українською).
"""
from __future__ import annotations

from services.cards import normalize_accent_word, normalize_mnemonic

Result = tuple["str | None", str]


def v_date(raw: str) -> Result:
    """Дата — вільний текст: рік, період, місяць і рік, століття («988», «1932–1933», «травень 1648», «VII–III ст. до н. е.»)."""
    date = " ".join(raw.split())
    if not 1 <= len(date) <= 60:
        return None, (
            "⚠️ Дата — від 1 до 60 символів. Приклади: <code>988</code>, <code>1932–1933</code>, "
            "<code>травень 1648</code>, <code>22 січня 1919</code>."
        )
    return date, ""


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


def v_topic_name(raw: str) -> Result:
    name = " ".join(raw.split())
    if not 2 <= len(name) <= 40:
        return None, "⚠️ Назва предмета/теми — від 2 до 40 символів (наприклад: Математика)."
    return name, ""


def v_question(raw: str) -> Result:
    question = raw.strip()
    if not 3 <= len(question) <= 600:
        return None, "⚠️ Питання — від 3 до 600 символів."
    return question, ""


def v_answer(raw: str) -> Result:
    answer = raw.strip()
    if not 1 <= len(answer) <= 600:
        return None, "⚠️ Відповідь — від 1 до 600 символів."
    return answer, ""
