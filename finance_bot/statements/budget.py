"""Бюджеты по категориям на месяц, статус «где в лимите, где перебор» и «сколько можно потратить сегодня»."""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date

from . import config
from .week import compute

SCHEMA = "CREATE TABLE IF NOT EXISTS budgets (category TEXT PRIMARY KEY, limit_rub REAL NOT NULL)"


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    conn.commit()


def set_limit(conn: sqlite3.Connection, category: str, limit: float) -> None:
    ensure(conn)
    conn.execute("INSERT INTO budgets (category, limit_rub) VALUES (?, ?) "
                 "ON CONFLICT(category) DO UPDATE SET limit_rub = excluded.limit_rub", (category, limit))
    conn.commit()


def delete_limit(conn: sqlite3.Connection, category: str) -> bool:
    ensure(conn)
    cur = conn.execute("DELETE FROM budgets WHERE category = ?", (category,))
    conn.commit()
    return cur.rowcount > 0


def list_limits(conn: sqlite3.Connection) -> dict[str, float]:
    ensure(conn)
    return {c: float(v) for c, v in conn.execute("SELECT category, limit_rub FROM budgets ORDER BY limit_rub DESC")}


def _data(conn: sqlite3.Connection, today: date):
    return compute(conn, today, int(config.load().get("big_purchase", 20000)))


def auto(conn: sqlite3.Connection, today: date | None = None) -> dict[str, float] | None:
    """Ставит лимиты по «обычной неделе» (медиана прошлых недель × 30,4/7, округление до 500 ₽).
    None, если истории мало для нормы."""
    today = today or date.today()
    data = _data(conn, today)
    if not data or not data["usual"]:
        return None
    out = {}
    for cat, weekly in data["usual"].items():
        monthly = round(weekly * 30.4 / 7 / 500) * 500
        if monthly >= 1000:
            out[cat] = float(monthly)
            set_limit(conn, cat, float(monthly))
    return out


def _icon(spent: float, limit: float, day: int, days_in_month: int) -> str:
    if spent > limit:
        return "🔴"
    pace = limit * day / days_in_month
    return "🟡" if spent >= 0.8 * limit or spent > pace + 0.15 * limit else "🟢"


def status(conn: sqlite3.Connection, today: date | None = None) -> str:
    today = today or date.today()
    limits = list_limits(conn)
    if not limits:
        return ("Лимитов пока нет.\nЗадать: <code>/budget Продукты 25000</code> (в месяц). "
                "Убрать: <code>/budget Продукты -</code>.\n"
                "Или поставить по обычным тратам сразу: <code>/budget авто</code>.")
    data = _data(conn, today)
    month = data["month"] if data else {}
    dim = calendar.monthrange(today.year, today.month)[1]
    left = dim - today.day + 1
    lines = [f"<b>Бюджеты на {today:%m.%Y}</b> (осталось дней: {left})", ""]
    rows = sorted(limits.items(), key=lambda kv: -(month.get(kv[0], 0) / kv[1]))
    for cat, lim in rows:
        spent = month.get(cat, 0.0)
        rest = lim - spent
        tail = f"осталось {_rub(rest)}" if rest >= 0 else f"перебор {_rub(-rest)}"
        lines.append(f"{_icon(spent, lim, today.day, dim)} {cat}: {_rub(spent)} из {_rub(lim)} "
                     f"({spent / lim * 100:.0f}%), {tail}")
    lines += ["", "🟢 в лимите · 🟡 ≥80% или опережаете график · 🔴 лимит превышен",
              "Считаются обычные траты: крупные от 20 000 ₽ и плановые в бюджет не входят."]
    return "\n".join(lines)


def today_text(conn: sqlite3.Connection, today: date | None = None) -> str:
    today = today or date.today()
    limits = list_limits(conn)
    if not limits:
        return "Сначала задайте лимиты: <code>/budget авто</code> или <code>/budget Продукты 25000</code>."
    data = _data(conn, today)
    month = data["month"] if data else {}
    day = data["day"] if data else {}
    dim = calendar.monthrange(today.year, today.month)[1]
    left = dim - today.day + 1
    lines = [f"<b>Сколько можно потратить сегодня</b> ({today:%d.%m}, дней до конца месяца: {left})", ""]
    total_daily = total_spent_today = 0.0
    parts = []
    for cat, lim in limits.items():
        spent = month.get(cat, 0.0)
        today_spent = day.get(cat, 0.0)
        before = max(lim - (spent - today_spent), 0.0)       # остаток на начало дня
        daily = before / left
        total_daily += daily
        total_spent_today += today_spent
        parts.append((cat, daily, today_spent, lim - spent))
    lines.append(f"Всего по лимитам: {_rub(total_daily)} в день, сегодня потрачено {_rub(total_spent_today)}, "
                 f"осталось на сегодня {_rub(max(total_daily - total_spent_today, 0))}.")
    lines.append("")
    for cat, daily, ts, rest in sorted(parts, key=lambda x: -x[1]):
        mark = "🔴" if rest < 0 else ("🟡" if ts > daily else "🟢")
        lines.append(f"{mark} {cat}: на день {_rub(daily)}, сегодня {_rub(ts)}"
                     + (f", перебор за месяц {_rub(-rest)}" if rest < 0 else ""))
    lines += ["", "Продукты обычно берут одним походом, так что смотрите на месяц и неделю, а не на один день."]
    return "\n".join(lines)


def warnings(conn: sqlite3.Connection, today: date | None = None) -> list[str]:
    """Короткие предупреждения для сообщения после новой траты: категории ≥80% лимита."""
    today = today or date.today()
    limits = list_limits(conn)
    if not limits:
        return []
    data = _data(conn, today)
    month = data["month"] if data else {}
    dim = calendar.monthrange(today.year, today.month)[1]
    out = []
    for cat, lim in limits.items():
        spent = month.get(cat, 0.0)
        if spent > lim:
            out.append(f"🔴 «{cat}»: лимит превышен на {_rub(spent - lim)}")
        elif spent >= 0.8 * lim:
            out.append(f"🟡 «{cat}»: потрачено {spent / lim * 100:.0f}% лимита, осталось {_rub(lim - spent)} "
                       f"на {dim - today.day + 1} дн.")
    return out
