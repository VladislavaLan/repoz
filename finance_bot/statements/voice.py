"""Голос → разметка категорий у операций со скриншота (через Gemini, ответ — JSON)."""
from __future__ import annotations

import base64
import json
import re

from . import insights

PROMPT = (
    "Ты помогаешь разметить траты по категориям. Человек прислал голосовое сообщение по-русски. "
    "Ниже пронумерованный список операций и допустимые категории. Прослушай аудио и верни ТОЛЬКО JSON вида "
    '{{"transcript": "дословная расшифровка", "assign": {{"1": "Продукты", "3": "Кафе и рестораны"}}, '
    '"remove": [2], "unclear": "что не удалось понять или пустая строка"}}.\n'
    "Правила: номера строго из списка; категория строго из допустимых (слова вроде «еда» или «магазин» "
    "сопоставь с ближайшей допустимой); «остальное»/«все остальные» относится ко всем строкам, которым категория "
    "не названа явно; «убери»/«не считай»/«это не трата» — номер в remove; если не уверена, строку не трогай "
    "и опиши в unclear. Если человек не знает категорию или просит подсказать, предложи самую подходящую "
    "допустимую в suggest (без assign). Не выдумывай того, чего человек не говорил.\n\n"
    "Операции:\n{items}\n\nДопустимые категории: {cats}"
)


def parse_answer(text: str, n: int, categories: list[str]) -> dict:
    """Разбирает ответ модели, оставляя только корректные номера и категории."""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    by_lower = {c.lower(): c for c in categories}
    assign, unknown = {}, []
    for k, v in (data.get("assign") or {}).items():
        try:
            i = int(k) - 1
        except (TypeError, ValueError):
            continue
        cat = by_lower.get(str(v).strip().lower())
        if 0 <= i < n and cat:
            assign[i] = cat
        elif 0 <= i < n:
            unknown.append(f"{i + 1}: «{v}»")
    suggest = {}
    for k, v in (data.get("suggest") or {}).items():
        try:
            i = int(k) - 1
        except (TypeError, ValueError):
            continue
        cat = by_lower.get(str(v).strip().lower())
        if 0 <= i < n and cat and i not in assign:
            suggest[i] = cat
    remove = sorted({int(x) - 1 for x in (data.get("remove") or []) if str(x).isdigit() and 1 <= int(x) <= n})
    return {"transcript": str(data.get("transcript") or "").strip(), "assign": assign, "remove": remove,
            "unclear": str(data.get("unclear") or "").strip(), "unknown": unknown, "suggest": suggest}


def understand(audio: bytes, mime: str, items: list[str], categories: list[str]) -> dict:
    """items — строки «1. Магазин — 450 ₽». Бросает RuntimeError при проблемах с Gemini."""
    prompt = PROMPT.format(items="\n".join(items), cats=", ".join(categories))
    parts = [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode()}}]
    answer = insights.ask_gemini(parts, json_mode=True)
    return parse_answer(answer, len(items), categories)
