"""Масовий імпорт матеріалів із CSV/JSON-файлу, надісланого адміном."""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from database.db import Database
from services import validators as v

logger = logging.getLogger(__name__)

MAX_BYTES = 2_000_000
MAX_ROWS = 5000
HEADER_WORDS = {
    "year", "дата", "рік", "word", "слово", "phrase", "фразеологізм", "category", "категорія",
    "topic", "тема", "предмет", "subject",
}


@dataclass(frozen=True, slots=True)
class ImportSpec:
    kind: str
    title: str
    columns: tuple[str, ...]          # колонки у файлі (і ключі JSON)
    validators: tuple[Callable, ...]  # перевірка кожної колонки
    required: int                     # скільки колонок обов'язкові (решта — необов'язкові)
    example: str                      # приклад рядка CSV для підказки адміну


SPECS: dict[str, ImportSpec] = {
    "d": ImportSpec("d", "📅 Дати", ("year", "event"), (v.v_date, v.v_event), 2,
                    "988;Хрещення Русі за князя Володимира"),
    "a": ImportSpec("a", "🔤 Наголоси", ("word", "note"), (v.v_word, v.v_note), 1,
                    "каталОг;Наголос на 3-му складі."),
    "f": ImportSpec("f", "🌐 Відповідники іншомовних слів", ("word", "synonym"), (v.v_foreign_word, v.v_synonym), 2,
                    "абзац;відступ"),
    "r": ImportSpec("r", "💬 Фразеологізми", ("phrase", "meaning"), (v.v_phrase, v.v_meaning), 2,
                    "Бити байдики;ледарювати"),
    "n": ImportSpec("n", "🧠 Фрази для наголосів", ("category", "text"), (v.v_category, v.v_mnemonic), 2,
                    "Їжа та напої;піцЕрія — феєрія смаку"),
    "q": ImportSpec("q", "📚 Інші предмети (завдання)", ("topic", "question", "answer"),
                    (v.v_topic_name, v.v_question, v.v_answer), 3,
                    "Математика;Чому дорівнює 7×8?;56"),
}


@dataclass(slots=True)
class ImportResult:
    added: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    fatal: str = ""


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).replace("⚠️", "").strip()


def _read_rows(spec: ImportSpec, filename: str, raw: bytes) -> list[list[str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Файл має бути в кодуванні UTF-8 (у Excel: «CSV UTF-8»).") from exc

    if filename.lower().endswith(".json") or text.lstrip().startswith(("[", "{")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Некоректний JSON: {exc.msg} (рядок {exc.lineno}).") from exc
        if not isinstance(data, list):
            raise ValueError("JSON має бути масивом об'єктів.")
        rows = []
        for item in data:
            if isinstance(item, dict):
                rows.append([str(item.get(col, "") or "") for col in spec.columns])
            elif isinstance(item, list):
                rows.append([str(x) for x in item])
            else:
                rows.append([])
        return rows

    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = max(";,\t", key=lambda d: first.count(d)) if any(d in first for d in ";,\t") else ";"
    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    if rows and rows[0] and rows[0][0].strip().lower() in HEADER_WORDS:
        rows = rows[1:]  # перший рядок — заголовок
    return rows


async def run_import(db: Database, kind: str, filename: str, raw: bytes) -> ImportResult:
    """Розбирає файл, перевіряє значення й додає в базу лише нові коректні записи."""
    spec = SPECS[kind]
    result = ImportResult()
    if len(raw) > MAX_BYTES:
        result.fatal = "Файл завеликий (максимум 2 МБ)."
        return result
    try:
        rows = _read_rows(spec, filename, raw)
    except ValueError as exc:
        result.fatal = str(exc)
        return result
    if not rows:
        result.fatal = "У файлі немає рядків."
        return result
    if len(rows) > MAX_ROWS:
        result.fatal = f"Забагато рядків (максимум {MAX_ROWS})."
        return result

    # --- наявні ключі для пропуску дублікатів
    if kind == "d":
        seen = {x.casefold() for x in await db.existing_values("history_dates", "event")}
    elif kind == "a":
        seen = await db.existing_values("accents", "word")
    elif kind == "f":
        seen = {w.casefold() for w in await db.existing_values("foreign_words", "word")}
    elif kind == "r":
        seen = {x.casefold() for x in await db.existing_values("phrases", "phrase")}
    elif kind == "n":
        seen = await db.existing_values("mnemonics", "text")
    else:
        seen = await db.custom_keys()

    valid: list[tuple[str, ...]] = []
    topic_ids: dict[str, int] = {}
    for line_no, row in enumerate(rows, start=1):
        if not row or all(not cell.strip() for cell in row):
            continue
        row = [cell for cell in row][: len(spec.columns)]
        if len(row) < spec.required:
            result.errors.append(f"рядок {line_no}: потрібно колонок — {spec.required}")
            continue
        row += [""] * (len(spec.columns) - len(row))
        values: list[str] = []
        failed = False
        for validator, cell in zip(spec.validators, row):
            value, error = validator(cell)
            if value is None:
                result.errors.append(f"рядок {line_no}: {_strip_tags(error)}")
                failed = True
                break
            values.append(value)
        if failed:
            continue

        if kind == "q":
            name = values[0]
            if name.casefold() not in topic_ids:
                topic_ids[name.casefold()] = await db.add_custom_topic(name)
            key = (topic_ids[name.casefold()], values[1].casefold())
            values = [str(topic_ids[name.casefold()]), values[1], values[2]]
        elif kind == "d":
            key = values[1].casefold()
        elif kind == "a":
            key = values[0]
        elif kind == "f":
            key = values[0].casefold()
        elif kind == "r":
            key = values[0].casefold()
        else:
            key = values[1]
        if key in seen:
            result.skipped += 1
            continue
        seen.add(key)
        valid.append(tuple(values))

    if valid:
        if kind == "d":
            await db.bulk_insert("history_dates", ("year", "event"), valid)
        elif kind == "a":
            await db.bulk_insert("accents", ("word", "note"), valid)
        elif kind == "f":
            await db.bulk_insert("foreign_words", ("word", "synonym"), valid)
        elif kind == "r":
            await db.bulk_insert("phrases", ("phrase", "meaning"), valid)
        elif kind == "n":
            await db.bulk_insert("mnemonics", ("category", "text"), valid)
        else:
            await db.bulk_insert("custom_cards", ("topic_id", "question", "answer"),
                                 [(int(t), q, a) for t, q, a in valid])
    result.added = len(valid)
    logger.info("Імпорт %s: додано %d, пропущено дублікатів %d, помилок %d",
                kind, result.added, result.skipped, len(result.errors))
    return result
