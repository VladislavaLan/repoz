"""Комментарии и рекомендации по тратам.

Два уровня:
  1. rule_comments — по правилам, без нейросети (бесплатно, данные никуда не уходят);
  2. ai_summary + ai_advice — нейросеть пишет текст по СВОДКЕ. В сводке только категории и суммы:
     без названий магазинов, имён, телефонов и отдельных операций.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import urllib.error
import urllib.request
from collections import defaultdict

from .report import build_report, planned_set, rub

SUBSCRIPTION_CATS = {"Связь и подписки", "Дом и ЖКХ"}
DEFAULT_MODEL = "gemini-2.5-flash"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def build_insights(conn: sqlite3.Connection, start: str | None = None) -> dict | None:
    rep = build_report(conn, start)
    if not rep or not rep["full_months"]:
        return None
    full = rep["full_months"]
    rows = conn.execute(
        "SELECT date, category, merchant, amount_kop, subtype, type, fingerprint FROM transactions "
        "WHERE date >= ? AND type IN ('expense','refund')",
        (rep["start"],),
    ).fetchall()
    cat_m: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    merch_m: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    cats_of = {m: c for _, c, m, _, _, _, _ in rows}
    planned = planned_set(conn)
    for d, cat, merchant, kop, sub, typ, fp in rows:
        a = -kop / 100.0
        if typ == "expense" and (a >= rep["big_limit"] or fp in planned):
            continue                    # крупные разовые покупки сравниваем отдельно, иначе они забивают всё
        cat_m[cat or "Прочее"][d[:7]] += a
        if merchant and a > 0 and sub != "interest":
            merch_m[merchant][d[:7]].append(a)

    # регулярные списания: один получатель минимум в 3 месяцах и сумма почти одна и та же
    recurring = []
    for merchant, per in merch_m.items():
        if len(per) < 3:
            continue
        amounts = [sum(v) / len(v) for v in per.values()]
        stable = statistics.mean(amounts) > 0 and statistics.pstdev(amounts) / statistics.mean(amounts) < 0.15
        if stable and statistics.mean(amounts) >= 200 and (cats_of.get(merchant) in SUBSCRIPTION_CATS or statistics.pstdev(amounts) == 0):
            recurring.append((merchant, statistics.mean(amounts)))
    recurring.sort(key=lambda x: -x[1])

    last, prev = full[-1], (full[-2] if len(full) > 1 else None)
    changes = []
    if prev:
        for cat, per in cat_m.items():
            a, b = per.get(last, 0.0), per.get(prev, 0.0)
            if max(a, b) >= 1000:
                changes.append((cat, b, a, a - b))
        changes.sort(key=lambda x: -abs(x[3]))
    return {
        "report": rep, "last": last, "prev": prev, "changes": changes, "recurring": recurring,
        "cat_m": {c: dict(v) for c, v in cat_m.items()},
    }


def rule_comments(ins: dict) -> list[str]:
    rep, last, prev = ins["report"], ins["last"], ins["prev"]
    out: list[str] = []
    income, spend = rep["by_m"][last]["income"], rep["by_m"][last]["spend"]
    if income > 0:
        saved = income - spend
        share = spend / income * 100
        big_m = sum(a for d, a, _, _ in rep["big"] if d[:7] == last)
        extra = f", из них крупные разовые покупки {rub(big_m)}" if big_m else ""
        out.append(f"{last}: доход {rub(income)}, траты {rub(spend)}{extra} ({share:.0f}% дохода). "
                   + ("Остаётся " + rub(saved) + "." if saved >= 0 else f"Ушли в минус на {rub(-saved)}."))
    if prev:
        a, b = spend, rep["by_m"][prev]["spend"]
        if b:
            out.append(f"Траты к {prev}: {(a - b) / b * 100:+.0f}% ({rub(b)} → {rub(a)}).")
        out.append("Сравнение по категориям ниже — без крупных разовых покупок:")
        for cat, before, now, diff in ins["changes"][:4]:
            word = "выросли" if diff > 0 else "снизились"
            out.append(f"«{cat}» {word} на {rub(abs(diff))} ({rub(before)} → {rub(now)}).")
    if rep["interest"] > 0:
        out.append(f"Проценты по кредиткам за период: {rub(rep['interest'])} — это деньги, которые ничего не купили. "
                   "Самый дешёвый способ «сэкономить» — гасить кредитки раньше.")
    if ins["recurring"]:
        tot = sum(a for _, a in ins["recurring"])
        names = ", ".join(f"{m} ({rub(a)})" for m, a in ins["recurring"][:5])
        out.append(f"Регулярные списания (≈{rub(tot)}/мес): {names}. Проверьте, всем ли вы пользуетесь.")
    if rep["big"]:
        out.append(f"Крупных разовых покупок: {len(rep['big'])} на {rub(rep['big_sum'])} — "
                   "они искажают «средний месяц», смотрите траты без них.")
    return out


def ai_summary(ins: dict) -> dict:
    """Анонимная сводка для нейросети: только категории и суммы по месяцам."""
    rep = ins["report"]
    months = rep["full_months"][-6:]
    return {
        "currency": "RUB",
        "months": months,
        "per_month": {m: {"income": round(rep["by_m"][m]["income"]), "spend": round(rep["by_m"][m]["spend"]),
                         "spend_without_big_purchases": round(rep["by_m"][m]["spend"] - sum(
                             a for d, a, _, _ in rep["big"] if d[:7] == m))}
                      for m in months},
        "categories_by_month": {c: {m: round(v.get(m, 0)) for m in months} for c, v in ins["cat_m"].items()
                                if sum(v.get(m, 0) for m in months) >= 1000},
        "interest_on_credit_cards_total": round(rep["interest"]),
        "recurring_monthly_total": round(sum(a for _, a in ins["recurring"])),
        "recurring_count": len(ins["recurring"]),
        "big_purchases_count": len(rep["big"]),
        "big_purchases_total": round(rep["big_sum"]),
    }


PROMPT = (
    "Ты — спокойный финансовый консультант. Ниже JSON с агрегированными тратами человека по месяцам (рубли). "
    "Человек хочет уменьшить траты. Напиши по-русски коротко (до 1500 знаков): 1) что бросается в глаза, "
    "2) 3–4 конкретные рекомендации с примерной суммой экономии в месяц, 3) один вопрос для размышления. "
    "Не выдумывай факты, которых нет в данных, не осуждай. Без Markdown-таблиц.\n\nДанные:\n"
)


MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=100"


def _post(model: str, api_key: str, body: bytes, timeout: int) -> dict:
    req = urllib.request.Request(
        GEMINI_URL.format(model=model), data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def pick_available_model(api_key: str, timeout: int = 30) -> str | None:
    """Спрашивает у Gemini список моделей и выбирает быструю (flash) с generateContent."""
    req = urllib.request.Request(MODELS_URL, headers={"x-goog-api-key": api_key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        models = json.load(resp).get("models", [])
    names = [
        m["name"].split("/", 1)[-1] for m in models
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    flash = sorted(n for n in names if "flash" in n and "lite" not in n and "preview" not in n
                   and "image" not in n and "tts" not in n and "live" not in n and "thinking" not in n)
    return (flash or names or [None])[-1]


def ai_advice(summary: dict, api_key: str | None = None, model: str | None = None, timeout: int = 60) -> str:
    """Запрос к Gemini API. Бросает RuntimeError с понятным текстом."""
    api_key = api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Не задан GEMINI_API_KEY в .env (ключ берут в Google AI Studio).")
    model = model or os.getenv("GEMINI_MODEL") or DEFAULT_MODEL
    body = json.dumps({"contents": [{"parts": [{"text": PROMPT + json.dumps(summary, ensure_ascii=False)}]}]}).encode()
    try:
        try:
            data = _post(model, api_key, body, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            # Модель переименована или снята с выдачи: подбираем доступную.
            alt = pick_available_model(api_key)
            if not alt or alt == model:
                raise
            model = alt
            data = _post(model, api_key, body, timeout)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        if e.code in (400, 403) and "location" in text.lower():
            raise RuntimeError("Gemini недоступен из вашего региона (включите VPN для Python/бота).") from e
        raise RuntimeError(f"Gemini вернул ошибку {e.code} (модель {model}): {text[:200]}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"Не удалось связаться с Gemini: {e}. Если вы в России — нужен VPN.") from e
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        raise RuntimeError("Gemini вернул пустой ответ.") from None
