"""Telegram-бот учёта личных финансов и погашения долгов по «Лавине»."""

from __future__ import annotations

import asyncio
import io
import tempfile
import uuid
import logging
import os
import re
import sqlite3
from datetime import date, datetime
from html import escape

from dotenv import load_dotenv

load_dotenv()  # до импорта database, чтобы подхватить DB_PATH из .env

from aiogram import Bot, Dispatcher, F, Router  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.aiohttp import AiohttpSession  # noqa: E402
from aiogram.exceptions import TelegramUnauthorizedError  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.filters import Command, CommandObject, CommandStart  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.state import State, StatesGroup  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message  # noqa: E402

import database as db  # noqa: E402
import sheets_engine  # noqa: E402
from chart_engine import build_month_chart  # noqa: E402
from debt_engine import avalanche_savings  # noqa: E402
from statements import config as stcfg  # noqa: E402
from statements import insights, manual, recat, screenshot, sheets_export  # noqa: E402
from statements import normalize as nz  # noqa: E402
from statements.importer import write as write_txns  # noqa: E402
from statements.report import build_report, default_start, format_telegram  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

router = Router()

_allowed = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
if _allowed:
    router.message.filter(F.from_user.id.in_(_allowed))


# ---------- Утилиты ----------

# "450 Еда обед", "1 200,50 Транспорт такси" -> сумма, категория, описание
EXPENSE_RE = re.compile(
    r"^\s*(?P<amount>\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"\s+(?P<category>\S+)(?:\s+(?P<description>.+))?\s*$",
    re.DOTALL,
)

def parse_number(text: str) -> float:
    """'1 500,50' -> 1500.5. Бросает ValueError при неверном вводе."""
    value = float(text.replace(" ", "").replace(" ", "").replace(",", ".").rstrip("%₽"))
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(text)
    return value


def money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".00", "") + " ₽"


def months_text(n: int) -> str:
    years, months = divmod(n, 12)
    parts = []
    if years:
        parts.append(f"{years} г.")
    if months or not parts:
        parts.append(f"{months} мес.")
    return " ".join(parts)


HELP_TEXT = (
    "👋 <b>Бот учёта финансов и долгов</b>\n\n"
    "<b>Расходы</b>\n"
    "/add_expense — записать расход (бот спросит сумму, категорию и описание)\n"
    "   или сразу: <code>/add_expense 450 Еда обед в кафе</code>\n"
    "/stats — диаграмма расходов за текущий месяц\n"
    "/report <code>[ГГГГ-ММ-ДД]</code> — отчёт по загруженным банковским выпискам\n"
    "📷 Пришлите скриншот истории операций из банка — я распознаю его и предложу сохранить\n"
    "/top <code>[категория]</code> — какие магазины в категории (без названия — в «Прочее»)\n"
    "/recat <code>озон</code> — поправить категории у операций магазина, отметить плановые траты\n"
    "/sheet — выгрузить операции из выписок в Google Таблицу (вкладки «Выписки» и «Итоги по месяцам»)\n"
    "/advice <code>[ГГГГ-ММ-ДД]</code> — комментарии и советы по тратам (с ключом Gemini — и от нейросети)\n\n"
    "<b>Долги</b>\n"
    "/add_debt — добавить долг (пошаговый диалог)\n"
    "/debts — список долгов и порядок погашения по «Лавине»\n"
    "/debts <code>сумма</code> — плюс расчёт экономии от досрочного платежа\n"
    "/del_debt <code>id</code> — удалить долг\n"
    "/cancel — отменить текущий диалог"
)


# ---------- /start ----------

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("❌ Действие отменено.")


# ---------- /add_expense ----------

def _sync_manual() -> int:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return manual.sync_manual(conn)
    except sqlite3.Error:
        log.exception("Не удалось скопировать ручные траты в transactions")
        return 0
    finally:
        conn.close()


async def _save_expense(user_id: int, amount: float, category: str, description: str) -> str:
    """Записывает расход (база + общая таблица + Google Таблица). Возвращает текст подтверждения."""
    await db.add_expense(amount, category, description)
    await asyncio.to_thread(_sync_manual)      # копия в общую таблицу для /report и /advice
    # Дублируем в Google Таблицу в фоне: ответ пользователю не ждёт сети.
    sheets_engine.schedule_append(date.today(), user_id, category, amount, description, "expense")
    text = f"✅ Записал расход: <b>{money(amount)}</b> — {escape(category)}"
    if description:
        text += f" ({escape(description)})"
    return text


class AddExpense(StatesGroup):
    amount = State()
    category = State()
    description = State()


DEFAULT_CATEGORIES = ["Продукты", "Кафе и рестораны", "Транспорт", "Покупки", "Здоровье", "Развлечения",
                      "Связь и подписки", "Дом и ЖКХ"]


def _category_choices() -> list[str]:
    """Сначала твои частые категории из ручных трат, потом стандартные."""
    out: list[str] = []
    try:
        conn = sqlite3.connect(db.DB_PATH)
        try:
            for (c,) in conn.execute("SELECT category FROM expenses GROUP BY category ORDER BY COUNT(*) DESC LIMIT 6"):
                if c not in out:
                    out.append(c)
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    for c in DEFAULT_CATEGORIES:
        if c not in out and len(out) < 10:
            out.append(c)
    return out


