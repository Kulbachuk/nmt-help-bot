"""Асинхронний доступ до SQLite (aiosqlite) та первинне наповнення бази."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from database.models import (
    ALLOWED_TABLES,
    EDITABLE_COLUMNS,
    KIND_TABLES,
    SCHEMA,
    User,
)

logger = logging.getLogger(__name__)

# (таблиця, файл у data/, поля, які беремо з JSON)
SEED_SOURCES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("history_dates", "dates.json", ("year", "event")),
    ("accents", "accents.json", ("word", "note")),
    ("mnemonics", "mnemonics.json", ("text", "category")),
    ("foreign_words", "foreign_words.json", ("word", "synonym")),
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

    # Виправлення стартових наголосів, помилково записаних у ранніх версіях seed.
    # Спрацьовують лише якщо запис не чіпали (слово збігається дослівно). Ідемпотентні.
    ACCENT_FIXES: tuple[tuple[str, str, str], ...] = (
        ("гуртожИток", "гуртОжиток", "Наголос на 2-му складі."),
        ("зАвдання", "завдАння", "Наголос на 2-му складі."),
        ("шОфер", "шофЕр", "Наголос на 2-му (останньому) складі."),
    )
    ACCENT_REMOVE: tuple[str, ...] = ("дОцент",)  # наголос не підтверджено офіційним переліком

    async def _migrate(self) -> None:
        """Невеликі міграції при оновленні бота."""
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
    async def seed(self, data_dir: Path) -> None:
        """Якщо таблиця порожня — завантажує стартові дані з data/*.json."""
        for table, filename, fields in SEED_SOURCES:
            existing = await self.count(table)
            if existing:
                logger.info("Seed: таблиця %s вже містить %d записів — пропускаю", table, existing)
                continue

            path = data_dir / filename
            try:
                raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
                items = json.loads(raw)
            except FileNotFoundError:
                logger.warning("Seed: файл %s не знайдено — таблиця %s залишається порожньою", path, table)
                continue
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Seed: не вдалося прочитати %s: %s", path, exc)
                continue

            if not isinstance(items, list):
                logger.error("Seed: %s має містити JSON-масив", path)
                continue

            rows: list[tuple[str, ...]] = []
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    logger.warning("Seed: %s[%d] — не об'єкт, пропускаю", filename, index)
                    continue
                values = tuple(str(item.get(field, "")).strip() for field in fields)
                if not values[0]:
                    logger.warning("Seed: %s[%d] — порожнє поле «%s», пропускаю", filename, index, fields[0])
                    continue
                rows.append(values)

            if not rows:
                logger.warning("Seed: у %s немає коректних записів", filename)
                continue

            placeholders = ", ".join("?" for _ in fields)
            await self.conn.executemany(
                f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({placeholders})", rows
            )
            await self.conn.commit()
            logger.info("Seed: у таблицю %s додано %d записів з %s", table, len(rows), filename)

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
        return User(
            user_id=row["user_id"],
            quiet_start=row["quiet_start"],
            quiet_end=row["quiet_end"],
            is_active=bool(row["is_active"]),
        )

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

    async def random_item(self, kind: str) -> Any | None:
        table, model = KIND_TABLES[kind]
        row = await self._fetchone(f"SELECT * FROM {table} ORDER BY RANDOM() LIMIT 1")
        return model(**dict(row)) if row else None

    async def get_item(self, kind: str, item_id: int) -> Any | None:
        entry = KIND_TABLES.get(kind)
        if entry is None:
            return None
        table, model = entry
        row = await self._fetchone(f"SELECT * FROM {table} WHERE id = ?", (item_id,))
        return model(**dict(row)) if row else None

    async def add_history_date(self, year: str, event: str) -> int:
        return await self._insert("INSERT INTO history_dates (year, event) VALUES (?, ?)", (year, event))

    async def add_accent(self, word: str, note: str) -> int:
        return await self._insert("INSERT INTO accents (word, note) VALUES (?, ?)", (word, note))

    async def add_mnemonic(self, category: str, text: str) -> int:
        return await self._insert(
            "INSERT INTO mnemonics (category, text) VALUES (?, ?)", (category, text)
        )

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
            f"SELECT * FROM {table} ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
        )
        return [model(**dict(row)) for row in rows]

    async def delete_item(self, kind: str, item_id: int) -> bool:
        """Видаляє запис. Повертає True, якщо запис існував."""
        table, _ = KIND_TABLES[kind]
        cursor = await self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        await self.conn.commit()
        return cursor.rowcount > 0

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
