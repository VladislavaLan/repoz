"""/week: траты за последние 7 дней по категориям в сравнении с «обычной неделей» (медиана прошлых недель)."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from . import config
from .normalize import IGNORE
from .report import planned_set

WEEKS_BACK = 8
MIN_WEEKS = 3


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def _icon(cur: float, usual: float | None) -> str:
    if usual is None:
        return "⚪"
    diff = cur - usual
    if diff <= 500 or cur <= usual * 1.15:
        return "🟢"
    return "🟡" if cur <= usual * 1.4 else "🔴"


def build(conn: sqlite3.Connection, today: date | None = None, big_limit: int | None = None) -> str | None:
    today = today or date.today()
    big_limit = big_limit or int(config.load().get("big_purchase", 20000))
    first = conn.execute("SELECT MIN(date) FROM transactions WHERE type IN ('expense','refund')").fetchone()[0]
    if not first:
        return None
    first_day = date.fromisoformat(first)
    planned = planned_set(conn)
    since = today - timedelta(days=7 * (WEEKS_BACK + 1) - 1)
    rows = conn.execute(
        "SELECT date, type, amount_kop, category, fingerprint FROM transactions "
        "WHERE type IN ('expense','refund') AND date >= ? AND date <= ?",
        (since.isoformat(), today.isoformat())).fetchall()

    week = defaultdict(lambda: defaultdict(float))   # номер окна -> категория -> сумма
    big_now: list[tuple[str, float]] = []
    month_start = today.replace(day=1).isoformat()
    month = defaultdict(float)
    for d, t, a, cat, fp in rows:
        cat = cat or "Прочее"
        if cat == IGNORE:
            continue
        amount = -a / 100.0
        if t == "expense" and (amount >= big_limit or fp in planned):
            if (today - date.fromisoformat(d)).days < 7:
                big_now.append((cat, amount))
            continue
        k = (today - date.fromisoformat(d)).days // 7
        week[k][cat] += amount
        if d >= month_start:
            month[cat] += amount

    # окна, целиком попадающие в период, за который есть данные
    past = [k for k in range(1, WEEKS_BACK + 1) if today - timedelta(days=7 * k + 6) >= first_day]
    cats = set(week[0]) | {c for k in past for c in week[k]}
    usual = {c: median([week[k].get(c, 0.0) for k in past]) for c in cats} if len(past) >= MIN_WEEKS else {}
    total_now = sum(week[0].values())
    totals_past = [sum(week[k].values()) for k in past]
    total_usual = median(totals_past) if len(past) >= MIN_WEEKS else None

    start = today - timedelta(days=6)
    lines = [f"<b>Траты за 7 дней</b> ({start:%d.%m}–{today:%d.%m})", ""]
    head = f"Всего: {_rub(total_now)}"
    if total_usual is not None:
        diff = total_now - total_usual
        head += f" · обычно {_rub(total_usual)} · {'+' if diff >= 0 else '−'}{_rub(abs(diff))}"
    lines.append(head)
    lines.append("")
    shown = sorted((c for c in cats if week[0].get(c, 0) > 0), key=lambda c: -week[0][c])
    for c in shown:
        cur = week[0][c]
        u = usual.get(c) if usual else None
        tail = f" (обычно {_rub(u)}, {'+' if cur >= u else '−'}{_rub(abs(cur - u))})" if u is not None else ""
        lines.append(f"{_icon(cur, u)} {c}: {_rub(cur)}{tail}")
    if not shown:
        lines.append("За последние 7 дней трат нет.")
    gone = sorted((c for c in cats if usual.get(c, 0) >= 1000 and week[0].get(c, 0) == 0), key=lambda c: -usual[c])[:3]
    if gone:
        lines += ["", "Обычно были траты, на этой неделе нет: " + ", ".join(f"{c} ({_rub(usual[c])})" for c in gone)]
    if big_now:
        lines += ["", f"Не входят в расчёт (крупные от {_rub(big_limit)} и плановые): "
                  f"{_rub(sum(a for _, a in big_now))}, {len(big_now)} шт."]
    if month:
        top = sorted(month.items(), key=lambda kv: -kv[1])[:5]
        lines += ["", "<b>С начала месяца:</b> " + ", ".join(f"{c} {_rub(v)}" for c, v in top)]
    lines += ["", f"🟢 в норме · 🟡 выше обычного · 🔴 заметно выше · ⚪ нет нормы"]
    if len(past) < MIN_WEEKS:
        lines.append(f"Норма пока не считается: для неё нужно минимум {MIN_WEEKS} полных недели истории, сейчас {len(past)}.")
    else:
        lines.append(f"«Обычно» = медиана за {len(past)} прошлых недель, без крупных и плановых трат. "
                     "Отпуск и разовые покупки лучше пометить в /recat как плановые.")
    return "\n".join(lines)
