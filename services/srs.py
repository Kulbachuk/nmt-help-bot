"""Вибір карток для конкретного користувача з урахуванням його прогресу (інтервальне повторення)."""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

from database.db import Database
from services.cards import (
    TOPIC_MISTAKES,
    Card,
    build_card,
    build_card_by_id,
    split_topic,
    topic_kinds,
)

logger = logging.getLogger(__name__)

REVIEW_CHANCE = 0.5   # яка частка карток — повторення того, що пора повторити
MASTERED_BOX = 4      # з цієї «коробки» картка вважається засвоєною й показується рідше
RETRIES = 5           # спроб підібрати не засвоєну картку


async def _first_existing(db: Database, user_id: int, pairs: list[tuple[str, int]]) -> Card | None:
    """Перша картка зі списку, що ще є в базі (видалені прибираємо з прогресу)."""
    for kind, item_id in pairs:
        card = await build_card_by_id(db, kind, item_id)
        if card is not None:
            return card
        await db.drop_progress(user_id, kind, item_id)
    return None


async def pick_card(
    db: Database, user_id: int, topic: str, enabled: set[str] | None = None, now: datetime | None = None
) -> Card | None:
    """Картка для користувача: або повторення (те, що пора повторити), або нова/випадкова.

    topic == «err» — лише помилки (картки, на які остання відповідь була «не знав»).
    """
    kinds = topic_kinds(topic, enabled)
    if not kinds:
        return None
    _, custom_topic = split_topic(topic)
    iso = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    if topic == TOPIC_MISTAKES:
        pairs = (await db.mistake_items(user_id, kinds, custom_topic))[:10]
        random.shuffle(pairs)
        return await _first_existing(db, user_id, pairs)

    if random.random() < REVIEW_CHANCE:
        due = (await db.due_items(user_id, kinds, iso, custom_topic, limit=10))[:3]  # найслабші «коробки»
        random.shuffle(due)
        card = await _first_existing(db, user_id, due)
        if card is not None:
            return card

    fallback: Card | None = None
    for _ in range(RETRIES):
        card = await build_card(db, topic, enabled)
        if card is None:
            return None
        progress = await db.get_progress(user_id, card.kind, card.item_id)
        if progress is None or progress["box"] < MASTERED_BOX or progress["due_at"] <= iso:
            return card
        fallback = fallback or card  # усі випадкові виявилися засвоєними — не лишаємо без картки
    return fallback
