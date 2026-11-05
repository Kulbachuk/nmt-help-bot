"""Асинхронний доступ до SQLite (aiosqlite) та первинне наповнення бази."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from database.models import (
    ALLOWED_TABLES,
    EDITABLE_COLUMNS,
    EDITABLE_USER_FIELDS,
    KIND_TABLES,
    READ_SOURCES,
    SCHEMA,
    USER_COLUMNS,
    User,
)

logger = logging.getLogger(__name__)

# (таблиця, файл у data/, поля, які беремо з JSON)
SEED_SOURCES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("history_dates", "dates.json", ("year", "event")),
    ("accents", "accents.json", ("word", "note")),
    ("mnemonics", "mnemonics.json", ("text", "category")),
    ("foreign_words", "foreign_words.json", ("word", "synonym")),
    ("phrases", "phrases.json", ("phrase", "meaning")),
)


class Database:
    """Тонка обгортка над одним спільним з'єднанням aiosqlite."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------ життєвий цикл
    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База даних не підключена: спочатку викличте connect()")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # LOWER() у SQLite не розуміє кирилицю — реєструємо свою функцію для пошуку
        await self._conn.create_function("ulower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
        logger.info("Підключено до SQLite: %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("З'єднання з БД закрито")

    async def init(self, data_dir: Path) -> None:
        """Створює таблиці та за потреби завантажує стартові дані."""
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()
        logger.info("Схему БД перевірено/створено")
        await self._migrate()
        await self.seed(data_dir)
        await self.import_packs(data_dir)

    # Виправлення стартових наголосів, помилково записаних у ранніх версіях seed.
    # Спрацьовують лише якщо запис не чіпали (слово збігається дослівно). Ідемпотентні.
    ACCENT_FIXES: tuple[tuple[str, str, str], ...] = (
        ("гуртожИток", "гуртОжиток", "Наголос на 2-му складі."),
        ("зАвдання", "завдАння", "Наголос на 2-му складі."),
        ("шОфер", "шофЕр", "Наголос на 2-му (останньому) складі."),
        # за переліком НМТ-2025 ці слова мають два допустимі наголоси
        ("алфавІт", "алфАвІт", "Допустимі два наголоси: на 2-му та 3-му складах."),
        ("зАвжди", "зАвждИ", "Допустимі два наголоси: на 1-му та 2-му складах."),
        ("дОговір", "дОговІр", "Допустимі два наголоси: на 1-му та 3-му складах."),
    )
    ACCENT_REMOVE: tuple[str, ...] = ("дОцент",)  # наголос не підтверджено офіційним переліком

    # Стартові дати з ранніх версій бота (мій перший короткий список). Повний перелік подій з НМТ
    # їх замінює; видаляються лише незмінені рядки й лише до першого імпорту пакета dates_nmt_v1.
    LEGACY_DATES: tuple[tuple[str, str], ...] = (
        ('882', "Князь Олег захопив Київ і об'єднав Північну та Південну Русь — утворилася держава Київська Русь."),
        ('988', 'Хрещення Русі за князя Володимира Великого: християнство стало державною релігією.'),
        ('1037', 'За літописною традицією — закладення Софійського собору в Києві князем Ярославом Мудрим.'),
        ('1054', 'Смерть князя Ярослава Мудрого; поділ земель між синами.'),
        ('1187', 'Перша літописна згадка назви «Україна» (Київський літопис).'),
        ('1199', "Князь Роман Мстиславич об'єднав Галицьке та Волинське князівства."),
        ('1240', 'Захоплення Києва монголо-татарами на чолі з ханом Батиєм.'),
        ('1253', 'Коронація князя Данила Галицького королівською короною.'),
        ('1385', 'Кревська унія між Великим князівством Литовським і Польським королівством.'),
        ('1569', 'Люблінська унія: утворення Речі Посполитої, більшість українських земель опинилася під владою Польщі.'),
        ('1596', 'Берестейська церковна унія: виникнення Греко-католицької (Української унійної) церкви.'),
        ('1648', 'Початок Національно-визвольної війни під проводом Богдана Хмельницького (битви під Жовтими Водами та Корсунем).'),
        ('1649', 'Зборівський договір Богдана Хмельницького з польським королем: визнання козацької автономії.'),
        ('1654', 'Переяславська рада та укладення українсько-московського договору.'),
        ('1710', 'Ухвалення «Конституції прав і вольностей Війська Запорозького» Пилипа Орлика.'),
        ('1775', 'Зруйнування Нової (Підпільненської) Запорозької Січі російськими військами.'),
        ('1798', 'Вихід перших частин «Енеїди» Івана Котляревського — початок нової української літератури.'),
        ('1840', 'Вихід першого видання «Кобзаря» Тараса Шевченка.'),
        ('1861', 'Скасування кріпацтва в Російській імперії.'),
        ('1917', 'Створення Української Центральної Ради в Києві.'),
        ('1918', 'Проголошення незалежності УНР Четвертим Універсалом Центральної Ради (22 січня).'),
        ('1919', 'Акт Злуки УНР і ЗУНР на Софійській площі в Києві (22 січня).'),
        ('1932–1933', 'Голодомор в Україні — штучний голод, організований радянською владою.'),
        ('1986', 'Аварія на Чорнобильській АЕС (26 квітня).'),
        ('1991', 'Акт проголошення незалежності України (24 серпня); всеукраїнський референдум 1 грудня підтвердив його.'),
        ('1996', 'Ухвалення Конституції України (28 червня).'),
        ('2004', 'Помаранчева революція.'),
        ('2013–2014', 'Революція Гідності.'),
    )

    async def _ensure_columns(self, table: str, columns: dict[str, str]) -> list[str]:
        """Додає в існуючу таблицю відсутні колонки. Повертає імена доданих."""
        rows = await self._fetchall(f"PRAGMA table_info({table})")
        have = {row["name"] for row in rows}
        added = []
        for name, ddl in columns.items():
            if name not in have:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.append(name)
        if added:
            await self.conn.commit()
            logger.info("Міграція: у таблицю %s додано колонки %s", table, ", ".join(added))
        return added

    async def _migrate(self) -> None:
        """Невеликі міграції при оновленні бота."""
        added = await self._ensure_columns("users", USER_COLUMNS)
        if "last_subject" in added:
            # переносимо глобальний стан розсилки в налаштування користувачів
            subject = await self.get_meta("last_subject")
            last_run = await self.get_meta("last_broadcast_at")
            await self.conn.execute(
                "UPDATE users SET last_subject = ?, last_sent_at = ?", (subject or "", last_run or "")
            )
            await self.conn.commit()

        if not await self.get_meta("pack:dates_nmt_v1"):
            removed = 0
            for year, event in self.LEGACY_DATES:
                cursor = await self.conn.execute(
                    "DELETE FROM history_dates WHERE year = ? AND event = ?", (year, event)
                )
                removed += cursor.rowcount
            if removed:
                await self.conn.commit()
                logger.info("Міграція: замінено %d старих стартових дат повним переліком НМТ", removed)

        row = await self._fetchone(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'proverbs'"
        )
        if row:
            await self.conn.execute("DROP TABLE proverbs")
            logger.info("Міграція: видалено застарілу таблицю proverbs (розділ прибрано)")

        for old, new, note in self.ACCENT_FIXES:
            cursor = await self.conn.execute(
                "UPDATE accents SET word = ?, note = ? WHERE word = ?", (new, note, old)
            )
            if cursor.rowcount:
                logger.info("Міграція: наголос «%s» виправлено на «%s»", old, new)
        for word in self.ACCENT_REMOVE:
            cursor = await self.conn.execute("DELETE FROM accents WHERE word = ?", (word,))
            if cursor.rowcount:
                logger.info("Міграція: запис «%s» видалено (не підтверджено словником)", word)
        await self.conn.commit()

    # ------------------------------------------------------------------ допоміжні
    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def _insert(self, sql: str, params: tuple[Any, ...]) -> int:
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor.lastrowid or 0

    # ------------------------------------------------------------------ seed
    @staticmethod
    async def _load_rows(path: Path, fields: tuple[str, ...]) -> list[tuple[str, ...]] | None:
        """Читає JSON-масив і повертає кортежі значень полів. None — файл відсутній/пошкоджений."""
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            items = json.loads(raw)
        except FileNotFoundError:
            logger.warning("Seed: файл %s не знайдено", path)
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Seed: не вдалося прочитати %s: %s", path, exc)
            return None
        if not isinstance(items, list):
            logger.error("Seed: %s має містити JSON-масив", path)
            return None

        rows: list[tuple[str, ...]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                logger.warning("Seed: %s[%d] — не об'єкт, пропускаю", path.name, index)
                continue
            values = tuple(str(item.get(field, "")).strip() for field in fields)
            if not values[0]:
                logger.warning("Seed: %s[%d] — порожнє поле «%s», пропускаю", path.name, index, fields[0])
                continue
            rows.append(values)
        return rows

    async def _insert_rows(self, table: str, fields: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
        placeholders = ", ".join("?" for _ in fields)
        await self.conn.executemany(
            f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({placeholders})", rows
        )
        await self.conn.commit()

    async def seed(self, data_dir: Path) -> None:
        """Якщо таблиця порожня — завантажує стартові дані з data/*.json."""
        for table, filename, fields in SEED_SOURCES:
            existing = await self.count(table)
            if existing:
                logger.info("Seed: таблиця %s вже містить %d записів — пропускаю", table, existing)
                continue
            rows = await self._load_rows(data_dir / filename, fields)
            if not rows:
                logger.warning("Seed: таблиця %s залишається порожньою", table)
                continue
            await self._insert_rows(table, fields, rows)
            logger.info("Seed: у таблицю %s додано %d записів з %s", table, len(rows), filename)

    # Одноразове донаповнення вже заселених таблиць новими матеріалами з data/*.json.
    # (таблиця, файл, ключове поле, поля, прапорець у meta). Додаються лише відсутні за ключем
    # записи, і лише один раз: видалені адміном записи при наступних запусках не повертаються.
    IMPORT_PACKS: tuple[tuple[str, str, str, tuple[str, ...], str], ...] = (
        ("accents", "accents.json", "word", ("word", "note"), "pack:accents_nmt2025"),
        ("mnemonics", "mnemonics.json", "text", ("text", "category"), "pack:mnemonics_rules_v1"),
        ("history_dates", "dates.json", "event", ("event", "year"), "pack:dates_nmt_v1"),
    )

    async def import_packs(self, data_dir: Path) -> None:
        for table, filename, key, fields, flag in self.IMPORT_PACKS:
            if await self.get_meta(flag):
                continue
            rows = await self._load_rows(data_dir / filename, fields)
            if rows is None:
                continue  # файл тимчасово недоступний — спробуємо наступного разу
            key_index = fields.index(key)
            existing = {row[0] for row in await self._fetchall(f"SELECT {key} FROM {table}")}
            fresh = [row for row in rows if row[key_index] not in existing]
            if fresh:
                await self._insert_rows(table, fields, fresh)
            await self.set_meta(flag, "1")
            logger.info("Пакет %s: у таблицю %s додано %d нових записів", flag, table, len(fresh))

    # ------------------------------------------------------------------ користувачі
    async def upsert_user(self, user_id: int) -> bool:
        """Реєструє або повторно активує користувача. Повертає True, якщо він новий."""
        existing = await self._fetchone("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        await self.conn.execute(
            "INSERT INTO users (user_id) VALUES (?) "
            "ON CONFLICT(user_id) DO UPDATE SET is_active = 1",
            (user_id,),
        )
        await self.conn.commit()
        return existing is None

    async def set_user_active(self, user_id: int, active: bool) -> None:
        await self.conn.execute(
            "UPDATE users SET is_active = ? WHERE user_id = ?", (int(active), user_id)
        )
        await self.conn.commit()

    async def set_quiet_hours(self, user_id: int, start: int, end: int) -> None:
        await self.conn.execute(
            "UPDATE users SET quiet_start = ?, quiet_end = ? WHERE user_id = ?",
            (start, end, user_id),
        )
        await self.conn.commit()

    @staticmethod
    def _to_user(row: aiosqlite.Row) -> User:
        data = dict(row)
        data["is_active"] = bool(data["is_active"])
        data["word_of_day"] = bool(data.get("word_of_day", 1))
        return User(**{key: data[key] for key in User.__dataclass_fields__ if key in data})

    async def update_user(self, user_id: int, **fields: Any) -> None:
        """Оновлює налаштування користувача (лише дозволені поля)."""
        bad = set(fields) - EDITABLE_USER_FIELDS
        if bad or not fields:
            raise ValueError(f"Недозволені поля users: {bad or 'порожній запит'}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        await self.conn.execute(f"UPDATE users SET {assignments} WHERE user_id = ?", (*values, user_id))
        await self.conn.commit()

    async def mark_sent(self, user_id: int, subject: str, now_iso: str, today: str) -> None:
        """Фіксує, що користувачу надіслано планову картку (для чергування, інтервалу та ліміту)."""
        await self.conn.execute(
            "UPDATE users SET last_subject = ?, last_sent_at = ?, "
            "sent_today = (CASE WHEN sent_date = ? THEN sent_today ELSE 0 END) + 1, sent_date = ? "
            "WHERE user_id = ?",
            (subject, now_iso, today, today, user_id),
        )
        await self.conn.commit()

    async def mark_wod(self, user_id: int, today: str) -> None:
        await self.conn.execute("UPDATE users SET last_wod_date = ? WHERE user_id = ?", (today, user_id))
        await self.conn.commit()

    async def get_user(self, user_id: int) -> User | None:
        row = await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return self._to_user(row) if row else None

    async def get_active_users(self) -> list[User]:
        rows = await self._fetchall("SELECT * FROM users WHERE is_active = 1")
        return [self._to_user(row) for row in rows]

    async def count_users(self, only_active: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM users" + (" WHERE is_active = 1" if only_active else "")
        row = await self._fetchone(sql)
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ навчальний контент
    async def count(self, table: str) -> int:
        if table not in ALLOWED_TABLES:
            raise ValueError(f"Невідома таблиця: {table}")
        row = await self._fetchone(f"SELECT COUNT(*) FROM {table}")
        return int(row[0]) if row else 0

    async def content_counts(self) -> dict[str, int]:
        return {table: await self.count(table) for table in sorted(ALLOWED_TABLES)}

    async def random_rows(
        self, kind: str, where: str = "", params: tuple[Any, ...] = (), n: int = 1, topic_id: int | None = None
    ) -> list[Any]:
        """n випадкових записів типу kind. `where` — готовий SQL-фрагмент (лише з наших констант!)."""
        table, model = KIND_TABLES[kind]
        source = READ_SOURCES.get(table, table)
        conditions = [where] if where else []
        values = list(params)
        if topic_id is not None and table == "custom_cards":
            conditions.append("topic_id = ?")
            values.append(topic_id)
        clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = await self._fetchall(f"SELECT * FROM {source}{clause} ORDER BY RANDOM() LIMIT ?", (*values, n))
        return [model(**dict(row)) for row in rows]

    async def random_item(self, kind: str, topic_id: int | None = None) -> Any | None:
        rows = await self.random_rows(kind, n=1, topic_id=topic_id)
        return rows[0] if rows else None

    async def get_item(self, kind: str, item_id: int) -> Any | None:
        entry = KIND_TABLES.get(kind)
        if entry is None:
            return None
        table, model = entry
        row = await self._fetchone(f"SELECT * FROM {READ_SOURCES.get(table, table)} WHERE id = ?", (item_id,))
        return model(**dict(row)) if row else None

    async def add_history_date(self, year: str, event: str) -> int:
        return await self._insert("INSERT INTO history_dates (year, event) VALUES (?, ?)", (year, event))

    async def add_accent(self, word: str, note: str) -> int:
        return await self._insert("INSERT INTO accents (word, note) VALUES (?, ?)", (word, note))

    async def add_mnemonic(self, category: str, text: str) -> int:
        return await self._insert(
            "INSERT INTO mnemonics (category, text) VALUES (?, ?)", (category, text)
        )

    async def add_phrase(self, phrase: str, meaning: str) -> int:
        return await self._insert("INSERT INTO phrases (phrase, meaning) VALUES (?, ?)", (phrase, meaning))

    async def add_foreign_word(self, word: str, synonym: str) -> int:
        return await self._insert(
            "INSERT INTO foreign_words (word, synonym) VALUES (?, ?)", (word, synonym)
        )

    async def list_categories(self) -> list[str]:
        rows = await self._fetchall(
            "SELECT DISTINCT category FROM mnemonics WHERE category != '' ORDER BY category"
        )
        return [row["category"] for row in rows]

    async def update_field(self, kind: str, item_id: int, column: str, value: str) -> bool:
        """Змінює одне поле запису. Повертає True, якщо запис існував."""
        table, _ = KIND_TABLES[kind]
        if column not in EDITABLE_COLUMNS[table]:
            raise ValueError(f"Колонку {table}.{column} не можна редагувати")
        cursor = await self.conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, item_id)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def add_monument(self, title: str, photo_file_id: str, description: str) -> int:
        return await self._insert(
            "INSERT INTO monuments (title, photo_file_id, description) VALUES (?, ?, ?)",
            (title, photo_file_id, description),
        )

    async def list_items(self, kind: str, limit: int, offset: int = 0) -> list[Any]:
        """Сторінка записів певного типу, за зростанням id."""
        table, model = KIND_TABLES[kind]
        rows = await self._fetchall(
            f"SELECT * FROM {READ_SOURCES.get(table, table)} ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
        )
        return [model(**dict(row)) for row in rows]

    async def delete_item(self, kind: str, item_id: int) -> bool:
        """Видаляє запис. Повертає True, якщо запис існував."""
        table, _ = KIND_TABLES[kind]
        cursor = await self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        await self.conn.execute("DELETE FROM progress WHERE kind = ? AND item_id = ?", (kind, item_id))
        await self.conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------ прогрес (інтервальне повторення)
    BOX_DELAYS: dict[int, timedelta] = {
        1: timedelta(hours=2),    # помилка — повторити скоро
        2: timedelta(days=1),
        3: timedelta(days=3),
        4: timedelta(days=7),
        5: timedelta(days=21),
    }

    async def record_review(
        self, user_id: int, kind: str, item_id: int, ok: bool, source: str = "card", now: datetime | None = None
    ) -> int:
        """Записує відповідь («знав» / «не знав» чи квіз) і зсуває картку між «коробками». Повертає нову коробку."""
        now = now or datetime.now(timezone.utc)
        iso = now.isoformat(timespec="seconds")
        row = await self._fetchone(
            "SELECT box FROM progress WHERE user_id = ? AND kind = ? AND item_id = ?", (user_id, kind, item_id)
        )
        box = row["box"] if row else 0
        new_box = min(5, max(box, 1) + 1) if ok else 1
        due = (now + self.BOX_DELAYS[new_box]).isoformat(timespec="seconds")
        await self.conn.execute(
            "INSERT INTO progress (user_id, kind, item_id, box, due_at, seen, correct, wrong, last_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(user_id, kind, item_id) DO UPDATE SET box = excluded.box, due_at = excluded.due_at, "
            "seen = seen + 1, correct = correct + excluded.correct, wrong = wrong + excluded.wrong, "
            "last_at = excluded.last_at",
            (user_id, kind, item_id, new_box, due, int(ok), int(not ok), iso),
        )
        await self.conn.execute(
            "INSERT INTO reviews (user_id, kind, item_id, ok, source, at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, kind, item_id, int(ok), source, iso),
        )
        await self.conn.commit()
        return new_box

    async def get_progress(self, user_id: int, kind: str, item_id: int) -> aiosqlite.Row | None:
        return await self._fetchone(
            "SELECT * FROM progress WHERE user_id = ? AND kind = ? AND item_id = ?", (user_id, kind, item_id)
        )

    @staticmethod
    def _progress_filter(kinds: list[str], custom_topic_id: int | None) -> tuple[str, list[Any]]:
        placeholders = ", ".join("?" for _ in kinds)
        clause = f"kind IN ({placeholders})"
        values: list[Any] = list(kinds)
        if custom_topic_id is not None:
            clause += " AND (kind != 'q' OR item_id IN (SELECT id FROM custom_cards WHERE topic_id = ?))"
            values.append(custom_topic_id)
        return clause, values

    async def due_items(
        self, user_id: int, kinds: list[str], now_iso: str, custom_topic_id: int | None = None, limit: int = 20
    ) -> list[tuple[str, int]]:
        """Картки, які пора повторити (спершу найслабші «коробки»)."""
        clause, values = self._progress_filter(kinds, custom_topic_id)
        rows = await self._fetchall(
            f"SELECT kind, item_id FROM progress WHERE user_id = ? AND box > 0 AND due_at != '' AND due_at <= ? "
            f"AND {clause} ORDER BY box ASC, due_at ASC LIMIT ?",
            (user_id, now_iso, *values, limit),
        )
        return [(row["kind"], row["item_id"]) for row in rows]

    async def mistake_items(
        self, user_id: int, kinds: list[str], custom_topic_id: int | None = None, limit: int = 30
    ) -> list[tuple[str, int]]:
        """Картки, де остання відповідь була «не знав» (коробка 1)."""
        clause, values = self._progress_filter(kinds, custom_topic_id)
        rows = await self._fetchall(
            f"SELECT kind, item_id FROM progress WHERE user_id = ? AND box = 1 AND {clause} "
            f"ORDER BY last_at ASC LIMIT ?",
            (user_id, *values, limit),
        )
        return [(row["kind"], row["item_id"]) for row in rows]

    async def drop_progress(self, user_id: int, kind: str, item_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM progress WHERE user_id = ? AND kind = ? AND item_id = ?", (user_id, kind, item_id)
        )
        await self.conn.commit()

    async def user_stats(self, user_id: int, now_iso: str) -> dict[str, Any]:
        by_kind = {
            row["kind"]: (row["total"], row["ok"] or 0)
            for row in await self._fetchall(
                "SELECT kind, COUNT(*) AS total, SUM(ok) AS ok FROM reviews WHERE user_id = ? GROUP BY kind",
                (user_id,),
            )
        }
        quiz = await self._fetchone(
            "SELECT COUNT(*) AS total, SUM(ok) AS ok FROM reviews WHERE user_id = ? AND source = 'quiz'", (user_id,)
        )
        due = await self._fetchone(
            "SELECT COUNT(*) FROM progress WHERE user_id = ? AND box > 0 AND due_at != '' AND due_at <= ?",
            (user_id, now_iso),
        )
        mistakes = await self._fetchone("SELECT COUNT(*) FROM progress WHERE user_id = ? AND box = 1", (user_id,))
        mastered = await self._fetchone("SELECT COUNT(*) FROM progress WHERE user_id = ? AND box >= 4", (user_id,))
        stamps = [
            row["at"]
            for row in await self._fetchall(
                "SELECT at FROM reviews WHERE user_id = ? ORDER BY at DESC LIMIT 5000", (user_id,)
            )
        ]
        return {
            "by_kind": by_kind,
            "quiz": (quiz["total"] or 0, quiz["ok"] or 0),
            "due": due[0],
            "mistakes": mistakes[0],
            "mastered": mastered[0],
            "stamps": stamps,
        }

    # ------------------------------------------------------------------ квізи
    async def add_poll(
        self, poll_id: str, user_id: int, kind: str, item_id: int, correct: int, session_id: int | None = None
    ) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO polls (poll_id, user_id, kind, item_id, correct_option, session_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (poll_id, user_id, kind, item_id, correct, session_id, datetime.now(timezone.utc).isoformat()),
        )
        await self.conn.commit()

    async def pop_poll(self, poll_id: str) -> aiosqlite.Row | None:
        row = await self._fetchone("SELECT * FROM polls WHERE poll_id = ?", (poll_id,))
        if row:
            await self.conn.execute("DELETE FROM polls WHERE poll_id = ?", (poll_id,))
            await self.conn.commit()
        return row

    async def create_session(self, user_id: int, topic: str, total: int) -> int:
        return await self._insert(
            "INSERT INTO quiz_sessions (user_id, topic, total) VALUES (?, ?, ?)", (user_id, topic, total)
        )

    async def get_session(self, session_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM quiz_sessions WHERE id = ?", (session_id,))

    async def advance_session(self, session_id: int, ok: bool) -> aiosqlite.Row | None:
        await self.conn.execute(
            "UPDATE quiz_sessions SET done = done + 1, correct = correct + ?, "
            "finished = (CASE WHEN done + 1 >= total THEN 1 ELSE 0 END) WHERE id = ?",
            (int(ok), session_id),
        )
        await self.conn.commit()
        return await self.get_session(session_id)

    async def finish_session(self, session_id: int) -> None:
        await self.conn.execute("UPDATE quiz_sessions SET finished = 1 WHERE id = ?", (session_id,))
        await self.conn.commit()

    # ------------------------------------------------------------------ інші предмети (довільні завдання)
    async def custom_topics(self, only_with_cards: bool = True) -> list[aiosqlite.Row]:
        having = "HAVING cards > 0" if only_with_cards else ""
        return await self._fetchall(
            "SELECT t.id AS id, t.name AS name, COUNT(c.id) AS cards FROM custom_topics t "
            f"LEFT JOIN custom_cards c ON c.topic_id = t.id GROUP BY t.id {having} ORDER BY t.name"
        )

    async def get_custom_topic(self, topic_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM custom_topics WHERE id = ?", (topic_id,))

    async def add_custom_topic(self, name: str) -> int:
        """Створює тему (або повертає id існуючої з такою назвою без урахування регістру)."""
        for row in await self._fetchall("SELECT id, name FROM custom_topics"):
            if row["name"].lower() == name.lower():
                return row["id"]
        return await self._insert("INSERT INTO custom_topics (name) VALUES (?)", (name,))

    async def add_custom_card(self, topic_id: int, question: str, answer: str) -> int:
        return await self._insert(
            "INSERT INTO custom_cards (topic_id, question, answer) VALUES (?, ?, ?)", (topic_id, question, answer)
        )

    # ------------------------------------------------------------------ пошук
    SEARCH_COLUMNS: dict[str, tuple[str, ...]] = {
        "d": ("year", "event"),
        "a": ("word", "note"),
        "n": ("text", "category"),
        "f": ("word", "synonym"),
        "r": ("phrase", "meaning"),
        "m": ("title", "description"),
        "q": ("question", "answer", "topic"),
    }

    async def search(self, query: str, per_kind: int = 5) -> dict[str, list[Any]]:
        """Пошук підрядка (без урахування регістру й знаків наголосу) за всіма розділами."""
        needle = query.strip().lower().replace("\u0301", "")
        pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        found: dict[str, list[Any]] = {}
        for kind, columns in self.SEARCH_COLUMNS.items():
            table, model = KIND_TABLES[kind]
            source = READ_SOURCES.get(table, table)
            cond = " OR ".join(
                f"ulower(replace({col}, char(769), '')) LIKE ? ESCAPE '\\'" for col in columns
            )
            rows = await self._fetchall(
                f"SELECT * FROM {source} WHERE {cond} ORDER BY id LIMIT ?", (*([pattern] * len(columns)), per_kind)
            )
            if rows:
                found[kind] = [model(**dict(row)) for row in rows]
        return found

    # ------------------------------------------------------------------ імпорт і бекап
    async def existing_values(self, table: str, column: str) -> set[str]:
        if table not in ALLOWED_TABLES or column not in {"year", "event", "word", "note", "text", "phrase", "question", "synonym"}:
            raise ValueError("Недозволений запит")
        return {row[0] for row in await self._fetchall(f"SELECT {column} FROM {table}")}

    async def custom_keys(self) -> set[tuple[int, str]]:
        """Пари (тема, питання) вже наявних завдань — для пропуску дублікатів при імпорті."""
        return {(row["topic_id"], row["question"].casefold()) for row in await self._fetchall(
            "SELECT topic_id, question FROM custom_cards"
        )}

    async def bulk_insert(self, table: str, fields: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        """Пакетна вставка рядків (для імпорту)."""
        allowed = {"history_dates", "accents", "foreign_words", "phrases", "mnemonics", "custom_cards"}
        if table not in allowed:
            raise ValueError("Недозволена таблиця")
        await self._insert_rows(table, fields, rows)

    async def backup_to(self, target: Path) -> None:
        """Цілісна копія бази (VACUUM INTO) — безпечна, поки бот працює."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        await self.conn.commit()
        await self.conn.execute("VACUUM INTO ?", (str(target),))

    # ------------------------------------------------------------------ meta (стан планувальника)
    async def get_meta(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()
