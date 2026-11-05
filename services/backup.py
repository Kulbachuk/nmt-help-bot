"""Резервні копії бази даних: надсилання файлу адміну (вручну з панелі та автоматично щодоби)."""
from __future__ import annotations

import logging
from datetime import datetime
from datetime import tzinfo as TzInfo

from aiogram import Bot
from aiogram.types import FSInputFile

from database.db import Database

logger = logging.getLogger(__name__)


async def send_backup(bot: Bot, db: Database, chat_id: int, tz: TzInfo, caption: str = "💾 Резервна копія бази") -> bool:
    """Робить цілісну копію SQLite і надсилає її документом у Telegram. Тимчасовий файл видаляється."""
    stamp = datetime.now(tz).strftime("%Y%m%d-%H%M")
    path = db.path.parent / "backups" / f"nmt-bot-{stamp}.db"
    try:
        await db.backup_to(path)
        users = await db.count_users(only_active=True)
        await bot.send_document(
            chat_id,
            FSInputFile(path, filename=path.name),
            caption=f"{caption}\n{datetime.now(tz).strftime('%d.%m.%Y %H:%M')} · активних користувачів: {users}",
        )
        logger.info("Бекап надіслано: %s (%d байт)", path.name, path.stat().st_size)
        return True
    except Exception:
        logger.exception("Не вдалося створити або надіслати бекап")
        return False
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
