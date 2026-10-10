"""Асинхронный слой доступа к SQLite (aiosqlite)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

import aiosqlite

from statements.schema import TRANSACTIONS_SCHEMA

DB_PATH = os.getenv("DB_PATH", "finance.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    amount      REAL    NOT NULL CHECK (amount > 0),
    category    TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    date        TEXT    NOT NULL            -- ISO-формат YYYY-MM-DD
);

CREATE TABLE IF NOT EXISTS debts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    total_amount   REAL    NOT NULL CHECK (total_amount >= 0),
    interest_rate  REAL    NOT NULL CHECK (interest_rate >= 0),  -- % годовых
    min_payment    REAL    NOT NULL CHECK (min_payment >= 0),
    grace_end_date TEXT                      -- YYYY-MM-DD или NULL
);

CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses (date);
""" + TRANSACTIONS_SCHEMA


@dataclass(slots=True)
class Expense:
    id: int
    amount: float
    category: str
    description: str
    date: date


@dataclass(slots=True)
class Debt:
    id: int
    name: str
    total_amount: float
    interest_rate: float
    min_payment: float
    grace_end_date: date | None = None


async def init_db(db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ---------- Расходы ----------

async def add_expense(
    amount: float,
    category: str,
    description: str = "",
    on_date: date | None = None,
    db_path: str = DB_PATH,
) -> int:
    on_date = on_date or date.today()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO expenses (amount, category, description, date) VALUES (?, ?, ?, ?)",
            (amount, category, description, on_date.isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_expenses_by_category(
    start: date, end: date, db_path: str = DB_PATH
) -> dict[str, float]:
    """Суммы расходов по категориям за полуинтервал [start, end)."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """
            SELECT category, SUM(amount) FROM expenses
            WHERE date >= ? AND date < ?
            GROUP BY category
            ORDER BY SUM(amount) DESC
            """,
            (start.isoformat(), end.isoformat()),
        ) as cursor:
            return {category: total for category, total in await cursor.fetchall()}


# ---------- Долги ----------

async def add_debt(
    name: str,
    total_amount: float,
    interest_rate: float,
    min_payment: float,
    grace_end_date: date | None = None,
    db_path: str = DB_PATH,
) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO debts (name, total_amount, interest_rate, min_payment, grace_end_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                total_amount,
                interest_rate,
                min_payment,
                grace_end_date.isoformat() if grace_end_date else None,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_debts(db_path: str = DB_PATH) -> list[Debt]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT id, name, total_amount, interest_rate, min_payment, grace_end_date FROM debts"
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        Debt(
            id=row[0],
            name=row[1],
            total_amount=row[2],
            interest_rate=row[3],
            min_payment=row[4],
            grace_end_date=date.fromisoformat(row[5]) if row[5] else None,
        )
        for row in rows
    ]


async def delete_debt(debt_id: int, db_path: str = DB_PATH) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM debts WHERE id = ?", (debt_id,))
        await db.commit()
        return cursor.rowcount > 0
