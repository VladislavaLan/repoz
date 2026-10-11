"""Компактная сводка для обсуждения финансов без личных данных (без названий магазинов и номеров карт)."""

from __future__ import annotations

import sqlite3

from .report import build_report, default_start, rub


def interest_by_source(conn: sqlite3.Connection, start: str, months: int = 3) -> dict[str, float]:
    """Средние проценты и комиссии за месяц по источнику (карте) за последние месяцы."""
    rows = conn.execute(
        "SELECT source, substr(date,1,7), -SUM(amount_kop)/100.0 FROM transactions "
        "WHERE date >= ? AND subtype = 'interest' AND type IN ('expense','refund') GROUP BY 1, 2", (start,)
    ).fetchall()
    per: dict[str, list[float]] = {}
    for src, mon, v in sorted(rows, key=lambda r: r[1]):
        per.setdefault(src, []).append(v)
    return {s: sum(v[-months:]) / len(v[-months:]) for s, v in per.items() if v}


def build(conn: sqlite3.Connection, start: str | None = None) -> str | None:
    start = start or default_start()
    r = build_report(conn, start)
    if not r:
        return None
    lines = ["📋 <b>Сводка (без личных данных)</b>", f"С {start}", "", "<b>Месяцы</b> (доход / траты / траты без крупных), ₽:"]
    big_by_m: dict[str, float] = {}
    for d, a, _c, _m in r["big"]:
        big_by_m[d[:7]] = big_by_m.get(d[:7], 0.0) + a
    for m in r["months"]:
        v = r["by_m"][m]
        lines.append(f"{m}: {v['income']:.0f} / {v['spend']:.0f} / {v['spend'] - big_by_m.get(m, 0.0):.0f}")
    if r["full_months"]:
        lines += ["", f"В среднем за полный месяц: доход {rub(r['avg']['income'])}, траты {rub(r['avg']['spend'])}, "
                      f"без крупных {rub(r['avg_spend_no_big'])}"]
    lines += ["", "<b>Категории: в среднем за месяц без крупных и плановых</b>"]
    for c in r["cats"][:12]:
        if c["per_month_no_big"] is not None:
            lines.append(f"• {c['category']}: {rub(c['per_month_no_big'])}")
    lines += ["", f"Проценты по кредиткам за период: {rub(r['interest'])}, комиссии: {rub(r['fees'])}"]
    ibs = interest_by_source(conn, start)
    for src, v in ibs.items():
        lines.append(f"• проценты в месяц ({src}): ≈{rub(v)}")
    lines += ["", f"Крупные и плановые траты: {len(r['big'])} шт. на {rub(r['big_sum'])}"]
    return "\n".join(lines)