@router.message(Command("add_expense"))
async def cmd_add_expense(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not command.args:                       # из меню: задаём вопросы по шагам
        await state.clear()
        await state.set_state(AddExpense.amount)
        await message.answer("💸 Сколько потратили? Напишите сумму, например <code>450</code>.\n"
                             "Можно сразу всё: <code>450 Еда обед</code>. /cancel — отмена.")
        return

    match = EXPENSE_RE.match(command.args)
    if not match:
        await message.answer("Не понял запись. Пример: <code>/add_expense 450 Еда обед</code>\n"
                             "Или просто отправьте /add_expense и ответьте на вопросы.")
        return
    amount = parse_number(match["amount"])
    if amount <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return
    await state.clear()
    await message.answer(await _save_expense(message.from_user.id, amount, match["category"].capitalize(),
                                             (match["description"] or "").strip()))


@router.message(AddExpense.amount, F.text, ~F.text.startswith("/"))
async def expense_amount(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    full = EXPENSE_RE.match(text)
    if full and re.search(r"[A-Za-zА-Яа-яЁё]", full["category"]):     # «450 Еда обед» одним сообщением
        amount = parse_number(full["amount"])
        if amount > 0:
            await state.clear()
            await message.answer(await _save_expense(message.from_user.id, amount, full["category"].capitalize(),
                                                     (full["description"] or "").strip()))
            return
    try:
        amount = parse_number(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите сумму числом, например <code>450</code> или <code>1 200,50</code>.")
        return
    cats = _category_choices()
    await state.update_data(amount=amount, cats=cats)
    await state.set_state(AddExpense.category)
    rows = [[InlineKeyboardButton(text=c, callback_data=f"exp:c{i}") for i, c in enumerate(cats)][j:j + 2]
            for j in range(0, len(cats), 2)]
    await message.answer(f"Сумма: <b>{money(amount)}</b>. Выберите категорию кнопкой или напишите свою:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _ask_description(target: Message, state: FSMContext, category: str) -> None:
    await state.update_data(category=category)
    await state.set_state(AddExpense.description)
    await target.answer(
        f"Категория: <b>{escape(category)}</b>. Добавьте описание (например, «обед») или нажмите кнопку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Без описания", callback_data="exp:skip")]]))


@router.message(AddExpense.category, F.text, ~F.text.startswith("/"))
async def expense_category_text(message: Message, state: FSMContext) -> None:
    category = message.text.strip()[:40].capitalize()
    await _ask_description(message, state, category)


@router.message(AddExpense.description, F.text, ~F.text.startswith("/"))
async def expense_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await message.answer(await _save_expense(message.from_user.id, data["amount"], data["category"],
                                             message.text.strip()[:200]))


@router.callback_query(F.data.startswith("exp:"))
async def on_expense_button(cb: CallbackQuery, state: FSMContext) -> None:
    if _allowed and cb.from_user.id not in _allowed:
        await cb.answer()
        return
    data = await state.get_data()
    action = cb.data.split(":", 1)[1]
    if action.startswith("c") and await state.get_state() == AddExpense.category.state:
        cats = data.get("cats", [])
        idx = int(action[1:])
        if idx < len(cats):
            await cb.message.edit_reply_markup(reply_markup=None)
            await _ask_description(cb.message, state, cats[idx])
    elif action == "skip" and await state.get_state() == AddExpense.description.state:
        await state.clear()
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer(await _save_expense(cb.from_user.id, data["amount"], data["category"], ""))
    else:
        await cb.answer("Этот вопрос уже закрыт. Начните заново: /add_expense", show_alert=True)
        return
    await cb.answer()


# ---------- /add_debt (FSM) ----------

class AddDebt(StatesGroup):
    name = State()
    amount = State()
    rate = State()
    min_payment = State()
    grace = State()


@router.message(Command("add_debt"))
async def cmd_add_debt(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddDebt.name)
    await message.answer("💳 Добавляем долг. /cancel — отмена.\n\n<b>Шаг 1/5.</b> Название долга (например, «Кредитка Тинькофф»):")


