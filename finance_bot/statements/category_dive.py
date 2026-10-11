"""Разбор одной категории: помесячно сумма, число покупок, средний чек; рост из-за частоты или чека."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from statistics import median


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def build(conn: sqlite3.Connection, category: str, start: str, end: str | None = None) -> str | None:
    """start/end — ISO-даты включительно. None, если трат в категории нет."""
    sql = ("SELECT substr(date,1,7), merchant, -amount_kop/100.0 FROM transactions "
           "WHERE type='expense' AND category=? AND date>=?")
    args: list = [category, start]
    if end:
        sql += " AND date<=?"
        args.append(end)
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return None
    by_month: dict[str, list[float]] = defaultdict(list)
    by_shop: dict[str, list[float]] = defaultdict(list)
    for month, merchant, amount in rows:
        by_month[month].append(amount)
        by_shop[(merchant or "без названия").strip()[:30]].append(amount)

    lines = [f"<b>Разбор категории «{category}»</b>", "", "<b>По месяцам</b> (сумма · покупок · средний чек · медиана):"]
    stats = {}
    for m in sorted(by_month):
        v = by_month[m]
        stats[m] = (sum(v), len(v), sum(v) / len(v))
        lines.append(f"• {m}: {_rub(sum(v))} · {len(v)} · {_rub(sum(v) / len(v))} · {_rub(median(v))}")

    months = sorted(stats)
    if len(months) >= 2:
        a, b = months[-2], months[-1]
        (s1, n1, c1), (s2, n2, c2) = stats[a], stats[b]
        d_freq = (n2 - n1) * c1       # сколько добавили покупки-количество
        d_check = n2 * (c2 - c1)      # сколько добавил рост среднего чека
        lines += ["", f"<b>Почему {b} отличается от {a}</b>: {'+' if s2 - s1 >= 0 else '−'}{_rub(abs(s2 - s1))}",
                  f"• из-за числа покупок ({n1} → {n2}): {d_freq:+,.0f} ₽".replace(",", " "),
                  f"• из-за среднего чека ({_rub(c1)} → {_rub(c2)}): {d_check:+,.0f} ₽".replace(",", " ")]
        if abs(d_check) > abs(d_freq):
            lines.append("Больше влияет размер чека: похоже на рост цен или более дорогие покупки.")
        else:
            lines.append("Больше влияет число покупок: это скорее привычки, чем цены.")

    lines += ["", "<b>Где тратится больше всего</b> (сумма · покупок · средний чек):"]
    top = sorted(by_shop.items(), key=lambda kv: -sum(kv[1]))[:7]
    lines += [f"• {name}: {_rub(sum(v))} · {len(v)} · {_rub(sum(v) / len(v))}" for name, v in top]
    return "\n".join(lines)
