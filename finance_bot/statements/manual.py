"""Ручные траты из /add_expense -> общая таблица transactions (source="manual").

Таблица expenses остаётся как есть (на ней работает /stats). Сюда копируются только те записи, которых ещё нет.
Когда приходит выписка с той же суммой (дата ±1 день), ручная строка помечается type="duplicate" и в отчёты не входит.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import config
from . import normalize as nz
from .schema import TRANSACTIONS_SCHEMA

SOURCE = "manual"


def to_txn(expense_id: int, amount: float, category: str, description: str, day: str) -> nz.Txn:
    alias = {k.lower(): v for k, v in config.load().get("category_aliases", {}).items()}
    cat = alias.get(category.lower())
    if not cat:
        guessed = nz.categorize(f"{category} {description}")
        cat = guessed if guessed != "Прочее" else category
    return nz.Txn(source=SOURCE, fingerprint=f"manual|{expense_id}", date=date.fromisoformat(day), posted=None,
                  amount_kop=-int(round(amount * 100)), type="expense", category=cat, merchant=description or category,
                  description=description[:160], subtype="manual")


def sync_manual(conn: sqlite3.Connection) -> int:
    """Копирует новые записи expenses в transactions. Возвращает, сколько добавлено."""
    from .importer import write
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='expenses'").fetchone():
        return 0
    conn.executescript(TRANSACTIONS_SCHEMA)
    have = {r[0] for r in conn.execute("SELECT fingerprint FROM transactions WHERE source = ?", (SOURCE,))}
    new = [to_txn(i, a, c, d, day) for i, a, c, d, day in
           conn.execute("SELECT id, amount, category, description, date FROM expenses ORDER BY id")
           if f"manual|{i}" not in have]
    if new:
        write(conn, new)
    return len(new)
