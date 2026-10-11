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
"""
SPEND_TYPES = ("expense", "refund")


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
    rules = conn.execute("SELECT needle, category FROM merchant_rules").fetchall()
    if rules:
        for id_, merchant, cat in conn.execute(
            "SELECT id, merchant, category FROM transactions WHERE type IN ('expense','refund')"
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
    return changed
