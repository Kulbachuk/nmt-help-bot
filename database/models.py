"""Схема бази даних та прості моделі даних."""
from __future__ import annotations

from dataclasses import dataclass

# Тихий режим за замовчуванням (київський час, години 0-23)
DEFAULT_QUIET_START = 22
DEFAULT_QUIET_END = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    quiet_start INTEGER NOT NULL DEFAULT 22,
    quiet_end   INTEGER NOT NULL DEFAULT 8,
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS history_dates (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    year  TEXT NOT NULL,           -- TEXT, бо бувають періоди: «1932–1933»
    event TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accents (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT NOT NULL,            -- наголошена літера записана ВЕЛИКОЮ: «алфавІт»
    note TEXT NOT NULL DEFAULT ''
);

-- Відповідники іншомовних слів: «абзац» -> «відступ»
CREATE TABLE IF NOT EXISTS foreign_words (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    word    TEXT NOT NULL,
    synonym TEXT NOT NULL
);

-- Римовані фрази для запам'ятовування наголосів.
-- Наголос позначено знаком U+0301 після голосної: «піце́рія»
CREATE TABLE IF NOT EXISTS mnemonics (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monuments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    photo_file_id TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT ''
);

-- Службова таблиця «ключ-значення»: тут зберігається, який предмет надсилався останнім
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class User:
    user_id: int
    quiet_start: int
    quiet_end: int
    is_active: bool


@dataclass(frozen=True, slots=True)
class HistoryDate:
    id: int
    year: str
    event: str


@dataclass(frozen=True, slots=True)
class Accent:
    id: int
    word: str
    note: str


@dataclass(frozen=True, slots=True)
class ForeignWord:
    id: int
    word: str
    synonym: str


@dataclass(frozen=True, slots=True)
class Mnemonic:
    id: int
    category: str
    text: str


@dataclass(frozen=True, slots=True)
class Monument:
    id: int
    title: str
    photo_file_id: str
    description: str


# Код типу картки -> (таблиця, модель). Використовується і в callback_data.
KIND_DATE = "d"
KIND_ACCENT = "a"
KIND_MNEMONIC = "n"
KIND_FOREIGN = "f"
KIND_MONUMENT = "m"

KIND_TABLES: dict[str, tuple[str, type]] = {
    KIND_DATE: ("history_dates", HistoryDate),
    KIND_ACCENT: ("accents", Accent),
    KIND_MNEMONIC: ("mnemonics", Mnemonic),
    KIND_FOREIGN: ("foreign_words", ForeignWord),
    KIND_MONUMENT: ("monuments", Monument),
}

ALLOWED_TABLES = frozenset(table for table, _ in KIND_TABLES.values())

# Колонки, які дозволено змінювати через адмін-панель (захист від підстановки в SQL)
EDITABLE_COLUMNS: dict[str, frozenset[str]] = {
    "history_dates": frozenset({"year", "event"}),
    "accents": frozenset({"word", "note"}),
    "monuments": frozenset({"title", "photo_file_id", "description"}),
    "mnemonics": frozenset({"category", "text"}),
    "foreign_words": frozenset({"word", "synonym"}),
}
