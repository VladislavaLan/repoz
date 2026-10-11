"""Разбор скриншота «История операций» из приложения банка (Альфа, Сбер и похожие).

Распознавание — Tesseract (бесплатно, локально, картинка никуда не отправляется). Нужен установленный Tesseract
с русским языком. Путь можно задать переменной TESSERACT_CMD.

Скриншот не содержит времени и номера карты, поэтому такие операции получают source="screenshot".
Когда придёт полная выписка, дубли (та же сумма, дата ±1 день) убираются автоматически — см. drop_screenshot_dupes.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config
from . import normalize as nz

SOURCE = "screenshot"
TEMP_SOURCES = ("screenshot", "manual")      # предварительные данные: выписка точнее и заменяет их

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
RE_HEADER = re.compile(r"(?P<d>\d{1,2})\s+(?P<m>" + "|".join(MONTHS) + r")\b", re.IGNORECASE)
RE_AMOUNT = re.compile(
    r"^(?P<title>.*?)\s*(?P<sign>[−–—\-+])\s?(?P<amt>\d{1,3}(?:[  ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"\s*[₽РPDpрР]?\s*[^\d\s]{0,3}\s*$"
)
RE_PERSON = re.compile(r"^[А-ЯЁA-Z][а-яёa-z]+ [А-ЯЁA-Z]\.?$")
RE_SELF = re.compile(r"себе|собственн|между своими|между счетами|накопительн", re.IGNORECASE)

# категории банка в приложении -> наши (используются, если по названию магазина категория не нашлась)
BANK_CATEGORY = {
    "продукты": "Продукты", "супермаркеты": "Продукты",
    "связь, интернет и тв": "Связь и подписки", "связь": "Связь и подписки",
    "транспорт": "Транспорт", "такси": "Транспорт", "авто": "Транспорт",
    "кафе и рестораны": "Кафе и рестораны", "рестораны и кафе": "Кафе и рестораны", "рестораны": "Кафе и рестораны",
    "здоровье": "Здоровье", "аптеки": "Аптека", "красота": "Красота", "здоровье и красота": "Здоровье",
    "путешествия": "Путешествия", "отели": "Путешествия",
    "покупки": "Прочие товары", "одежда": "Одежда и обувь", "дом и ремонт": "Ремонт", "все для дома": "Прочие товары",
    "развлечения": "Развлечения", "кино": "Развлечения", "жкх": "Дом и ЖКХ", "коммунальные платежи": "Дом и ЖКХ",
}


@dataclass
class ShotOp:
    day: date
    sign: str
    kop: int
    title: str
    category: str = ""
    context: list[str] = field(default_factory=list)


# ---------- распознавание ----------

def find_tesseract() -> str | None:
    cmd = os.getenv("TESSERACT_CMD")
    if cmd and os.path.isfile(cmd):
        return cmd
    return shutil.which("tesseract")


def ocr_lines(image_path: str, timeout: int = 90) -> list[str]:
    """Строки текста со скриншота. RuntimeError с подсказкой, если Tesseract или русский язык не установлены."""
    exe = find_tesseract()
    if not exe:
        raise RuntimeError("Не найден Tesseract. Установите его (с русским языком) или задайте TESSERACT_CMD в .env.")
    langs = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=30).stdout.split()
    if "rus" not in langs:
        raise RuntimeError("В Tesseract нет русского языка. Переустановите его и отметьте «Russian» в списке языков.")
    r = subprocess.run([exe, image_path, "stdout", "-l", "rus+eng", "--psm", "6"],
                       capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("Tesseract не смог прочитать картинку.")
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


# ---------- разбор ----------

def _header_date(line: str, today: date) -> date | None:
    m = RE_HEADER.search(line)
    if not m:
        return None
    d, mon = int(m["d"]), MONTHS[m["m"].lower()]
    year = today.year if mon <= today.month else today.year - 1
    try:
        return date(year, mon, d)
    except ValueError:
        return None


def parse_lines(lines: list[str], today: date | None = None) -> list[ShotOp]:
    today = today or date.today()
    day: date | None = None
    ops: list[ShotOp] = []
    cur: ShotOp | None = None
    for line in lines:
        h = _header_date(line, today)
        if h and not RE_AMOUNT.match(line):
            day, cur = h, None
            continue
        if day is None:
            continue
        m = RE_AMOUNT.match(line)
        if m:
            title = m["title"].strip(" .·|")
            kop = int(round(float(m["amt"].replace(" ", "").replace(" ", "").replace(",", ".")) * 100))
            # «Продукты  +4 ₽»: вторая строка операции с кешбэком, а не новая операция
            if cur and not cur.category and m["sign"] == "+" and kop < 100_000 and title:
                cur.category = title
                continue
            if not title:
                continue                       # одинокая сумма (кешбэк) — пропускаем
            cur = ShotOp(day=day, sign="+" if m["sign"] == "+" else "-", kop=kop, title=title)
            ops.append(cur)
        elif cur is not None:
            if not cur.category:
                cur.category = line
            else:
                cur.context.append(line)       # «Перевод собственных средств» и т.п.
    return ops


def _mask(title: str) -> str:
    return re.sub(r"\d{10,}", lambda m: "…" + m.group()[-4:], title)


def to_txns(ops: list[ShotOp]) -> list[nz.Txn]:
    people = {k.strip(" ."): v for k, v in config.load().get("people", {}).items()}
    out: list[nz.Txn] = []
    for o in ops:
        text = " ".join([o.title, o.category, *o.context])
        amount = o.kop if o.sign == "+" else -o.kop
        title = _mask(o.title)
        t = nz.Txn(source=SOURCE, fingerprint=nz._fp("shot", o.day.isoformat(), str(amount), title),
                   date=o.day, posted=None, amount_kop=amount, type="", category="", merchant=title,
                   description=text[:160], bank_category=o.category, subtype="screenshot")
        role = people.get(o.title, "")
        if RE_SELF.search(text) or role == "own":
            t.type = "transfer"
        elif RE_PERSON.match(o.title) and o.category.lower().startswith("перевод"):
            fam = role == "family"
            t.type = ("family_" if fam else "p2p_") + ("in" if amount > 0 else "out")
            t.counterparty = o.title
        elif amount > 0:
            t.type, t.category = "income", ("Зарплата" if re.search("зарплат", text, re.IGNORECASE) else "Пополнение")
        else:
            t.type = "expense"
            t.category = nz.categorize(o.title)
            if t.category == "Прочее":
                t.category = BANK_CATEGORY.get(o.category.lower().strip(" ."), "Прочее")
        out.append(t)
    return out


def read_screenshot(image_path: str, today: date | None = None) -> list[nz.Txn]:
    from .importer import dedupe
    return dedupe(to_txns(parse_lines(ocr_lines(image_path), today)))


# ---------- дубли с выписками ----------

def find_statement_dupes(conn: sqlite3.Connection, txns: list[nz.Txn]) -> set[str]:
    """Отпечатки операций, которые уже есть в базе из выписки (сумма та же, дата ±1 день)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='transactions'").fetchone():
        return set()
    dup = set()
    for t in txns:
        lo, hi = (t.date - timedelta(days=1)).isoformat(), (t.date + timedelta(days=1)).isoformat()
        if conn.execute("SELECT 1 FROM transactions WHERE source != ? AND type != 'duplicate' AND amount_kop = ? "
                        "AND date BETWEEN ? AND ? LIMIT 1", (SOURCE, t.amount_kop, lo, hi)).fetchone():
            dup.add(t.fingerprint)
    return dup


def drop_screenshot_dupes(conn: sqlite3.Connection) -> int:
    """Помечает строки из скриншотов и ручных трат, которые теперь есть в выписках (type='duplicate').
    Строки не удаляются, чтобы ручная трата не копировалась из expenses повторно. Возвращает, сколько помечено."""
    cur = conn.execute(
        "UPDATE transactions SET type = 'duplicate' WHERE source IN ('screenshot', 'manual') "
        "AND type != 'duplicate' AND EXISTS (SELECT 1 FROM transactions s WHERE s.source NOT IN ('screenshot', 'manual') "
        "AND s.amount_kop = transactions.amount_kop AND ABS(julianday(s.date) - julianday(transactions.date)) <= 1)")
    conn.commit()
    return cur.rowcount
