"""Массовая правка категорий через Google Таблицу.

/edit_sheet  выгружает операции за период на вкладку «Правка категорий» (с выпадающим списком категорий);
вы правите столбцы «Новая категория», «Плановая», «Разбить» (можно протягивать вниз, фильтровать, сортировать);
/apply_sheet читает вкладку и применяет правки к базе. Правки хранятся как и правки из бота (переживают повторную загрузку выписок).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import config
from . import normalize as nz
from . import recat

TAB = "Правка категорий"
HEADER = ["Дата", "Магазин", "Сумма, ₽", "Категория сейчас", "Новая категория", "Плановая", "Разбить", "Результат", "ID (не менять)"]
COL_NEW, COL_PLAN, COL_SPLIT, COL_RESULT, COL_ID = 4, 5, 6, 7, 8        # номера столбцов с нуля
HELP = [
    "Как пользоваться:",
    "1. В столбце «Новая категория» выберите категорию из списка (или оставьте пустым).",
    "2. «Плановая»: «да» — крупная плановая трата, не искажает средние; пусто — обычная.",
    "3. «Разбить»: например «2000 Красота, 3000 Подарки» (последняя сумма может быть без числа — остаток).",
    "4. Когда закончите, напишите боту /apply_sheet. Столбец «Результат» покажет, что применилось.",
    "5. Потом /sheet обновит вкладки «Выписки» и «Итоги по месяцам».",
]
YES = {"да", "yes", "y", "true", "1", "+", "✓", "✔", "плановая", "истина"}


def default_start(today: date | None = None) -> str:
    """Начало прошлого месяца: обычно именно его и хочется поправить."""
    today = today or date.today()
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return date(y, m, 1).isoformat()


def build_rows(conn: sqlite3.Connection, start: str) -> list[list]:
    ops = recat.find_ops(conn, "", start, limit=20000)
    rows = [HEADER]
    for o in ops:
        rows.append([o["date"], o["merchant"], round(o["kop"] / 100.0, 2), o["category"], "",
                     "да" if o["planned"] else "", "", "", o["fingerprint"]])
    return rows


def _decorate(ws, spreadsheet, n_rows: int, cats: list[str]) -> None:
    """Выпадающие списки, скрытый столбец ID, оформление. Необязательно: ошибки не мешают."""
    try:
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
        sid = ws.id
        def dv(col: int, values: list[str]) -> dict:
            return {"setDataValidation": {
                "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": max(n_rows, 2), "startColumnIndex": col, "endColumnIndex": col + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]},
                         "showCustomUi": True, "strict": False}}}
        spreadsheet.batch_update({"requests": [
            dv(COL_NEW, cats), dv(COL_PLAN, ["да", "нет"]),
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": COL_ID, "endIndex": COL_ID + 1},
                                           "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                                           "properties": {"pixelSize": 260}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": COL_NEW, "endIndex": COL_NEW + 1},
                                           "properties": {"pixelSize": 180}, "fields": "pixelSize"}},
        ]})
    except Exception:
        pass


def export(conn: sqlite3.Connection, spreadsheet, start: str | None = None) -> dict:
    start = start or default_start()
    rows = build_rows(conn, start)
    try:
        ws = spreadsheet.worksheet(TAB)
    except Exception:
        ws = spreadsheet.add_worksheet(title=TAB, rows=max(len(rows) + 20, 100), cols=12)
    ws.clear()
    ws.update(values=rows, range_name="A1", value_input_option="USER_ENTERED")
    ws.update(values=[[t] for t in HELP], range_name="K1", value_input_option="USER_ENTERED")
    _decorate(ws, spreadsheet, len(rows), nz.all_categories())
    return {"rows": len(rows) - 1, "start": start, "url": getattr(spreadsheet, "url", "")}


def _resolve(raw: str, cats: list[str], aliases: dict[str, str]) -> str | None:
    key = raw.strip().casefold()
    names = {c.casefold(): c for c in cats}
    names.update({k.casefold(): v for k, v in aliases.items()})
    if key in names:
        return names[key]
    starts = {v for k, v in names.items() if k.startswith(key)}
    return next(iter(starts)) if len(starts) == 1 and len(key) >= 3 else None


def apply(conn: sqlite3.Connection, spreadsheet) -> dict:
    """Применяет правки с вкладки к базе, пишет результат по каждой строке в столбец «Результат»."""
    ws = spreadsheet.worksheet(TAB)
    values = ws.get_all_values()
    cats = nz.all_categories()
    aliases = config.load().get("category_aliases", {})
    stats = {"category": 0, "planned": 0, "split": 0, "errors": 0, "rows": 0}
    status: list[str] = []
    by_cat: dict[str, list[str]] = {}
    for row in values[1:]:
        row = list(row) + [""] * (len(HEADER) - len(row))
        fp = row[COL_ID].strip()
        msg = []
        if not fp:
            status.append("")
            continue
        stats["rows"] += 1
        cur = conn.execute("SELECT type, category, amount_kop FROM transactions WHERE fingerprint = ?", (fp,)).fetchone()
        if not cur:
            status.append("⚠ операции нет в базе")
            stats["errors"] += 1
            continue
        prow = conn.execute("SELECT planned FROM tx_overrides WHERE fingerprint = ?", (fp,)).fetchone()
        planned_now = bool(prow and prow[0])
        want_planned = row[COL_PLAN].strip().casefold() in YES
        split_text, new_raw = row[COL_SPLIT].strip(), row[COL_NEW].strip()
        try:
            if want_planned != planned_now:
                recat.set_planned(conn, [fp], want_planned)
                stats["planned"] += 1
                msg.append("✓ плановая" if want_planned else "✓ не плановая")
            if split_text:
                if cur[0] not in ("expense", "split"):
                    raise ValueError("разбить можно только трату")
                parts = recat.parse_split(split_text, -cur[2], cats, aliases)
                recat.split(conn, fp, parts)
                stats["split"] += 1
                msg.append("✓ разбито, обновите вкладку")
            elif new_raw:
                cat = _resolve(new_raw, cats, aliases)
                if cat is None:
                    raise ValueError(f"неизвестная категория «{new_raw}» (добавьте её командой /add_category)")
                if cat != cur[1]:
                    by_cat.setdefault(cat, []).append(fp)
                    stats["category"] += 1
                    msg.append(f"✓ {cat}")
        except ValueError as e:
            stats["errors"] += 1
            msg.append(f"⚠ {e}")
        status.append("; ".join(msg))
    for cat, fps in by_cat.items():
        recat.set_category(conn, fps, cat)
    recat.apply_overrides(conn)
    if status:
        ws.update(values=[[s] for s in status], range_name=f"H2:H{len(status) + 1}", value_input_option="USER_ENTERED")
    return stats