@router.message(AddDebt.name, F.text)
async def debt_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name or name.startswith("/") or len(name) > 100:
        await message.answer("Введите название текстом (до 100 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(AddDebt.amount)
    await message.answer("<b>Шаг 2/5.</b> Остаток долга в рублях:")


@router.message(AddDebt.amount, F.text)
async def debt_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = parse_number(message.text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число, например <code>150000</code>.")
        return
    await state.update_data(amount=amount)
    await state.set_state(AddDebt.rate)
    await message.answer("<b>Шаг 3/5.</b> Процентная ставка, % годовых (например, <code>24.9</code>):")


@router.message(AddDebt.rate, F.text)
async def debt_rate(message: Message, state: FSMContext) -> None:
    try:
        rate = parse_number(message.text)
        if not 0 <= rate <= 1000:
            raise ValueError
    except ValueError:
        await message.answer("Введите ставку числом от 0 до 1000, например <code>17.5</code>.")
        return
    await state.update_data(rate=rate)
    await state.set_state(AddDebt.min_payment)
    await message.answer("<b>Шаг 4/5.</b> Минимальный ежемесячный платёж в рублях:")


@router.message(AddDebt.min_payment, F.text)
async def debt_min_payment(message: Message, state: FSMContext) -> None:
    try:
        min_payment = parse_number(message.text)
        if min_payment <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число, например <code>5000</code>.")
        return

    data = await state.get_data()
    monthly_interest = data["amount"] * data["rate"] / 100 / 12
    warning = ""
    if min_payment <= monthly_interest:
        warning = (
            f"\n⚠️ Платёж не покрывает ежемесячные проценты (~{money(monthly_interest)}): "
            "долг будет только расти."
        )

    await state.update_data(min_payment=min_payment)
    await state.set_state(AddDebt.grace)
    await message.answer(
        f"<b>Шаг 5/5.</b> Дата окончания льготного периода в формате <code>ДД.ММ.ГГГГ</code>, "
        f"или <code>-</code>, если его нет:{warning}"
    )


@router.message(AddDebt.grace, F.text)
async def debt_grace(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    grace_end: date | None = None
    if raw not in ("-", "—", "нет", "Нет"):
        try:
            grace_end = datetime.strptime(raw, "%d.%m.%Y").date()
        except ValueError:
            await message.answer("Не понял дату. Формат <code>ДД.ММ.ГГГГ</code> или <code>-</code>.")
            return

    data = await state.get_data()
    await state.clear()
    debt_id = await db.add_debt(
        name=data["name"],
        total_amount=data["amount"],
        interest_rate=data["rate"],
        min_payment=data["min_payment"],
        grace_end_date=grace_end,
    )
    grace_line = f"\nЛьготный период до: {grace_end:%d.%m.%Y}" if grace_end else ""
    await message.answer(
        f"✅ Долг #{debt_id} добавлен:\n"
        f"<b>{escape(data['name'])}</b> — {money(data['amount'])}, "
        f"{data['rate']:g}% годовых, мин. платёж {money(data['min_payment'])}{grace_line}\n\n"
        "Смотреть план погашения: /debts"
    )


@router.message(AddDebt.name)
@router.message(AddDebt.amount)
@router.message(AddDebt.rate)
@router.message(AddDebt.min_payment)
@router.message(AddDebt.grace)
async def debt_non_text(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте ответ текстом или /cancel для отмены.")


# ---------- /debts ----------

@router.message(Command("debts"))
async def cmd_debts(message: Message, command: CommandObject) -> None:
    extra = 0.0
    if command.args:
        try:
            extra = parse_number(command.args)
            if extra < 0:
                raise ValueError
        except ValueError:
            await message.answer("Формат: <code>/debts [сумма досрочного платежа]</code>")
            return

    debts = await db.get_debts()
    if not debts:
        await message.answer("Долгов нет 🎉 Добавить: /add_debt")
        return

    report = avalanche_savings(debts, extra)
    today = date.today()

    lines = ["🏔 <b>Приоритет погашения по «Лавине»</b>", "<i>(сначала — самая высокая ставка)</i>", ""]
    for i, d in enumerate(report.prioritized, start=1):
        marker = "🔥" if i == 1 else f"{i}."
        grace = ""
        if d.grace_end_date and d.grace_end_date > today:
            grace = f"\n    🕊 льготный период до {d.grace_end_date:%d.%m.%Y}"
        month_closed = report.baseline.payoff_month.get(d.id)
        closed = f", закроется через {months_text(month_closed)}" if month_closed else ""
        lines.append(
            f"{marker} <b>{escape(d.name)}</b> (id {d.id})\n"
            f"    {money(d.total_amount)} · {d.interest_rate:g}% · мин. {money(d.min_payment)}{closed}{grace}"
        )

    total_debt = sum(d.total_amount for d in debts)
    total_min = sum(d.min_payment for d in debts)
    base = report.baseline
    lines += [
        "",
        f"💰 Всего долгов: <b>{money(total_debt)}</b>",
        f"📅 Ежемесячно: {money(total_min)}",
    ]
    if base.finished:
        lines.append(f"⏳ Срок погашения: {months_text(base.months)}, переплата {money(base.total_interest)}")
    else:
        lines.append("⚠️ При текущих платежах долги не погашаются — увеличьте платежи!")

    if extra > 0:
        lines += ["", f"⚡ <b>Досрочный платёж {money(extra)}</b> → в «{escape(report.prioritized[0].name)}»"]
        if base.finished and report.with_extra.finished:
            lines.append(
                f"Экономия на процентах: <b>{money(report.interest_saved)}</b>\n"
                f"Срок сократится на {months_text(report.months_saved)} "
                f"(до {months_text(report.with_extra.months)})"
            )
        elif report.with_extra.finished:
            lines.append(f"Долги будут погашены за {months_text(report.with_extra.months)}.")
        else:
            lines.append("Даже с досрочным платежом долги не гасятся при текущих мин. платежах.")
    else:
        lines += ["", "💡 Рассчитать экономию от досрочного платежа: <code>/debts 10000</code>"]

    await message.answer("\n".join(lines))


@router.message(Command("del_debt"))
async def cmd_del_debt(message: Message, command: CommandObject) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Формат: <code>/del_debt id</code> (id смотрите в /debts)")
        return
    if await db.delete_debt(int(command.args)):
        await message.answer("🗑 Долг удалён.")
    else:
        await message.answer("Долг с таким id не найден.")


# ---------- /stats ----------

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    png, data = await build_month_chart()
    if png is None:
        await message.answer("В этом месяце расходов пока нет. Добавить: /add_expense")
        return
    total = sum(data.values())
    top = "\n".join(f"• {escape(cat)}: {money(val)}" for cat, val in list(data.items())[:5])
    await message.answer_photo(
        BufferedInputFile(png, filename="expenses.png"),
        caption=f"📊 Расходы за месяц: <b>{money(total)}</b>\n\nТоп категорий:\n{top}",
    )


# ---------- /report (банковские выписки) ----------

def _report_text(start: str) -> str | None:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        report = build_report(conn, start)
    finally:
        conn.close()
    return format_telegram(report) if report else None


@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject) -> None:
    start = default_start()
    if command.args:
        try:
            start = date.fromisoformat(command.args.strip()).isoformat()
        except ValueError:
            await message.answer("Формат: <code>/report [ГГГГ-ММ-ДД]</code>, например <code>/report 2026-04-01</code>")
            return
    text = await asyncio.to_thread(_report_text, start)
    if text is None:
        await message.answer(
            "Операций из выписок за этот период нет.\n"
            "Загрузите выписки на компьютере: <code>python import_statements.py папка_с_pdf</code>"
        )
        return
    await message.answer(text)


# ---------- /sheet (выгрузка в Google Таблицу) ----------

def _export_sheet() -> dict:
    spreadsheet = sheets_engine.get_spreadsheet()
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return sheets_export.export(conn, spreadsheet)
    finally:
        conn.close()


@router.message(Command("sheet"))
async def cmd_sheet(message: Message) -> None:
    if not sheets_engine.is_enabled():
        await message.answer("Google Таблица не подключена (нет credentials.json или GOOGLE_SPREADSHEET_NAME в .env).")
        return
    wait = await message.answer("Выгружаю операции в Google Таблицу…")
    try:
        r = await asyncio.to_thread(_export_sheet)
    except Exception as e:
        log.exception("Выгрузка в Google Таблицу не удалась")
        await wait.edit_text(f"Не получилось: {escape(type(e).__name__)}. Подробности в окне бота.")
        return
    await wait.edit_text(f"✅ Готово. Вкладка «{sheets_export.DETAIL_TAB}»: {r['detail_rows']} операций, "
                         f"вкладка «{sheets_export.SUMMARY_TAB}» обновлена.")


# ---------- /top и /recat (ручная правка категорий) ----------

_recat_sessions: dict[str, dict] = {}


class SplitOp(StatesGroup):
    parts = State()
_RC_PAGE = 8


def _db_call(fn, *args):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return fn(conn, *args)
    finally:
        conn.close()


def _rc_text(p: dict) -> str:
    ops, page = p["ops"], p["page"]
    pages = max(1, -(-len(ops) // _RC_PAGE))
    lines = [f"<b>«{escape(p['query'])}»</b> · операций: {len(ops)} · стр. {page + 1}/{pages}",
             "Отметьте операции кнопками с номерами, потом выберите действие.", ""]
    for i in range(page * _RC_PAGE, min(len(ops), (page + 1) * _RC_PAGE)):
        o = ops[i]
        mark = "✅" if i in p["sel"] else "▫️"
        plan = (" 📌" if o["planned"] else "") + (" ✂️" if recat.SPLIT_MARK in o["fingerprint"] else "")
        lines.append(f"{mark} {i + 1}. {o['date'][8:]}.{o['date'][5:7]} · {_ru(-o['kop'])} · "
                     f"{escape(o['category'])}{plan}")
    if p["sel"]:
        lines += ["", f"Отмечено: {len(p['sel'])} на {_ru(-sum(ops[i]['kop'] for i in p['sel']))}"]
    return "\n".join(lines)


def _rc_kb(token: str, p: dict) -> InlineKeyboardMarkup:
    ops, page = p["ops"], p["page"]
    idx = range(page * _RC_PAGE, min(len(ops), (page + 1) * _RC_PAGE))
    nums = [InlineKeyboardButton(text=("✅" if i in p["sel"] else "") + str(i + 1), callback_data=f"rc:{token}:t{i}") for i in idx]
    rows = [nums[j:j + 4] for j in range(0, len(nums), 4)]
    rows.append([InlineKeyboardButton(text="Все на странице", callback_data=f"rc:{token}:sa"),
                 InlineKeyboardButton(text="Все найденные", callback_data=f"rc:{token}:sall"),
                 InlineKeyboardButton(text="Сбросить", callback_data=f"rc:{token}:sx")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"rc:{token}:pp"))
    if (page + 1) * _RC_PAGE < len(ops):
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"rc:{token}:pn"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏷 Категория отмеченным", callback_data=f"rc:{token}:cm")])
    rows.append([InlineKeyboardButton(text="📌 Плановая трата вкл/выкл", callback_data=f"rc:{token}:pl")])
    rows.append([InlineKeyboardButton(text="✂️ Разбить отмеченную на части", callback_data=f"rc:{token}:sp"),
                 InlineKeyboardButton(text="↩ Отменить разбивку", callback_data=f"rc:{token}:us")])
    rows.append([InlineKeyboardButton(text="🏪 Правило для всех такого магазина", callback_data=f"rc:{token}:rm")])
    rows.append([InlineKeyboardButton(text="Закрыть", callback_data=f"rc:{token}:cl")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _rc_cat_kb(token: str) -> InlineKeyboardMarkup:
    cats = nz.all_categories()
    btns = [InlineKeyboardButton(text=c, callback_data=f"rc:{token}:k{k}") for k, c in enumerate(cats)]
    rows = [btns[j:j + 2] for j in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"rc:{token}:bk")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _open_recat(target: Message, user_id: int, query: str, edit: bool = False) -> None:
    ops = await asyncio.to_thread(_db_call, recat.find_ops, query)
    if not ops:
        text = f"Не нашла трат со словом «{escape(query)}» с {_d_iso(default_start())}."
        await (target.edit_text(text) if edit else target.answer(text))
        return
    token = uuid.uuid4().hex[:8]
    p = {"user": user_id, "query": query, "ops": ops, "sel": set(), "page": 0, "mode": "sel"}
    _recat_sessions[token] = p
    if edit:
        await target.edit_text(_rc_text(p), reply_markup=_rc_kb(token, p))
    else:
        await target.answer(_rc_text(p), reply_markup=_rc_kb(token, p))


def _d_iso(iso: str) -> str:
    return f"{iso[8:]}.{iso[5:7]}.{iso[:4]}"


@router.message(Command("recat"))
async def cmd_recat(message: Message, command: CommandObject) -> None:
    if not command.args or not command.args.strip():
        await message.answer("Напишите часть названия магазина, например <code>/recat озон</code>.\n"
                             "Названия, которые бот видит у вас в выписках, покажет команда /top.")
        return
    await _open_recat(message, message.from_user.id, command.args.strip())


_top_sessions: dict[str, dict] = {}


@router.message(Command("top"))
@router.message(Command("categories"))
async def cmd_top(message: Message, command: CommandObject) -> None:
    cat = (command.args or "").strip() or "Прочее"
    rows = await asyncio.to_thread(_db_call, recat.top_merchants, cat)
    if not rows:
        await message.answer(f"В категории «{escape(cat)}» нет трат. Названия категорий: "
                             + ", ".join(nz.all_categories()))
        return
    token = uuid.uuid4().hex[:8]
    _top_sessions[token] = {"user": message.from_user.id, "names": [m for m, _, _ in rows]}
    lines = [f"<b>«{escape(cat)}»: крупнейшие получатели с {_d_iso(default_start())}</b>", ""]
    lines += [f"{i + 1}. {escape(m[:40])} — {_ru(int(s * 100))} ({n} оп.)" for i, (m, s, n) in enumerate(rows)]
    lines += ["", "Нажмите номер, чтобы разобрать магазин."]
    btns = [InlineKeyboardButton(text=str(i + 1), callback_data=f"top:{token}:{i}") for i in range(len(rows))]
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[btns[j:j + 5] for j in range(0, len(btns), 5)]))


@router.callback_query(F.data.startswith("top:"))
async def on_top_button(cb: CallbackQuery) -> None:
    if _allowed and cb.from_user.id not in _allowed:
        await cb.answer()
        return
    _, token, i = cb.data.split(":")
    p = _top_sessions.get(token)
    if not p or p["user"] != cb.from_user.id:
        await cb.answer("Список устарел, вызовите /top ещё раз.", show_alert=True)
        return
    await cb.answer()
    await _open_recat(cb.message, cb.from_user.id, p["names"][int(i)])


@router.message(SplitOp.parts, F.text, ~F.text.startswith("/"))
async def split_parts(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    p = _recat_sessions.get(data.get("token", ""))
    if not p or p["user"] != message.from_user.id:
        await state.clear()
        await message.answer("Список устарел. Откройте /recat ещё раз.")
        return
    op = p["ops"][data["i"]]
    try:
        parts = recat.parse_split(message.text, -op["kop"], nz.all_categories(), stcfg.load().get("category_aliases", {}))
        await asyncio.to_thread(_db_call, recat.split, op["fingerprint"], parts)
    except ValueError as e:
        await message.answer(f"⚠️ {escape(str(e))}\nПопробуйте ещё раз или /cancel.")
        return
    await state.clear()
    _recat_sessions.pop(data["token"], None)
    await message.answer("✂️ Готово: " + " + ".join(f"{escape(c)} {_ru(k)}" for c, k in parts)
                         + ". Общая сумма не изменилась. Чтобы увидеть это в таблице, отправьте /sheet.")
    await _open_recat(message, message.from_user.id, p["query"])


def _rc_apply(kind: str, fps: list[str], arg: str) -> int:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        if kind == "cat":
            return recat.set_category(conn, fps, arg)
        if kind == "rule":
            return recat.add_rule(conn, arg[0], arg[1])
        return recat.set_planned(conn, fps, arg)
    finally:
        conn.close()


@router.callback_query(F.data.startswith("rc:"))
async def on_recat_button(cb: CallbackQuery, state: FSMContext) -> None:
    if _allowed and cb.from_user.id not in _allowed:
        await cb.answer()
        return
    _, token, action = cb.data.split(":", 2)
    p = _recat_sessions.get(token)
    if not p or p["user"] != cb.from_user.id:
        await cb.answer("Список устарел, вызовите команду ещё раз.", show_alert=True)
        return
    ops = p["ops"]
    page_idx = range(p["page"] * _RC_PAGE, min(len(ops), (p["page"] + 1) * _RC_PAGE))

    async def refresh() -> None:
        await cb.message.edit_text(_rc_text(p), reply_markup=_rc_kb(token, p))

    if action == "cl":
        _recat_sessions.pop(token, None)
        await cb.message.edit_text("Готово. Чтобы увидеть изменения в таблице, отправьте /sheet; в /report они уже учтены.")
    elif action[0] == "t" and action[1:].isdigit():
        p["sel"] ^= {int(action[1:])}
        await refresh()
    elif action == "sa":
        p["sel"] |= set(page_idx)
        await refresh()
    elif action == "sall":
        p["sel"] = set(range(len(ops)))
        await refresh()
    elif action == "sx":
        p["sel"] = set()
        await refresh()
    elif action == "pp":
        p["page"] = max(0, p["page"] - 1)
        await refresh()
    elif action == "pn":
        p["page"] += 1
        await refresh()
    elif action == "bk":
        await refresh()
    elif action in ("cm", "rm"):
        if action == "cm" and not p["sel"]:
            await cb.answer("Сначала отметьте операции номерами.", show_alert=True)
            return
        p["mode"] = "sel" if action == "cm" else "rule"
        title = (f"Категория для {len(p['sel'])} отмеченных операций:" if action == "cm"
                 else f"Категория для ВСЕХ трат со словом «{escape(p['query'])}» (и будущих):")
        await cb.message.edit_text(title, reply_markup=_rc_cat_kb(token))
    elif action[0] == "k" and action[1:].isdigit():
        cat = nz.all_categories()[int(action[1:])]
        if p["mode"] == "rule":
            n = await asyncio.to_thread(_rc_apply, "rule", [], (p["query"], cat))
            await cb.message.edit_text(f"✅ Правило сохранено: всё со словом «{escape(p['query'])}» → {escape(cat)}. "
                                       f"Изменено операций: {n}.")
            _recat_sessions.pop(token, None)
            await cb.answer()
            return
        fps = [ops[i]["fingerprint"] for i in sorted(p["sel"])]
        await asyncio.to_thread(_rc_apply, "cat", fps, cat)
        for i in p["sel"]:
            ops[i]["category"] = cat
        p["sel"] = set()
        await refresh()
        await cb.answer(f"Готово: {len(fps)} → {cat}")
        return
    elif action == "sp":
        if len(p["sel"]) != 1:
            await cb.answer("Отметьте ровно одну операцию, которую хотите разбить.", show_alert=True)
            return
        i = next(iter(p["sel"]))
        if ops[i]["kop"] >= 0:
            await cb.answer("Разбить можно только трату, а не возврат.", show_alert=True)
            return
        await state.set_state(SplitOp.parts)
        await state.update_data(token=token, i=i)
        await cb.message.answer(
            f"Разбиваем: {ops[i]['date'][8:]}.{ops[i]['date'][5:7]} · {escape(ops[i]['merchant'])} · <b>{_ru(-ops[i]['kop'])}</b>\n\n"
            "Напишите части одним сообщением: сумма и категория через запятую. Последнюю сумму можно не писать — "
            "она возьмёт остаток.\nНапример:\n<code>2000 Красота, 3000 Подарки</code>\nили <code>2000 Красота, Подарки</code>\n"
            "Категории: " + ", ".join(nz.all_categories()) + "\n\nОтмена: /cancel")
    elif action == "us":
        if not p["sel"]:
            await cb.answer("Отметьте части разбивки (они помечены ✂️).", show_alert=True)
            return
        fps = [ops[i]["fingerprint"] for i in sorted(p["sel"]) if recat.SPLIT_MARK in ops[i]["fingerprint"]]
        if not fps:
            await cb.answer("У отмеченных операций нет разбивки.", show_alert=True)
            return
        for fp in fps:
            await asyncio.to_thread(_db_call, recat.unsplit, fp)
        _recat_sessions.pop(token, None)
        await cb.answer("Разбивка отменена")
        await _open_recat(cb.message, cb.from_user.id, p["query"], edit=True)
        return
    elif action == "pl":
        if not p["sel"]:
            await cb.answer("Сначала отметьте операции номерами.", show_alert=True)
            return
        sel = sorted(p["sel"])
        mark = not all(ops[i]["planned"] for i in sel)       # если все уже плановые — снимаем пометку
        await asyncio.to_thread(_rc_apply, "plan", [ops[i]["fingerprint"] for i in sel], mark)
        for i in sel:
            ops[i]["planned"] = mark
        p["sel"] = set()
        await refresh()
        await cb.answer("Помечено как плановая трата" if mark else "Пометка снята")
        return
    await cb.answer()


# ---------- /advice (комментарии и рекомендации) ----------

def _advice_parts(start: str) -> tuple[list[str], dict] | None:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        ins = insights.build_insights(conn, start)
    finally:
        conn.close()
    if not ins:
        return None
    return insights.rule_comments(ins), insights.ai_summary(ins)


@router.message(Command("advice"))
async def cmd_advice(message: Message, command: CommandObject) -> None:
    start = default_start()
    if command.args:
        try:
            start = date.fromisoformat(command.args.strip()).isoformat()
        except ValueError:
            await message.answer("Формат: <code>/advice [ГГГГ-ММ-ДД]</code>")
            return
    parts = await asyncio.to_thread(_advice_parts, start)
    if parts is None:
        await message.answer("Нужен хотя бы один полный месяц операций из выписок (см. /report).")
        return
    comments, summary = parts
    await message.answer("<b>Наблюдения</b>\n" + "\n".join("• " + escape(c) for c in comments))
    if not os.getenv("GEMINI_API_KEY"):
        await message.answer("Совет от нейросети выключен: добавьте GEMINI_API_KEY в .env (см. README).")
        return
    wait = await message.answer("Спрашиваю Gemini (отправляю только суммы по категориям)…")
    try:
        text = await asyncio.to_thread(insights.ai_advice, summary)
    except RuntimeError as e:
        await wait.edit_text(escape(str(e)))
        return
    await wait.edit_text("<b>Совет нейросети</b>\n" + escape(text))


# ---------- Скриншоты истории операций ----------

_pending_shots: dict[str, dict] = {}       # token -> {user, txns, dupes, removed}
TYPE_LABEL = {"expense": "расход", "refund": "возврат", "income": "доход", "transfer": "между своими",
              "p2p_out": "перевод людям", "p2p_in": "перевод от людей", "family_out": "семье", "family_in": "от семьи"}


def _ru(kop: int) -> str:
    return f"{kop / 100:,.2f}".replace(",", " ").replace(".", ",").replace(",00", "") + " ₽"


def _shot_text(p: dict) -> str:
    lines = ["<b>Нашла на скриншоте</b> (проверьте и нажмите «Сохранить»):", ""]
    for i, t in enumerate(p["txns"]):
        mark = "♻️" if t.fingerprint in p["dupes"] else ("➖" if i in p["removed"] else "✅")
        what = t.category or TYPE_LABEL.get(t.type, t.type)
        lines.append(f"{mark} {i + 1}. {t.date:%d.%m} · {escape(t.merchant)} · <b>{_ru(t.amount_kop)}</b> · {escape(what)}")
    if p["dupes"]:
        lines += ["", "♻️ — уже есть в базе из выписки, не сохраняю."]
    lines += ["", "Чтобы убрать лишнюю строку, нажмите её номер."]
    return "\n".join(lines)


def _shot_kb(token: str, p: dict) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, t in enumerate(p["txns"]):
        if t.fingerprint in p["dupes"]:
            continue
        row.append(InlineKeyboardButton(text=("➖" if i in p["removed"] else "✅") + str(i + 1),
                                        callback_data=f"shot:{token}:x{i}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🏷 Категория", callback_data=f"shot:{token}:cm")])
    rows.append([InlineKeyboardButton(text="💾 Сохранить", callback_data=f"shot:{token}:save"),
                 InlineKeyboardButton(text="✖ Отмена", callback_data=f"shot:{token}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pick_row_kb(token: str, p: dict) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(text=str(i + 1), callback_data=f"shot:{token}:c{i}")
            for i, t in enumerate(p["txns"]) if t.fingerprint not in p["dupes"]]
    rows = [btns[i:i + 6] for i in range(0, len(btns), 6)]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"shot:{token}:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pick_cat_kb(token: str, i: int) -> InlineKeyboardMarkup:
    cats = nz.all_categories()
    btns = [InlineKeyboardButton(text=c, callback_data=f"shot:{token}:s{i}-{k}") for k, c in enumerate(cats)]
    btns.append(InlineKeyboardButton(text="↔ Свой перевод", callback_data=f"shot:{token}:s{i}-{len(cats)}"))
    rows = [btns[j:j + 2] for j in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"shot:{token}:cm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _apply_category(t, k: int) -> None:
    cats = nz.all_categories()
    if k >= len(cats):                      # «Свой перевод»: не расход и не доход
        t.type, t.category = "transfer", ""
        return
    t.category = cats[k]
    if t.type in ("transfer", "p2p_out", "family_out", "unknown"):
        t.type = "expense"
    elif t.type in ("p2p_in", "family_in"):
        t.type = "income"


def _process_shot(path: str) -> tuple[list, set]:
    txns = screenshot.read_screenshot(path, date.today())
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return txns, screenshot.find_statement_dupes(conn, txns)
    finally:
        conn.close()


def _is_image_doc(message: Message) -> bool:
    return bool(message.document and (message.document.mime_type or "").startswith("image/"))


@router.message(F.photo | F.func(_is_image_doc))
async def on_screenshot(message: Message, bot: Bot) -> None:
    file = message.photo[-1] if message.photo else message.document
    buf = io.BytesIO()
    await bot.download(file, destination=buf)
    wait = await message.answer("Читаю скриншот…")
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/shot.jpg"
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        try:
            txns, dupes = await asyncio.to_thread(_process_shot, path)
        except RuntimeError as e:
            await wait.edit_text(escape(str(e)))
            return
    if not txns:
        await wait.edit_text("Не нашла операций. Нужен скриншот экрана «История» с датами и суммами. "
                             "Пришлите его как файл («без сжатия»), так текст читается лучше.")
        return
    token = uuid.uuid4().hex[:8]
    p = {"user": message.from_user.id, "txns": txns, "dupes": dupes, "removed": set()}
    _pending_shots[token] = p
    await wait.edit_text(_shot_text(p), reply_markup=_shot_kb(token, p))


def _save_shot(p: dict) -> list:
    keep = [t for i, t in enumerate(p["txns"]) if i not in p["removed"] and t.fingerprint not in p["dupes"]]
    conn = sqlite3.connect(db.DB_PATH)
    try:
        write_txns(conn, keep)
    finally:
        conn.close()
    return keep


SHEET_TYPES = {"expense": "expense", "income": "income", "refund": "Возврат", "p2p_out": "Перевод людям",
               "p2p_in": "Перевод от людей", "family_out": "Перевод семье", "family_in": "Перевод от семьи"}


@router.callback_query(F.data.startswith("shot:"))
async def on_shot_button(cb: CallbackQuery) -> None:
    if _allowed and cb.from_user.id not in _allowed:
        await cb.answer()
        return
    _, token, action = cb.data.split(":", 2)
    p = _pending_shots.get(token)
    if not p or p["user"] != cb.from_user.id:
        await cb.answer("Этот список уже закрыт. Пришлите скриншот заново.", show_alert=True)
        return
    if action == "cancel":
        _pending_shots.pop(token, None)
        await cb.message.edit_text("Отменено, ничего не сохранено.")
    elif action == "save":
        _pending_shots.pop(token, None)
        keep = await asyncio.to_thread(_save_shot, p)
        for t in keep:
            if t.type in SHEET_TYPES:
                sheets_engine.schedule_append(t.date, cb.from_user.id, t.category or TYPE_LABEL.get(t.type, ""),
                                              abs(t.amount_kop) / 100, f"{t.merchant} (скриншот)", SHEET_TYPES[t.type])
        await cb.message.edit_text(f"Сохранено операций: {len(keep)}."
                                   + (" Записала и в Google Таблицу." if sheets_engine.is_enabled() else ""))
    elif action == "cm":
        await cb.message.edit_text(_shot_text(p) + "\n\n<b>У какой строки сменить категорию?</b>",
                                   reply_markup=_pick_row_kb(token, p))
    elif action == "back":
        await cb.message.edit_text(_shot_text(p), reply_markup=_shot_kb(token, p))
    elif action.startswith("c") and action[1:].isdigit():
        i = int(action[1:])
        t = p["txns"][i]
        await cb.message.edit_text(f"{i + 1}. {escape(t.merchant)} · <b>{_ru(t.amount_kop)}</b>\nВыберите категорию:",
                                   reply_markup=_pick_cat_kb(token, i))
    elif action.startswith("s") and "-" in action:
        i, k = (int(x) for x in action[1:].split("-"))
        _apply_category(p["txns"][i], k)
        await cb.message.edit_text(_shot_text(p), reply_markup=_shot_kb(token, p))
    elif action.startswith("x"):
        i = int(action[1:])
        p["removed"] ^= {i}
        await cb.message.edit_text(_shot_text(p), reply_markup=_shot_kb(token, p))
    await cb.answer()


# ---------- Запуск ----------

async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN. Скопируйте .env.example в .env и укажите токен.")

    await db.init_db()
    moved = await asyncio.to_thread(_sync_manual)
    if moved:
        log.info("Ручные траты скопированы в общую таблицу: %s", moved)
    if sheets_engine.is_enabled():
        log.info("Google Sheets: запись в таблицу «%s» включена", os.getenv("GOOGLE_SPREADSHEET_NAME"))
    else:
        log.info("Google Sheets выключен (нет GOOGLE_SPREADSHEET_NAME или credentials.json)")

    # Если Telegram не открывается напрямую, укажите прокси в .env: TELEGRAM_PROXY=socks5://127.0.0.1:1080
    proxy = os.getenv("TELEGRAM_PROXY") or None
    session = AiohttpSession(proxy=proxy) if proxy else AiohttpSession()
    if proxy:
        log.info("Telegram: работаю через прокси")
    bot = Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    commands = [
        BotCommand(command="start", description="Приветствие и меню"),
        BotCommand(command="add_expense", description="Записать расход"),
        BotCommand(command="stats", description="Диаграмма расходов за месяц"),
        BotCommand(command="report", description="Отчёт по банковским выпискам"),
        BotCommand(command="top", description="Магазины в категории (по умолчанию «Прочее»)"),
        BotCommand(command="recat", description="Поправить категории у магазина"),
        BotCommand(command="sheet", description="Выгрузить операции в Google Таблицу"),
        BotCommand(command="advice", description="Комментарии и советы по тратам"),
        BotCommand(command="add_debt", description="Добавить долг"),
        BotCommand(command="debts", description="Долги и план «Лавина»"),
        BotCommand(command="cancel", description="Отменить диалог"),
    ]

    async def publish_commands() -> None:
        """Меню команд. Если Telegram пока недоступен, повторяем, а бот не падает."""
        while True:
            try:
                await bot.set_my_commands(commands)
                log.info("Меню команд обновлено")
                return
            except TelegramUnauthorizedError:
                raise
            except Exception as e:  # сеть, VPN, прокси: ждём и пробуем снова
                log.warning("Telegram пока недоступен (%s), повторю через 30 секунд. Проверьте VPN или TELEGRAM_PROXY",
                            type(e).__name__)
                await asyncio.sleep(30)

    commands_task = asyncio.create_task(publish_commands())

    log.info("Бот запущен")
    try:
        while True:
            try:
                await dp.start_polling(bot)
                break
            except TelegramUnauthorizedError:
                raise RuntimeError("Telegram не принял BOT_TOKEN. Проверьте токен в .env.") from None
            except Exception as e:
                log.warning("Нет связи с Telegram (%s), повторю через 30 секунд. Проверьте VPN или TELEGRAM_PROXY",
                            type(e).__name__)
                await asyncio.sleep(30)
    finally:
        commands_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
