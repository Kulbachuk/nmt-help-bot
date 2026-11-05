"""Текстові звіти: статистика користувача та результати пошуку."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import tzinfo as TzInfo
from typing import Any

from services.cards import accent_variants, escape, highlight_stress, split_note_qualifier

KIND_STAT_LABELS = {
    "d": "📅 Дати",
    "m": "🏛 Пам'ятки",
    "a": "🔤 Наголоси",
    "n": "🧠 Фрази-підказки",
    "f": "🌐 Іншомовні слова",
    "r": "💬 Фразеологізми",
    "q": "📚 Інші предмети",
}
WEAK_MIN_ANSWERS = 5     # мінімум відповідей, щоб тема потрапила в «слабкі місця»
WEAK_BELOW_PERCENT = 80


def streak_days(stamps: list[str], tz: TzInfo, today: date) -> int:
    """Скільки днів поспіль (до сьогодні або вчора) були відповіді. `stamps` — UTC ISO-мітки."""
    days = set()
    for stamp in stamps:
        try:
            days.add(datetime.fromisoformat(stamp).astimezone(tz).date())
        except ValueError:
            continue
    day = today if today in days else today - timedelta(days=1)
    count = 0
    while day in days:
        count += 1
        day -= timedelta(days=1)
    return count


def _percent(ok: int, total: int) -> int:
    return round(100 * ok / total) if total else 0


def format_stats(stats: dict[str, Any], tz: TzInfo, now: datetime) -> str:
    by_kind: dict[str, tuple[int, int]] = stats["by_kind"]
    total = sum(t for t, _ in by_kind.values())
    if total == 0:
        return (
            "📊 <b>Твоя статистика</b>\n\n"
            "Поки що порожньо. Відповідай на картки кнопками «✅ Знав» / «❌ Не знав» або пройди /quiz — "
            "і тут з'явиться прогрес."
        )
    ok = sum(o for _, o in by_kind.values())
    streak = streak_days(stats["stamps"], tz, now.astimezone(tz).date())
    quiz_total, quiz_ok = stats["quiz"]

    lines = [
        "📊 <b>Твоя статистика</b>",
        "",
        f"Відповідей: <b>{total}</b> · правильних: <b>{ok}</b> ({_percent(ok, total)}%)",
        f"Серія днів поспіль: <b>{streak}</b> 🔥" if streak else "Серія днів поспіль: 0 — починай сьогодні!",
    ]
    if quiz_total:
        lines.append(f"З них квізи: {quiz_ok} з {quiz_total} ({_percent(quiz_ok, quiz_total)}%)")
    lines += ["", "<b>За темами</b>"]
    for kind, label in KIND_STAT_LABELS.items():
        if kind in by_kind:
            t, o = by_kind[kind]
            lines.append(f"{label}: {o} з {t} ({_percent(o, t)}%)")

    weak = [
        (_percent(o, t), kind) for kind, (t, o) in by_kind.items()
        if t >= WEAK_MIN_ANSWERS and _percent(o, t) < WEAK_BELOW_PERCENT
    ]
    lines += ["", "<b>Повторення</b>"]
    lines.append(f"Пора повторити: <b>{stats['due']}</b> · на виправлення (помилки): <b>{stats['mistakes']}</b> · "
                 f"добре засвоєно: <b>{stats['mastered']}</b>")
    if weak:
        percent, kind = min(weak)
        lines.append(f"Слабке місце: {KIND_STAT_LABELS.get(kind, kind)} ({percent}%) — потренуй цю тему в /now")
    if stats["mistakes"]:
        lines.append("🔁 Виправити помилки: /mistakes")
    return "\n".join(lines)


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_search(found: dict[str, list[Any]], query: str) -> str:
    """Результати пошуку: відповіді видно одразу (це довідка, а не тренування)."""
    if not found:
        return f"🔍 За запитом «{escape(query)}» нічого не знайдено."
    lines = [f"🔍 <b>Результати за «{escape(query)}»</b>"]
    for kind, items in found.items():
        lines.append("")
        for item in items:
            if kind == "d":
                lines.append(f"📅 <b>{escape(item.year)}</b> — {escape(_clip(item.event))}")
            elif kind == "a":
                qualifier, note = split_note_qualifier(item.note)
                hint = f" ({escape(qualifier)})" if qualifier else ""
                lines.append(f"🔤 <b>{escape(' і '.join(accent_variants(item.word)))}</b>{hint}")
            elif kind == "n":
                lines.append(f"🧠 {highlight_stress(_clip(item.text))}")
            elif kind == "f":
                lines.append(f"🌐 {escape(item.word)} — <b>{escape(_clip(item.synonym))}</b>")
            elif kind == "r":
                lines.append(f"💬 <b>{escape(item.phrase)}</b> — {escape(_clip(item.meaning))}")
            elif kind == "m":
                lines.append(f"🏛 <b>{escape(item.title)}</b>")
            elif kind == "q":
                lines.append(f"📚 {escape(item.topic)}: {escape(_clip(item.question, 120))} → <b>{escape(_clip(item.answer, 120))}</b>")
    text = "\n".join(lines)
    return text if len(text) <= 3900 else text[:3890] + "…"
