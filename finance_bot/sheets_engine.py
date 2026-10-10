"""Дублирование транзакций в Google Таблицу через gspread (Service Account).

Настройки берутся из окружения (.env):
    GOOGLE_SPREADSHEET_NAME  — название таблицы (обязательно для включения модуля)
    GOOGLE_CREDENTIALS_FILE  — путь к ключу сервисного аккаунта
                               (по умолчанию credentials.json рядом с этим файлом)

Если таблица не настроена, модуль тихо выключен: бот работает только с SQLite.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import date as date_type, datetime
from pathlib import Path

import gspread

log = logging.getLogger(__name__)

HEADER = ["Дата", "User ID", "Тип", "Категория", "Сумма", "Комментарий"]
TRANSACTION_TYPES = {"expense": "Расход", "income": "Доход"}

_lock = threading.Lock()
_worksheet: gspread.Worksheet | None = None


def _credentials_path() -> Path:
    raw = os.getenv("GOOGLE_CREDENTIALS_FILE")
    # Путь относительно папки бота, а не текущей директории — так запуск
    # из любого места (в т.ч. на Windows через ярлык) находит ключ.
    return Path(raw) if raw else Path(__file__).resolve().parent / "credentials.json"


def is_enabled() -> bool:
    return bool(os.getenv("GOOGLE_SPREADSHEET_NAME")) and _credentials_path().is_file()


def _get_worksheet() -> gspread.Worksheet:
    """Подключается к первой вкладке таблицы (один раз, дальше — из кэша)."""
    global _worksheet
    if _worksheet is None:
        name = os.getenv("GOOGLE_SPREADSHEET_NAME")
        if not name:
            raise RuntimeError("Не задана переменная окружения GOOGLE_SPREADSHEET_NAME")
        client = gspread.service_account(filename=str(_credentials_path()))
        worksheet = client.open(name).sheet1  # open() по названию требует Google Drive API
        if not worksheet.row_values(1):
            worksheet.append_row(HEADER, value_input_option="USER_ENTERED")
            worksheet.freeze(rows=1)
        _worksheet = worksheet
    return _worksheet


def get_spreadsheet() -> gspread.Spreadsheet:
    """Вся таблица целиком (для выгрузки выписок на отдельные вкладки)."""
    name = os.getenv("GOOGLE_SPREADSHEET_NAME")
    if not name:
        raise RuntimeError("Не задана переменная окружения GOOGLE_SPREADSHEET_NAME")
    with _lock:
        return gspread.service_account(filename=str(_credentials_path())).open(name)


def append_transaction(
    date: date_type | datetime | str,
    user_id: int,
    category: str,
    amount: float,
    comment: str = "",
    transaction_type: str = "expense",
) -> None:
    """Синхронно добавляет строку в первую вкладку таблицы.

    transaction_type: "expense" / "income" (или любая своя подпись — запишется как есть).
    Блокирует поток на время HTTP-запроса — из async-кода вызывайте
    append_transaction_async().
    """
    global _worksheet
    if isinstance(date, datetime):
        date_str = date.strftime("%Y-%m-%d %H:%M")
    elif isinstance(date, date_type):
        date_str = date.isoformat()
    else:
        date_str = str(date)

    row = [
        date_str,
        user_id,
        TRANSACTION_TYPES.get(transaction_type, transaction_type),
        category,
        round(float(amount), 2),
        comment,
    ]
    with _lock:  # gspread-клиент не потокобезопасен; заодно сохраняем порядок строк
        try:
            # USER_ENTERED: Google распознает дату и число, а не сохранит их текстом.
            _get_worksheet().append_row(row, value_input_option="USER_ENTERED")
        except Exception:
            _worksheet = None  # при следующей попытке переподключимся
            raise


async def append_transaction_async(*args, **kwargs) -> bool:
    """Неблокирующая обёртка: выполняет запись в отдельном потоке.

    Никогда не бросает исключений — ошибка Google не должна ломать бота.
    Возвращает True, если строка записана.
    """
    if not is_enabled():
        return False
    try:
        await asyncio.to_thread(append_transaction, *args, **kwargs)
        return True
    except Exception:
        log.exception("Не удалось записать транзакцию в Google Таблицу")
        return False


# Фоновые задачи держим в множестве, иначе сборщик мусора может их прервать.
_background_tasks: set[asyncio.Task] = set()


def schedule_append(*args, **kwargs) -> None:
    """Запускает запись в фоне, не задерживая ответ пользователю."""
    task = asyncio.create_task(append_transaction_async(*args, **kwargs))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
