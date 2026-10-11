"""Выгрузка операций из таблицы transactions в Google Таблицу.

Вкладка «Выписки»            все операции с даты начала анализа (выписки, скриншоты, ручные траты), без дублей;
Вкладка «Итоги по месяцам»   доход, траты и траты по категориям по месяцам.
Вкладки каждый раз перезаписываются целиком, поэтому повторная выгрузка дублей не создаёт.
Первая вкладка таблицы (живой журнал /add_expense) не трогается.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .report import default_start

DETAIL_TAB = "Выписки"
SUMMARY_TAB = "Итоги по месяцам"

TYPE_LABEL = {
    "expense": "Расход", "refund": "Возврат", "income": "Доход", "transfer": "Перевод между своими",
    "p2p_out": "Перевод людям", "p2p_in": "Перевод от людей", "family_out": "Перевод семье",
    "family_in": "Перевод от семьи", "cash_in": "Внесение наличных", "unknown": "Не определено",
}


def source_label(source: str) -> str:
    if source.startswith("alfa_"):
        return f"Альфа ..{source[5:]}"
    if source.startswith("sber_"):
        return f"Сбер ..{source[5:]}"
    return {"screenshot": "Скриншот", "manual": "Вручную"}.get(source, source)


def build_tables(conn: sqlite3.Connection, start: str | None = None) -> tuple[list[list], list[list]]:
    start = start or default_start()
    rows = conn.execute(
        "SELECT date, type, category, merchant, amount_kop, source FROM transactions "
        "WHERE date >= ? AND type NOT IN ('duplicate', 'technical', 'split') ORDER BY date DESC, id DESC", (start,)).fetchall()

    detail: list[list] = [["Дата", "Тип", "Категория", "Описание", "Сумма, ₽", "Откуда"]]
    for d, t, cat, merchant, kop, src in rows:
        detail.append([d, TYPE_LABEL.get(t, t), cat or "", merchant or "", kop / 100.0, source_label(src)])

    months = sorted({r[0][:7] for r in rows})
    income: dict[str, float] = defaultdict(float)
    spend: dict[str, float] = defaultdict(float)
    cats: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for d, t, cat, _, kop, _ in rows:
        a, m = kop / 100.0, d[:7]
        if t == "income":
            income[m] += a
        elif t in ("expense", "refund"):
            spend[m] -= a
            cats[cat or "Прочее"][m] -= a

    summary: list[list] = [["", *months, "Итого"]]
    summary.append(["Доход", *[round(income[m], 2) for m in months], round(sum(income.values()), 2)])
    summary.append(["Траты", *[round(spend[m], 2) for m in months], round(sum(spend.values()), 2)])
    summary.append(["Доход минус траты", *[round(income[m] - spend[m], 2) for m in months],
                    round(sum(income.values()) - sum(spend.values()), 2)])
    summary.append([""] * (len(months) + 2))
    summary.append(["Траты по категориям"] + [""] * (len(months) + 1))
    for cat, per in sorted(cats.items(), key=lambda kv: -sum(kv[1].values())):
        summary.append([cat, *[round(per.get(m, 0.0), 2) for m in months], round(sum(per.values()), 2)])
    return detail, summary


def _tab(ss, title: str, rows: int, cols: int):
    try:
        return ss.worksheet(title)
    except Exception:  # вкладки ещё нет
        return ss.add_worksheet(title=title, rows=max(rows, 100), cols=max(cols, 8))


def export(conn: sqlite3.Connection, spreadsheet, start: str | None = None) -> dict:
    """Записывает обе вкладки. spreadsheet — gspread.Spreadsheet. Возвращает счётчики и ссылку."""
    detail, summary = build_tables(conn, start)
    for title, data in ((DETAIL_TAB, detail), (SUMMARY_TAB, summary)):
        ws = _tab(spreadsheet, title, len(data) + 10, len(data[0]))
        ws.clear()
        ws.update(values=data, range_name="A1", value_input_option="USER_ENTERED")
        try:
            ws.freeze(rows=1)
            ws.format("1:1", {"textFormat": {"bold": True}})
        except Exception:
            pass                                # оформление необязательно
    return {"detail_rows": len(detail) - 1, "summary_rows": len(summary) - 1, "url": getattr(spreadsheet, "url", "")}
