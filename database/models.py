"""Схема бази даних та прості моделі даних."""
from __future__ import annotations

from dataclasses import dataclass

# Тихий режим за замовчуванням (київський час, години 0-23)
DEFAULT_QUIET_START = 22
DEFAULT_QUIET_END = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    quiet_start    INTEGER NOT NULL DEFAULT 22,
    quiet_end      INTEGER NOT NULL DEFAULT 8,
    is_active      INTEGER NOT NULL DEFAULT 1,
    interval_hours INTEGER NOT NULL DEFAULT 2,       -- як часто надсилати картки (год)
    topics         TEXT    NOT NULL DEFAULT 'dat,mon,acc,for,phr,oth',  -- увімкнені теми
    daily_limit    INTEGER NOT NULL DEFAULT 0,       -- макс. карток за розкладом на добу (0 = без ліміту)
    word_of_day    INTEGER NOT NULL DEFAULT 1,       -- щоденне «слово дня»
    last_subject   TEXT    NOT NULL DEFAULT '',      -- для чергування предметів
    last_sent_at   TEXT    NOT NULL DEFAULT '',      -- ISO-час останньої планової картки
    sent_today     INTEGER NOT NULL DEFAULT 0,
    sent_date      TEXT    NOT NULL DEFAULT '',
    last_wod_date  TEXT    NOT NULL DEFAULT ''
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

-- Фразеологізми: «Авгієві стайні» -> значення
CREATE TABLE IF NOT EXISTS phrases (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase  TEXT NOT NULL,
    meaning TEXT NOT NULL
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

-- Інші предмети: довільні завдання «питання -> відповідь», згруповані за темами (математика, англійська …)
CREATE TABLE IF NOT EXISTS custom_topics (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS custom_cards (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES custom_topics(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS custom_cards_v AS
    SELECT c.id AS id, c.topic_id AS topic_id, c.question AS question, c.answer AS answer, t.name AS topic
    FROM custom_cards c JOIN custom_topics t ON t.id = c.topic_id;

-- Прогрес користувача (система інтервального повторення, «коробки» 1-5)
CREATE TABLE IF NOT EXISTS progress (
    user_id INTEGER NOT NULL,
    kind    TEXT    NOT NULL,
    item_id INTEGER NOT NULL,
    box     INTEGER NOT NULL DEFAULT 0,      -- 0 нова, 1 помилка, 2..5 вивчено дедалі краще
    due_at  TEXT    NOT NULL DEFAULT '',     -- UTC ISO: коли повторити
    seen    INTEGER NOT NULL DEFAULT 0,
    correct INTEGER NOT NULL DEFAULT 0,
    wrong   INTEGER NOT NULL DEFAULT 0,
    last_at TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, kind, item_id)
);
CREATE INDEX IF NOT EXISTS idx_progress_due ON progress (user_id, due_at);

-- Журнал відповідей («знав / не знав» та квізи) — для статистики
CREATE TABLE IF NOT EXISTS reviews (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind    TEXT    NOT NULL,
    item_id INTEGER NOT NULL,
    ok      INTEGER NOT NULL,
    source  TEXT    NOT NULL DEFAULT 'card', -- card | quiz
    at      TEXT    NOT NULL                 -- UTC ISO
);
CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews (user_id, at);

-- Квізи (нативні опитування Telegram): запам'ятовуємо правильну відповідь для poll_id
CREATE TABLE IF NOT EXISTS polls (
    poll_id        TEXT PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    item_id        INTEGER NOT NULL,
    correct_option INTEGER NOT NULL,
    session_id     INTEGER,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quiz_sessions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    topic    TEXT NOT NULL,
    total    INTEGER NOT NULL,
    done     INTEGER NOT NULL DEFAULT 0,
    correct  INTEGER NOT NULL DEFAULT 0,
    finished INTEGER NOT NULL DEFAULT 0
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
    interval_hours: int = 2
    topics: str = "dat,mon,acc,for,phr,oth"
    daily_limit: int = 0
    word_of_day: bool = True
    last_subject: str = ""
    last_sent_at: str = ""
    sent_today: int = 0
    sent_date: str = ""
    last_wod_date: str = ""

    @property
    def topic_set(self) -> set[str]:
        return {t for t in self.topics.split(",") if t}


# Колонки users, яких може не бути в старих базах (додаються міграцією): назва -> DDL-фрагмент
USER_COLUMNS: dict[str, str] = {
    "interval_hours": "INTEGER NOT NULL DEFAULT 2",
    "topics": "TEXT NOT NULL DEFAULT 'dat,mon,acc,for,phr,oth'",
    "daily_limit": "INTEGER NOT NULL DEFAULT 0",
    "word_of_day": "INTEGER NOT NULL DEFAULT 1",
    "last_subject": "TEXT NOT NULL DEFAULT ''",
    "last_sent_at": "TEXT NOT NULL DEFAULT ''",
    "sent_today": "INTEGER NOT NULL DEFAULT 0",
    "sent_date": "TEXT NOT NULL DEFAULT ''",
    "last_wod_date": "TEXT NOT NULL DEFAULT ''",
}
EDITABLE_USER_FIELDS = frozenset(USER_COLUMNS) | {"quiet_start", "quiet_end", "is_active"}

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
class Phrase:
    id: int
    phrase: str
    meaning: str


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
class CustomCard:
    id: int
    topic_id: int
    question: str
    answer: str
    topic: str  # назва теми (з подання custom_cards_v)


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
KIND_PHRASE = "r"
KIND_MONUMENT = "m"
KIND_CUSTOM = "q"

KIND_TABLES: dict[str, tuple[str, type]] = {
    KIND_DATE: ("history_dates", HistoryDate),
    KIND_ACCENT: ("accents", Accent),
    KIND_MNEMONIC: ("mnemonics", Mnemonic),
    KIND_FOREIGN: ("foreign_words", ForeignWord),
    KIND_PHRASE: ("phrases", Phrase),
    KIND_MONUMENT: ("monuments", Monument),
    KIND_CUSTOM: ("custom_cards", CustomCard),
}

# Звідки читати записи (для custom_cards — подання з назвою теми)
READ_SOURCES: dict[str, str] = {"custom_cards": "custom_cards_v"}

ALLOWED_TABLES = frozenset(table for table, _ in KIND_TABLES.values())

# Колонки, які дозволено змінювати через адмін-панель (захист від підстановки в SQL)
EDITABLE_COLUMNS: dict[str, frozenset[str]] = {
    "history_dates": frozenset({"year", "event"}),
    "accents": frozenset({"word", "note"}),
    "monuments": frozenset({"title", "photo_file_id", "description"}),
    "mnemonics": frozenset({"category", "text"}),
    "foreign_words": frozenset({"word", "synonym"}),
    "phrases": frozenset({"phrase", "meaning"}),
    "custom_cards": frozenset({"question", "answer"}),
}
