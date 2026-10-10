"""Схема таблицы transactions (операции из банковских выписок).

Таблица expenses (ручные траты из Telegram) не меняется: выписки пишутся только сюда,
чтобы ручные записи и покупки из выписок не дублировали друг друга.
"""

TRANSACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT    NOT NULL,             -- например alfa_1234, sber_5678
    fingerprint   TEXT    NOT NULL UNIQUE,      -- повторная загрузка не создаёт дублей
    date          TEXT    NOT NULL,             -- дата операции YYYY-MM-DD
    posted        TEXT,                         -- дата проводки банком
    amount_kop    INTEGER NOT NULL,             -- копейки: + пришло, - ушло
    type          TEXT    NOT NULL,             -- expense / refund / income / transfer / technical / ...
    category      TEXT    NOT NULL DEFAULT '',
    merchant      TEXT    NOT NULL DEFAULT '',
    description   TEXT    NOT NULL DEFAULT '',
    mcc           TEXT    NOT NULL DEFAULT '',
    bank_category TEXT    NOT NULL DEFAULT '',
    counterparty  TEXT    NOT NULL DEFAULT '',
    card_last4    TEXT    NOT NULL DEFAULT '',
    subtype       TEXT    NOT NULL DEFAULT '',
    derived       INTEGER NOT NULL DEFAULT 0    -- 1 = строка добавлена импортом (проценты по кредитке)
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (date);
CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions (type);
"""
