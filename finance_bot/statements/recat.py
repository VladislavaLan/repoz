"""Ручная правка категорий: по отдельным операциям и по магазинам, плюс пометка «плановая трата».

Правки хранятся в отдельных таблицах и переживают повторный импорт выписок:
  tx_overrides    отпечаток операции -> своя категория и/или флаг planned (плановая: не искажает средние и советы)
  merchant_rules  фрагмент названия магазина -> категория (действует и на будущие операции)
"""

from __future__ import annotations

import sqlite3

from . import report

SCHEMA = """
CREATE TABLE IF NOT EXISTS tx_overrides (
    fingerprint TEXT PRIMARY KEY,
    category    TEXT NOT NULL DEFAULT '',
    planned     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS merchant_rules (
    needle   TEXT PRIMARY KEY,      -- фрагмент названия, без учёта регистра
    category TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tx_splits (
    fingerprint TEXT NOT NULL,      -- исходная операция
    idx         INTEGER NOT NULL,
    category    TEXT NOT NULL,
    amount_kop  INTEGER NOT NULL,   -- часть, положительное число
    PRIMARY KEY (fingerprint, idx)
);
"""
SPEND_TYPES = ("expense", "refund")
from .normalize import RENAMED


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _has_transactions(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='transactions'").fetchone() is not None


def find_ops(conn: sqlite3.Connection, query: str, start: str | None = None, limit: int = 300) -> list[dict]:
    """Траты, в названии которых есть query (без учёта регистра). Свежие сверху."""
    if not _has_transactions(conn):
        return []
    ensure(conn)
    start = start or report.default_start()
    q = query.strip().casefold()
    rows = conn.execute(
        f"SELECT t.fingerprint, t.date, t.amount_kop, t.merchant, t.category, COALESCE(o.planned, 0) "
        f"FROM transactions t LEFT JOIN tx_overrides o ON o.fingerprint = t.fingerprint "
        f"WHERE t.date >= ? AND t.type IN ({','.join('?' * len(SPEND_TYPES))}) ORDER BY t.date DESC, t.id DESC",
        (start, *SPEND_TYPES),
    ).fetchall()
    out = [{"fingerprint": f, "date": d, "kop": k, "merchant": m, "category": c or "Прочее", "planned": bool(p)}
           for f, d, k, m, c, p in rows if q in (m or "").casefold()]
    return out[:limit]


def top_merchants(conn: sqlite3.Connection, category: str, start: str | None = None, limit: int = 10) -> list[tuple[str, float, int]]:
    """Самые «тяжёлые» получатели в категории: (название, сумма ₽, число операций)."""
    if not _has_transactions(conn):
        return []
    start = start or report.default_start()
    acc: dict[str, list[float]] = {}
    for m, k, c in conn.execute(
        f"SELECT merchant, amount_kop, category FROM transactions WHERE date >= ? AND type IN ('expense','refund')", (start,)
    ):
        if (c or "Прочее").casefold() != category.casefold():
            continue
        v = acc.setdefault(m or "(без названия)", [0.0, 0])
        v[0] -= k / 100.0
        v[1] += 1
    rows = sorted(((m, v[0], int(v[1])) for m, v in acc.items()), key=lambda x: -x[1])
    return rows[:limit]


def set_category(conn: sqlite3.Connection, fingerprints: list[str], category: str) -> int:
    ensure(conn)
    for fp in fingerprints:
        conn.execute("INSERT INTO tx_overrides (fingerprint, category) VALUES (?, ?) "
                     "ON CONFLICT(fingerprint) DO UPDATE SET category = excluded.category", (fp, category))
        conn.execute("UPDATE transactions SET category = ? WHERE fingerprint = ?", (category, fp))
    conn.commit()
    return len(fingerprints)


def set_planned(conn: sqlite3.Connection, fingerprints: list[str], planned: bool) -> int:
    ensure(conn)
    for fp in fingerprints:
        conn.execute("INSERT INTO tx_overrides (fingerprint, planned) VALUES (?, ?) "
                     "ON CONFLICT(fingerprint) DO UPDATE SET planned = excluded.planned", (fp, int(planned)))
    conn.commit()
    return len(fingerprints)


def add_rule(conn: sqlite3.Connection, needle: str, category: str) -> int:
    """Правило «всё, где в названии есть needle, — это category». Сразу применяется ко всей истории."""
    ensure(conn)
    conn.execute("INSERT INTO merchant_rules (needle, category) VALUES (?, ?) "
                 "ON CONFLICT(needle) DO UPDATE SET category = excluded.category", (needle.strip().casefold(), category))
    conn.commit()
    return apply_overrides(conn)


def apply_overrides(conn: sqlite3.Connection) -> int:
    """Накладывает правила магазинов, затем точечные правки операций. Возвращает число изменённых строк."""
    if not _has_transactions(conn):
        return 0
    ensure(conn)
    changed = 0
    for old, new in RENAMED.items():              # категория «Покупки» переименована в «Прочие товары»
        for table in ("tx_overrides", "merchant_rules", "tx_splits"):
            conn.execute(f"UPDATE {table} SET category = ? WHERE category = ?", (new, old))
    rules = conn.execute("SELECT needle, category FROM merchant_rules").fetchall()
    if rules:
        for id_, merchant, cat in conn.execute(
            "SELECT id, merchant, category FROM transactions WHERE type IN ('expense','refund') "
            "AND fingerprint NOT LIKE '%#s%'"
        ).fetchall():
            for needle, new in rules:
                if needle in (merchant or "").casefold():
                    if cat != new:
                        conn.execute("UPDATE transactions SET category = ? WHERE id = ?", (new, id_))
                        changed += 1
                    break
    cur = conn.execute(
        "UPDATE transactions SET category = (SELECT category FROM tx_overrides o WHERE o.fingerprint = transactions.fingerprint) "
        "WHERE fingerprint IN (SELECT fingerprint FROM tx_overrides WHERE category != '') "
        "AND category != (SELECT category FROM tx_overrides o WHERE o.fingerprint = transactions.fingerprint)")
    changed += cur.rowcount
    conn.commit()
    return changed + apply_splits(conn)


# ---------- разбивка одной операции на части ----------

SPLIT_MARK = "#s"


def parent_of(fingerprint: str) -> str | None:
    """Отпечаток исходной операции для части разбивки (или None, если это обычная операция)."""
    return fingerprint.rsplit(SPLIT_MARK, 1)[0] if SPLIT_MARK in fingerprint else None


def parse_split(text: str, total_kop: int, categories: list[str], aliases: dict[str, str] | None = None) -> list[tuple[str, int]]:
    """«2000 Красота, 3000 Подарки» -> [("Красота", 200000), ("Подарки", 300000)].
    Последняя часть может быть без суммы: она получит остаток. ValueError с понятным текстом при ошибке."""
    import re
    names = {c.casefold(): c for c in categories}
    names.update({k.casefold(): v for k, v in (aliases or {}).items()})
    parts, used = [], 0
    chunks = [c.strip() for c in re.split(r"[,;\n]+", text) if c.strip()]
    if len(chunks) < 2:
        raise ValueError("Нужно минимум две части, например: 2000 Красота, 3000 Подарки")
    for n, chunk in enumerate(chunks):
        m = re.match(r"^(?:(?P<amt>\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s+)?(?P<cat>\D.*)$", chunk)
        if not m:
            raise ValueError(f"Не поняла «{chunk}». Формат: сумма и категория, например 2000 Красота")
        cat_raw = m["cat"].strip().casefold()
        cat = names.get(cat_raw)
        if cat is None:
            starts = [v for k, v in names.items() if k.startswith(cat_raw)]
            if len(set(starts)) == 1:
                cat = starts[0]
        if cat is None:
            raise ValueError(f"Не знаю категорию «{m['cat'].strip()}». Доступные: " + ", ".join(sorted(set(categories))))
        if m["amt"]:
            kop = int(round(float(m["amt"].replace(" ", "").replace("\u00a0", "").replace(",", ".")) * 100))
        elif n == len(chunks) - 1:
            kop = total_kop - used
        else:
            raise ValueError(f"У части «{chunk}» нет суммы. Без суммы может быть только последняя часть (она получит остаток).")
        if kop <= 0:
            raise ValueError("Суммы частей должны быть больше нуля и вместе не больше суммы операции.")
        used += kop
        parts.append((cat, kop))
    if used != total_kop:
        raise ValueError(f"Части дают {used / 100:,.2f} ₽, а операция — {total_kop / 100:,.2f} ₽. "
                         "Сумма частей должна совпасть (или оставьте последнюю часть без суммы).".replace(",", " "))
    return parts


def split(conn: sqlite3.Connection, fingerprint: str, parts: list[tuple[str, int]]) -> int:
    """Запоминает разбивку и сразу применяет её. Исходная строка остаётся в базе, но в отчёты входят части."""
    ensure(conn)
    row = conn.execute("SELECT type, amount_kop FROM transactions WHERE fingerprint = ?", (fingerprint,)).fetchone()
    if not row or row[0] not in ("expense", "split"):
        raise ValueError("Разбить можно только трату.")
    if sum(k for _, k in parts) != -row[1]:
        raise ValueError("Сумма частей не совпадает с суммой операции.")
    conn.execute("DELETE FROM tx_splits WHERE fingerprint = ?", (fingerprint,))
    conn.executemany("INSERT INTO tx_splits (fingerprint, idx, category, amount_kop) VALUES (?, ?, ?, ?)",
                     [(fingerprint, i + 1, c, k) for i, (c, k) in enumerate(parts)])
    conn.commit()
    apply_splits(conn)
    return len(parts)


def unsplit(conn: sqlite3.Connection, fingerprint: str) -> int:
    """Отменяет разбивку: fingerprint — исходной операции или любой её части. Возвращает, сколько частей убрано."""
    ensure(conn)
    root = parent_of(fingerprint) or fingerprint
    like = root + SPLIT_MARK + "%"
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE fingerprint LIKE ?", (like,)).fetchone()[0]
    conn.execute("DELETE FROM transactions WHERE fingerprint LIKE ?", (like,))
    conn.execute("DELETE FROM tx_splits WHERE fingerprint = ? OR fingerprint LIKE ?", (root, like))
    conn.execute("DELETE FROM tx_overrides WHERE fingerprint LIKE ?", (like,))
    conn.execute("UPDATE transactions SET type = 'expense' WHERE fingerprint = ? AND type = 'split'", (root,))
    conn.commit()
    return n


def apply_splits(conn: sqlite3.Connection) -> int:
    """Создаёт строки-части для всех разбивок и скрывает исходные (type='split'). Идемпотентно."""
    if not _has_transactions(conn):
        return 0
    ensure(conn)
    done = 0
    defs = conn.execute("SELECT fingerprint, idx, category, amount_kop FROM tx_splits ORDER BY LENGTH(fingerprint), fingerprint, idx").fetchall()
    for fp, idx, cat, kop in defs:
        src = conn.execute("SELECT source, date, posted, type, merchant, description, mcc, bank_category, counterparty, "
                           "card_last4, subtype FROM transactions WHERE fingerprint = ?", (fp,)).fetchone()
        if not src:
            continue
        if src[3] == "expense":
            conn.execute("UPDATE transactions SET type = 'split' WHERE fingerprint = ?", (fp,))
        conn.execute(
            "INSERT INTO transactions (source, fingerprint, date, posted, amount_kop, type, category, merchant, description, "
            "mcc, bank_category, counterparty, card_last4, subtype, derived) VALUES (?, ?, ?, ?, ?, 'expense', ?, ?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(fingerprint) DO UPDATE SET amount_kop = excluded.amount_kop, category = excluded.category, type = 'expense'",
            (src[0], f"{fp}{SPLIT_MARK}{idx}", src[1], src[2], -kop, cat, src[4], src[5], src[6], src[7], src[8], src[9], src[10]))
        done += 1
    # части, которых больше нет в tx_splits (разбивку переделали на меньшее число частей)
    keep = {f"{fp}{SPLIT_MARK}{i}" for fp, i, _, _ in defs}
    for (fp,) in conn.execute("SELECT fingerprint FROM transactions WHERE fingerprint LIKE ?", ("%" + SPLIT_MARK + "%",)).fetchall():
        if fp not in keep:
            conn.execute("DELETE FROM transactions WHERE fingerprint = ?", (fp,))
    conn.commit()
    return done
