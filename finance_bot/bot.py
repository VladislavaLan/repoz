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
from datetime import date, datetime, timedelta
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
from statements import edit_sheet, insights, manual, planner, recat, screenshot, sheets_export  # noqa: E402
from statements import summary as st_summary  # noqa: E402
from statements import category_dive as st_category  # noqa: E402
from statements import week as st_week  # noqa: E402
from statements import budget as st_budget  # noqa: E402
from statements import voice as st_voice  # noqa: E402
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


def parse_user_date(text: str) -> date:
    """Дата в любом привычном виде: 2026-09-01, 01.09.2026, 1.9.26, 01/09/2026 или просто 01.09 (текущий год).
    ValueError, если не получилось."""
    t = text.strip().replace("/", ".").replace("-", ".").replace(" ", ".")
    parts = [p for p in t.split(".") if p]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValueError("не дата") from None
    if len(nums) == 3 and len(parts[0]) == 4:          # ГГГГ-ММ-ДД
        return date(nums[0], nums[1], nums[2])
    if len(nums) == 3:                                  # ДД.ММ.ГГГГ или ДД.ММ.ГГ
        y = nums[2] + 2000 if nums[2] < 100 else nums[2]
        return date(y, nums[1], nums[0])
    if len(nums) == 2:                                  # ДД.ММ — текущий год
        return date(date.today().year, nums[1], nums[0])
    raise ValueError("не дата")


_MONTH_STEMS = ["январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]


def _month_of(token: str) -> int | None:
    t = token.strip(".,").lower()
    if t in ("май", "мая"):
        return 5
    for i, stem in enumerate(_MONTH_STEMS, 1):
        if i != 5 and len(t) >= 3 and t.startswith(stem[:max(3, min(len(stem), len(t)))]) and t.startswith(stem[:3]):
            return i
    return None


def parse_period(text: str) -> tuple[date, date | None]:
    """Период для команд: «сентябрь», «сентябрь октябрь», «09.2026», «01.09.2026 30.09.2026», «с 1.9 по 30.9».
    Возвращает (с, по); «по» = None, если указана только начальная дата. ValueError, если не поняла."""
    import calendar
    import re
    t = text.strip().lower()
    today = date.today()
    # 1) месяцы словами (с годом или без)
    words = re.findall(r"[а-яё]+|\d{4}", t)
    months = [(_month_of(w), w) for w in words if not w.isdigit()]
    months = [m for m, _ in months if m]
    years = [int(w) for w in words if w.isdigit() and len(w) == 4]
    if months:
        def year_for(m: int) -> int:
            return years[0] if years else (today.year if m <= today.month else today.year - 1)
        first, last = months[0], months[-1]
        y1, y2 = year_for(first), year_for(last)
        if len(years) > 1:
            y2 = years[-1]
        elif last < first:
            y2 = y1 + 1
        return date(y1, first, 1), date(y2, last, calendar.monthrange(y2, last)[1])
    # 2) месяц числами: 09.2026 или 2026-09
    m = re.fullmatch(r"(\d{1,2})[./-](\d{4})", t) or None
    if m:
        mo, y = int(m[1]), int(m[2])
        return date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
    m = re.fullmatch(r"(\d{4})[./-](\d{1,2})", t)
    if m:
        y, mo = int(m[1]), int(m[2])
        return date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
    # 3) одна или две даты
    found = re.findall(r"\d{1,4}[./-]\d{1,2}(?:[./-]\d{1,4})?", t)
    if not found:
        raise ValueError("не период")
    dates = sorted(parse_user_date(x) for x in found[:2])
    return (dates[0], dates[1]) if len(dates) == 2 else (dates[0], None)


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
    "/report <code>[дата, напр. 01.04.2026]</code> — отчёт по загруженным банковским выпискам\n"
    "📷 Пришлите скриншот истории операций из банка — я распознаю его и предложу сохранить\n"
    "/top <code>[категория]</code> — какие магазины в категории (без названия — в «Прочее»)\n"
    "/edit_sheet <code>[сентябрь | 01.09.2026 30.09.2026]</code> — вкладка в Google Таблице для массовой правки категорий; /apply_sheet — применить правки\n"
    "/add_category <code>[название]</code> — добавить свою категорию (убрать: /del_category)\n"
    "/recat <code>озон</code> — поправить категории у операций магазина, отметить плановые траты\n"
    "/sheet — выгрузить операции из выписок в Google Таблицу (вкладки «Выписки» и «Итоги по месяцам»)\n"
    "/advice <code>[дата]</code> — комментарии и советы по тратам (с ключом Gemini — и от нейросети)\n\n"
    "<b>Долги</b>\n"
    "/add_debt — добавить долг (пошаговый диалог)\n"
    "/debts — список долгов и порядок погашения по «Лавине»\n"
    "/debts <code>сумма</code> — плюс расчёт экономии от досрочного платежа\n"
    "/debt_edit <code>id поле значение</code> — поменять остаток, ставку, льготу, лимит, платёж\n"
    "/del_debt <code>id</code> — удалить долг\n\n"
    "<b>План денег</b>\n"
    "/balance <code>сумма</code> — сколько денег сейчас · /reserve <code>сумма</code> — резерв · /living <code>сумма</code> — обычные траты в месяц\n"
    "/plan_add <code>23.10.2026 +90000 Аванс</code> — приход или трата в плане · /plan_list · /plan_del <code>id</code>\n"
    "/plan <code>[до даты]</code> — прогноз по дням с кредитками и дедлайнами\n"
    "🎤 После скриншота пришлите голосовое («первая и третья — продукты, остальное — кафе»): бот проставит категории (нужен Gemini и VPN)\n"
    "/budget — лимиты по категориям на месяц: <code>/budget авто</code>, <code>/budget Продукты 25000</code>\n"
    "/today — сколько можно потратить сегодня в рамках лимитов\n"
    "/week — траты за 7 дней по категориям: где выше обычного, где в норме\n"
    "/cat <code>категория [период]</code> — разбор категории: покупки, средний чек, магазины, рост из-за цен или привычек\n"
    "/summary — компактная сводка по месяцам и категориям (можно показать ассистенту)\n"
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


# ---------- Команды, которые ждут ввода ----------
# Из меню Telegram команда уходит сразу, без аргументов. Если ей нужно значение, бот спрашивает его
# и следующее сообщение обрабатывает так, как будто оно написано после команды.

class AwaitArgs(StatesGroup):
    text = State()


_ASK = {
    "balance": "Сколько денег на счетах и картах сейчас (без кредитных)? Напишите число, например <code>21888</code>.",
    "reserve": "Сколько оставлять на счёте, а не нести на кредитку? Например <code>30000</code>.",
    "living": "Сколько вы тратите в месяц на жизнь (без ЭКО и крупных разовых)? Число, например <code>110000</code>, "
              "или слово «авто», чтобы считать по выпискам.",
    "plan_add": "Одной строкой: дата, сумма со знаком, название. Например:\n<code>23.10.2026 +90000 Аванс</code>\n"
                "Можно добавить слова «ежемесячно», «рабочий», «приоритет 2».",
    "plan_del": "Номер пункта, который удалить (номера в списке выше).",
    "debt_edit": "Одной строкой: номер долга, поле, значение. Например: <code>2 срок 12.11.2026</code>.\n"
                 "Поля: остаток, ставка, платёж, льгота, лимит, срок, карта, название. Номера смотрите в /debts.",
    "del_debt": "Номер долга, который удалить (номера в /debts).",
    "cat": "Категория и, если нужно, период. Например: <code>Продукты сентябрь октябрь</code> или просто "
           "<code>Продукты</code> (тогда последние 3 месяца).",
    "del_category": "Название своей категории, которую убрать. Операции с ней останутся как есть, исчезнет только кнопка.",
    "budget_set": "",
}


async def _ask_args(message: Message, state: FSMContext, name: str) -> None:
    await state.set_state(AwaitArgs.text)
    await state.update_data(cmd=name)
    await message.answer(_ASK[name] + "\nОтмена: /cancel")


@router.message(AwaitArgs.text, F.text, ~F.text.startswith("/"))
async def on_await_args(message: Message, state: FSMContext) -> None:
    name = (await state.get_data()).get("cmd")
    await state.clear()
    handler = _AWAIT_HANDLERS.get(name)
    if not handler:
        return
    cmd = CommandObject(prefix="/", command=name, mention=None, args=message.text.strip())
    await handler(message, cmd, state)


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
    try:
        warns = await asyncio.to_thread(_db_call, st_budget.warnings)
    except Exception:  # предупреждения не должны ломать запись траты
        log.exception("Не удалось проверить лимиты")
        warns = []
    if warns:
        text += "\n\n" + "\n".join(escape(w) for w in warns)
    return text


class AddExpense(StatesGroup):
    amount = State()
    category = State()
    description = State()


DEFAULT_CATEGORIES = ["Продукты", "Кафе и рестораны", "Транспорт", "Прочие товары", "Здоровье", "Развлечения",
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
    await message.answer("<b>Шаг 4/5.</b> Минимальный ежемесячный платёж в рублях (<code>0</code>, если банк пока не выставил):")


@router.message(AddDebt.min_payment, F.text)
async def debt_min_payment(message: Message, state: FSMContext) -> None:
    try:
        raw = message.text.strip()
        min_payment = 0.0 if raw in ("-", "—", "нет", "Нет") else parse_number(raw)
        if min_payment < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите число, например <code>5000</code>, или <code>0</code>, если платежа пока нет.")
        return

    data = await state.get_data()
    monthly_interest = data["amount"] * data["rate"] / 100 / 12
    warning = ""
    if 0 < min_payment <= monthly_interest:
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

def _interest_hints() -> dict[str, float]:
    """Средние проценты в месяц по источникам из выписок (для оценки ставки)."""
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return st_summary.interest_by_source(conn, default_start())
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


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

    today = date.today()
    hints = await asyncio.to_thread(_interest_hints)
    lines = ["💳 <b>Долги и кредитки</b>", ""]
    for d in sorted(debts, key=lambda x: (x.grace_end_date or date.max, -x.interest_rate)):
        head = f"<b>{escape(d.name)}</b> (id {d.id}): {money(d.total_amount)}"
        if d.limit_amount:
            head += f" из {money(d.limit_amount)}, свободно {money(max(d.limit_amount - d.total_amount, 0))}"
        lines.append(head)
        rate = f"{d.interest_rate:g}%" if d.interest_rate else "ставка не задана"
        pay = f"мин. платёж {money(d.min_payment)}" if d.min_payment else "мин. платёж не выставлен"
        if d.due_date:
            pay += f" до {d.due_date:%d.%m.%Y}"
        lines.append(f"    {rate} · {pay}")
        monthly = d.total_amount * d.interest_rate / 100 / 12
        if d.interest_rate and d.total_amount:
            lines.append(f"    стоимость долга: ≈{money(monthly)} в месяц, ≈{money(monthly * 12 / 365)} в день")
        if d.grace_end_date and d.grace_end_date >= today:
            days = (d.grace_end_date - today).days
            months_left = max(days / 30.4, 0.1)
            lines.append(
                f"    🕊 без процентов до {d.grace_end_date:%d.%m.%Y} (осталось {days} дн.)\n"
                f"    чтобы успеть: ≈{money(d.total_amount / months_left)} в месяц"
            )
            if d.interest_rate and days <= 60 and d.total_amount > 0:
                lines.append("    ⚠️ дедлайн близко, закройте долг полностью или проценты могут начислить за весь период")
        elif d.grace_end_date:
            lines.append("    ⛔ льготный период закончился")
        if d.card:
            for src, v in hints.items():
                if src.endswith(d.card) and v > 0 and d.total_amount:
                    lines.append(f"    по выпискам проценты ≈{money(v)} в месяц → ≈{v * 12 / d.total_amount * 100:.1f}% годовых")
        lines.append("")

    total_debt = sum(d.total_amount for d in debts)
    total_min = sum(d.min_payment for d in debts)
    lines += [f"💰 Всего долгов: <b>{money(total_debt)}</b> · обязательные платежи {money(total_min)}"]

    by_rate = sorted(debts, key=lambda x: -x.interest_rate)
    if len(by_rate) > 1:
        order = " → ".join(f"{escape(d.name)} ({d.interest_rate:g}%)" for d in by_rate)
        lines += ["", f"🏔 Приоритет по ставке: {order}"]
        top = by_rate[0]
        if top.grace_end_date and top.grace_end_date >= today:
            lines.append(f"Пока у «{escape(top.name)}» льгота до {top.grace_end_date:%d.%m.%Y}, остальные платите по минимуму, "
                         "свободные деньги направляйте в неё.")
    paying = [d for d in debts if d.min_payment > 0]
    if paying:
        report = avalanche_savings(paying, extra)
        base = report.baseline
        names = ", ".join(escape(d.name) for d in paying)
        if base.finished:
            lines.append(f"⏳ Только по долгам с платежом ({names}), если платить минимум: {months_text(base.months)}, "
                         f"переплата {money(base.total_interest)}")
        else:
            lines.append(f"⚠️ При минимальных платежах ({names}) долг не гасится — увеличьте платежи.")
        if extra > 0 and report.with_extra.finished:
            lines.append(f"⚡ С досрочным {money(extra)}: экономия {money(report.interest_saved)}, срок −{months_text(report.months_saved)}")
    lines += ["", "Изменить данные: /debt_edit · план денег и сценарии: /plan"]
    await message.answer("\n".join(lines))


_DEBT_FIELD_ALIAS = {
    "остаток": "total_amount", "долг": "total_amount", "balance": "total_amount",
    "ставка": "interest_rate", "rate": "interest_rate",
    "платёж": "min_payment", "платеж": "min_payment", "min": "min_payment",
    "льгота": "grace_end_date", "grace": "grace_end_date",
    "лимит": "limit_amount", "limit": "limit_amount",
    "срок": "due_date", "due": "due_date",
    "карта": "card", "card": "card",
    "название": "name", "name": "name",
}
_DEBT_EDIT_HELP = (
    "Формат: <code>/debt_edit id поле значение</code>\n\n"
    "Поля: остаток, ставка, платёж, льгота (дата), лимит, срок (дата платежа), карта (4 цифры), название.\n"
    "Примеры:\n<code>/debt_edit 1 остаток 151381</code>\n<code>/debt_edit 1 льгота 31.01.2027</code>\n"
    "<code>/debt_edit 2 платёж 24249,72</code>\n<code>/debt_edit 2 срок 09.11.2026</code>\n"
    "Дата отключается значением <code>-</code>. id смотрите в /debts."
)


@router.message(Command("debt_edit"))
async def cmd_debt_edit(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not (command.args or "").strip():
        await _ask_args(message, state, "debt_edit")
        return
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[0].isdigit():
        await message.answer(_DEBT_EDIT_HELP)
        return
    field = _DEBT_FIELD_ALIAS.get(parts[1].lower())
    if not field:
        await message.answer("Не знаю такого поля.\n\n" + _DEBT_EDIT_HELP)
        return
    raw = parts[2].strip()
    try:
        if field in ("grace_end_date", "due_date"):
            value = None if raw in ("-", "—", "нет") else parse_user_date(raw).isoformat()
        elif field in ("card", "name"):
            value = raw[:100]
        else:
            value = parse_number(raw)
            if value < 0:
                raise ValueError
    except ValueError:
        await message.answer("Не понял значение. Число или дата вида 31.01.2027.\n\n" + _DEBT_EDIT_HELP)
        return
    if await db.update_debt(int(parts[0]), **{field: value}):
        await message.answer("✅ Обновлено. Смотреть: /debts")
    else:
        await message.answer("Долг с таким id не найден.")


@router.message(Command("del_debt"))
async def cmd_del_debt(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not (command.args or "").strip():
        await _ask_args(message, state, "del_debt")
        return
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
    await asyncio.to_thread(_sync_manual)
    png, data = await build_month_chart()
    if png is None:
        await message.answer("В этом месяце расходов пока нет. Загрузите выписки или добавьте трату: /add_expense")
        return
    total = sum(data.values())
    top = "\n".join(f"• {escape(cat)}: {money(val)}" for cat, val in list(data.items())[:5])
    await message.answer_photo(
        BufferedInputFile(png, filename="expenses.png"),
        caption=f"📊 Расходы за месяц: <b>{money(total)}</b>\n\nТоп категорий:\n{top}",
    )


# ---------- /plan, /balance, /summary ----------

def _plan_data():
    conn = sqlite3.connect(db.DB_PATH)
    try:
        planner.ensure(conn)
        report = build_report(conn, default_start())
        living = planner.get_setting(conn, "living")
        daily = living / 30.4 if living else planner.baseline_daily(report)
        return (planner.list_items(conn), planner.get_setting(conn, "balance", 0.0),
                planner.get_setting(conn, "reserve", planner.DEFAULT_RESERVE), daily)
    finally:
        conn.close()


def _plan_write(fn, *args):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        planner.ensure(conn)
        return fn(conn, *args)
    finally:
        conn.close()


_PLAN_HELP = (
    "<b>План денег</b>\n"
    "1. Остаток на счетах сейчас: <code>/balance 21888</code>\n"
    "2. Приходы и траты: <code>/plan_add 23.10.2026 +90000 Аванс</code>, "
    "<code>/plan_add 06.11.2026 -100000 ЭКО</code>\n"
    "   добавь «ежемесячно» для повторов, «рабочий» (если выходной или праздник — раньше, в рабочий день), «приоритет 2» (1 неизбежно, 2 можно сдвинуть, 3 по желанию)\n"
    "3. Резерв, который не отдавать на кредитку: <code>/reserve 30000</code>\n"
    "   Свои обычные траты в месяц: <code>/living 110000</code> (вернуть авто: <code>/living авто</code>)\n"
    "4. Прогноз: <code>/plan</code> или <code>/plan 31.01.2027</code> (по какую дату считать)\n"
    "Убрать пункт: <code>/plan_del id</code>"
)


@router.message(Command("balance"))
async def cmd_balance(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not (command.args or "").strip():
        await _ask_args(message, state, "balance")
        return
    try:
        value = parse_number(command.args or "")
    except ValueError:
        await message.answer("Формат: <code>/balance 21888</code> — сколько денег на счетах и картах сейчас (без кредитных).")
        return
    await asyncio.to_thread(_plan_write, planner.set_setting, "balance", value)
    await message.answer(f"✅ Остаток: {money(value)}. Прогноз: /plan")


@router.message(Command("reserve"))
async def cmd_reserve(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not (command.args or "").strip():
        await _ask_args(message, state, "reserve")
        return
    try:
        value = parse_number(command.args or "")
    except ValueError:
        await message.answer("Формат: <code>/reserve 30000</code> — сколько оставлять на счёте, а не нести на кредитку.")
        return
    await asyncio.to_thread(_plan_write, planner.set_setting, "reserve", value)
    await message.answer(f"✅ Резерв: {money(value)}. Прогноз: /plan")


@router.message(Command("living"))
async def cmd_living(message: Message, command: CommandObject, state: FSMContext) -> None:
    raw = (command.args or "").strip().lower()
    if not raw:
        await _ask_args(message, state, "living")
        return
    if raw in ("авто", "auto", "-"):
        def reset(conn):
            conn.execute("DELETE FROM plan_settings WHERE key = 'living'")
            conn.commit()
        await asyncio.to_thread(_plan_write, reset)
        await message.answer("✅ Обычные траты снова считаются автоматически по выпискам. Прогноз: /plan")
        return
    try:
        value = parse_number(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Формат: <code>/living 110000</code> — сколько вы тратите в месяц на жизнь (без ЭКО и крупных разовых).\n"
                             "Вернуть автоматический расчёт по выпискам: <code>/living авто</code>")
        return
    await asyncio.to_thread(_plan_write, planner.set_setting, "living", value)
    await message.answer(f"✅ Обычные траты в плане: {money(value)} в месяц. Прогноз: /plan")


@router.message(Command("plan_add"))
async def cmd_plan_add(message: Message, command: CommandObject, state: FSMContext) -> None:
    words = (command.args or "").split()
    if not words:
        await _ask_args(message, state, "plan_add")
        return
    if len(words) < 3:
        await message.answer(_PLAN_HELP)
        return
    try:
        day = parse_user_date(words[0])
        raw = words[1].replace("\u00a0", "")
        if raw[0] not in "+-−":
            raise ValueError
        amount = parse_number(raw.lstrip("+-−"))
        amount = amount if raw[0] == "+" else -amount
    except (ValueError, IndexError):
        await message.answer("Формат: <code>/plan_add 06.11.2026 -100000 ЭКО</code> (+ приход, − трата).\n\n" + _PLAN_HELP)
        return
    rest = words[2:]
    monthly = False
    workday = False
    priority = 1
    title: list[str] = []
    i = 0
    while i < len(rest):
        w = rest[i].lower()
        if w in ("рабочий", "будни", "будний"):
            workday = True
        elif w in ("ежемесячно", "каждый"):
            monthly = True
            if w == "каждый" and i + 1 < len(rest) and rest[i + 1].lower().startswith("месяц"):
                i += 1
        elif w == "приоритет" and i + 1 < len(rest) and rest[i + 1] in "123":
            priority = int(rest[i + 1])
            i += 1
        else:
            title.append(rest[i])
        i += 1
    name = " ".join(title) or "Без названия"
    item_id = await asyncio.to_thread(_plan_write, planner.add_item, day, name, amount, monthly, priority, workday)
    await message.answer(
        f"✅ #{item_id}: {day:%d.%m.%Y} {'+' if amount > 0 else '−'}{money(abs(amount))} {escape(name)}"
        f"{' (каждый месяц)' if monthly else ''}{', в выходной — раньше, в рабочий день' if workday else ''}, "
        f"{planner.PRIORITY[priority]}\nПрогноз: /plan"
    )


@router.message(Command("plan_del"))
async def cmd_plan_del(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not (command.args or "").strip():
        await cmd_plan_list(message)
        await _ask_args(message, state, "plan_del")
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Формат: <code>/plan_del id</code> (id видны в /plan_list)")
        return
    ok = await asyncio.to_thread(_plan_write, planner.delete_item, int(command.args))
    await message.answer("🗑 Удалено." if ok else "Пункт с таким id не найден.")


@router.message(Command("plan_list"))
async def cmd_plan_list(message: Message) -> None:
    items = await asyncio.to_thread(_plan_write, planner.list_items)
    if not items:
        await message.answer(_PLAN_HELP)
        return
    lines = ["📝 <b>Пункты плана</b>"]
    for it in items:
        sign = "+" if it["amount"] > 0 else "−"
        lines.append(f"#{it['id']} {it['date']:%d.%m.%Y} {sign}{money(abs(it['amount']))} {escape(it['title'])}"
                     f"{' · каждый месяц' if it['monthly'] else ''}{' · если выходной, раньше' if it['workday'] else ''} · "
                     f"{planner.PRIORITY[it['priority']]}")
    await message.answer("\n".join(lines))


@router.message(Command("plan"))
async def cmd_plan(message: Message, command: CommandObject) -> None:
    items, balance, reserve, daily = await asyncio.to_thread(_plan_data)
    debts = await db.get_debts()
    today = date.today()
    graces = [d.grace_end_date for d in debts if d.grace_end_date and d.grace_end_date >= today]
    end = max(graces) if graces else today + timedelta(days=60)
    if command.args:
        try:
            end = parse_user_date(command.args)
        except ValueError:
            await message.answer("Дата вида <code>31.01.2027</code>.")
            return
    if end <= today:
        await message.answer("Конечная дата должна быть позже сегодняшней.")
        return
    if not items and not balance:
        await message.answer(_PLAN_HELP)
        return
    cards = [planner.Card(d.name, d.total_amount, d.limit_amount, d.grace_end_date, d.min_payment, d.due_date,
                          d.interest_rate) for d in debts]
    result = planner.simulate(items, cards, balance, today, end, daily, reserve)
    text = planner.format_result(result, cards)
    if daily == 0:
        text += "\n\n⚠️ Обычные траты не посчитаны: загрузите выписки (нужны полные месяцы)."
    await message.answer(text)


@router.message(Command("summary"))
async def cmd_summary(message: Message) -> None:
    def build() -> str | None:
        conn = sqlite3.connect(db.DB_PATH)
        try:
            return st_summary.build(conn)
        finally:
            conn.close()
    text = await asyncio.to_thread(build)
    await message.answer(text or "Операций из выписок пока нет.")


@router.message(Command("cat"))
async def cmd_cat(message: Message, command: CommandObject, state: FSMContext) -> None:
    """/cat Продукты [период] — помесячный разбор категории: число покупок, средний чек, магазины."""
    args = (command.args or "").strip()
    if not args:
        await _ask_args(message, state, "cat")
        return
    cats = sorted(nz.all_categories(), key=len, reverse=True)
    cat = next((c for c in cats if args.lower().startswith(c.lower())), None)
    if not cat:
        await message.answer("Напишите категорию и, если нужно, период: <code>/cat Продукты сентябрь октябрь</code>.\n"
                             "Категории: " + ", ".join(cats))
        return
    rest = args[len(cat):].strip()
    if rest:
        try:
            start, end = parse_period(rest)
        except ValueError:
            await message.answer("Не поняла период. Примеры: сентябрь октябрь, 01.09.2026 31.10.2026.")
            return
    else:
        today = date.today()
        start, end = (today.replace(day=1) - timedelta(days=62)).replace(day=1), None
    text = await asyncio.to_thread(
        _db_call, st_category.build, cat, start.isoformat(), end.isoformat() if end else None)
    await message.answer(text or f"В категории «{escape(cat)}» нет трат за этот период.")


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    text = await asyncio.to_thread(_db_call, st_week.build)
    await message.answer(text or "Операций пока нет.")


@router.message(Command("budget"))
async def cmd_budget(message: Message, command: CommandObject) -> None:
    """/budget — статус; /budget Категория сумма — лимит на месяц; /budget Категория - — убрать; /budget авто."""
    args = (command.args or "").strip()
    if not args:
        await asyncio.to_thread(_sync_manual)
        await message.answer(await asyncio.to_thread(_db_call, st_budget.status))
        return
    if args.lower() in ("авто", "auto"):
        await asyncio.to_thread(_sync_manual)
        res = await asyncio.to_thread(_db_call, st_budget.auto)
        if not res:
            await message.answer("Для автолимитов нужна история минимум за 3 полные недели. Пока задайте вручную: "
                                 "<code>/budget Продукты 25000</code>.")
            return
        lines = [f"{escape(c)}: {_ru(int(v * 100))} в месяц" for c, v in sorted(res.items(), key=lambda kv: -kv[1])]
        await message.answer("<b>Лимиты поставлены по вашим обычным тратам</b>\n" + "\n".join(lines)
                             + "\n\nПодправьте нужные: <code>/budget Продукты 25000</code>. Статус: /budget")
        return
    cats = sorted(nz.all_categories(), key=len, reverse=True)
    cat = next((c for c in cats if args.lower().startswith(c.lower())), None)
    rest = args[len(cat):].strip() if cat else ""
    if not cat or not rest:
        await message.answer("Формат: <code>/budget Продукты 25000</code>, убрать: <code>/budget Продукты -</code>, "
                             "посмотреть: /budget. Категории: " + ", ".join(cats))
        return
    if rest in ("-", "нет", "0"):
        ok = await asyncio.to_thread(_db_call, st_budget.delete_limit, cat)
        await message.answer(f"Лимит на «{escape(cat)}» убран." if ok else f"Лимита на «{escape(cat)}» и не было.")
        return
    try:
        limit = float(rest.replace(" ", "").replace("₽", "").replace(",", "."))
        if limit <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Сумма должна быть числом, например <code>/budget Продукты 25000</code>.")
        return
    await asyncio.to_thread(_db_call, st_budget.set_limit, cat, limit)
    await message.answer(f"✅ Лимит на «{escape(cat)}»: {_ru(int(limit * 100))} в месяц. Статус: /budget")


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    await asyncio.to_thread(_sync_manual)
    await message.answer(await asyncio.to_thread(_db_call, st_budget.today_text))


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
            start = parse_user_date(command.args).isoformat()
        except ValueError:
            await message.answer("Дата вида <code>01.04.2026</code> (или 2026-04-01): <code>/report 01.04.2026</code>")
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
        await wait.edit_text(_sheet_error_text(e))
        return
    await wait.edit_text(f"✅ Готово. Вкладка «{sheets_export.DETAIL_TAB}»: {r['detail_rows']} операций, "
                         f"вкладка «{sheets_export.SUMMARY_TAB}» обновлена.")


# ---------- /top и /recat (ручная правка категорий) ----------

_recat_sessions: dict[str, dict] = {}


class SplitOp(StatesGroup):
    parts = State()


class NewCategory(StatesGroup):
    name = State()


class RecatQuery(StatesGroup):
    query = State()
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


def _rc_cat_kb(token: str, cats: list[str]) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(text=c, callback_data=f"rc:{token}:k{k}") for k, c in enumerate(cats)]
    rows = [btns[j:j + 2] for j in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="➕ Новая категория", callback_data=f"rc:{token}:nc")])
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


def _sheet_error_text(e: Exception) -> str:
    """Понятное сообщение об ошибке Google Таблицы."""
    name = type(e).__name__
    if name in ("SSLError", "ConnectionError", "ReadTimeout", "ConnectTimeout", "Timeout", "ProxyError", "TransportError"):
        return ("Не получилось связаться с Google Таблицами (обрыв связи). Это бывает из-за VPN: смените сервер или режим "
                "и повторите команду. Ничего не изменилось.")
    return f"Не получилось: {escape(name)}. Подробности в окне бота."


def _d_iso(iso: str) -> str:
    return f"{iso[8:]}.{iso[5:7]}.{iso[:4]}"


@router.message(Command("recat"))
async def cmd_recat(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not command.args or not command.args.strip():           # из меню: спрашиваем, что искать, и ждём ответа
        await state.set_state(RecatQuery.query)
        await message.answer("Часть названия магазина или получателя? Например: <code>озон</code>.\n"
                             "Названия, которые бот видит в выписках, покажет /top. Отмена: /cancel")
        return
    await _open_recat(message, message.from_user.id, command.args.strip())


@router.message(RecatQuery.query, F.text, ~F.text.startswith("/"))
async def recat_query_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _open_recat(message, message.from_user.id, message.text.strip())


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


async def _rc_choose(p: dict, token: str, cat: str) -> str | None:
    """Применяет выбранную категорию: к отмеченным операциям или (режим «правило») ко всему магазину.
    Для правила возвращает итоговый текст (диалог закрывается), для отмеченных — None."""
    ops = p["ops"]
    if p["mode"] == "rule":
        n = await asyncio.to_thread(_rc_apply, "rule", [], (p["query"], cat))
        _recat_sessions.pop(token, None)
        return (f"✅ Правило сохранено: всё со словом «{escape(p['query'])}» → {escape(cat)}. "
                f"Изменено операций: {n}.")
    fps = [ops[i]["fingerprint"] for i in sorted(p["sel"])]
    await asyncio.to_thread(_rc_apply, "cat", fps, cat)
    for i in p["sel"]:
        ops[i]["category"] = cat
    p["sel"] = set()
    return None


@router.message(Command("add_category"))
async def cmd_add_category(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not command.args:
        await state.set_state(NewCategory.name)
        await state.update_data(token="")
        await message.answer("Как назвать новую категорию? Напишите название одним сообщением, например: "
                             "<code>Хобби</code>. Отмена: /cancel")
        return
    await _create_category(message, state, command.args)


@router.message(Command("del_category"))
async def cmd_del_category(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not (command.args or "").strip():
        await _ask_args(message, state, "del_category")
        return
    name = await asyncio.to_thread(_db_call, recat.delete_category, command.args)
    await message.answer(f"Категория «{escape(name)}» убрана из списка." if name
                         else "Такой своей категории нет. Стандартные категории удалить нельзя.")


@router.message(NewCategory.name, F.text, ~F.text.startswith("/"))
async def new_category_name(message: Message, state: FSMContext) -> None:
    await _create_category(message, state, message.text)


async def _create_category(message: Message, state: FSMContext, raw: str) -> None:
    data = await state.get_data()
    try:
        name, created = await asyncio.to_thread(_db_call, recat.add_category, raw)
    except ValueError as e:
        await message.answer(f"⚠️ {escape(str(e))}\nПопробуйте ещё раз или /cancel.")
        return
    await state.clear()
    head = f"✅ Категория «{escape(name)}» добавлена." if created else f"Категория «{escape(name)}» уже есть."
    p = _recat_sessions.get(data.get("token", ""))
    if p and p["user"] == message.from_user.id:        # создавали из выбора категории: сразу применяем её
        text = await _rc_choose(p, data["token"], name)
        if text:
            await message.answer(head + "\n" + text)
            return
        await message.answer(head + " Применила к отмеченным операциям.")
        await message.answer(_rc_text(p), reply_markup=_rc_kb(data["token"], p))
        return
    await message.answer(head + " Теперь она есть в списках категорий (/recat, разбивка операций).")


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
        p["cats"] = nz.all_categories()           # снимок списка: кнопки не «съедут», если добавят новую категорию
        await cb.message.edit_text(title, reply_markup=_rc_cat_kb(token, p["cats"]))
    elif action[0] == "k" and action[1:].isdigit():
        cats = p.get("cats") or nz.all_categories()
        if int(action[1:]) >= len(cats):
            await cb.answer("Список устарел, откройте категории ещё раз.", show_alert=True)
            return
        text = await _rc_choose(p, token, cats[int(action[1:])])
        if text:                                   # правило для магазина: диалог закрыт
            await cb.message.edit_text(text)
            await cb.answer()
            return
        await refresh()
        await cb.answer("Готово")
        return
    elif action == "nc":
        await state.set_state(NewCategory.name)
        await state.update_data(token=token)
        await cb.message.answer("Как назвать новую категорию? Напишите название одним сообщением, например: "
                                "<code>Хобби</code>. Отмена: /cancel")
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


# ---------- /edit_sheet и /apply_sheet (массовая правка категорий через Google Таблицу) ----------

def _edit_sheet_export(start: str | None, end: str | None = None) -> dict:
    spreadsheet = sheets_engine.get_spreadsheet()
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return edit_sheet.export(conn, spreadsheet, start, end)
    finally:
        conn.close()


def _edit_sheet_apply() -> dict:
    spreadsheet = sheets_engine.get_spreadsheet()
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return edit_sheet.apply(conn, spreadsheet)
    finally:
        conn.close()


class EditPeriod(StatesGroup):
    period = State()


def _period_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Этот месяц", callback_data="ep:cur"),
             InlineKeyboardButton(text="Прошлый месяц", callback_data="ep:prev")],
            [InlineKeyboardButton(text="Прошлый и этот", callback_data="ep:both"),
             InlineKeyboardButton(text="Всё с начала анализа", callback_data="ep:all")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _period_for(code: str) -> tuple[str | None, str | None]:
    import calendar
    t = date.today()
    py, pm = (t.year, t.month - 1) if t.month > 1 else (t.year - 1, 12)
    first_prev = date(py, pm, 1).isoformat()
    last_prev = date(py, pm, calendar.monthrange(py, pm)[1]).isoformat()
    if code == "cur":
        return date(t.year, t.month, 1).isoformat(), None
    if code == "prev":
        return first_prev, last_prev
    if code == "both":
        return first_prev, None
    return default_start(), None


async def _run_edit_sheet(message: Message, start: str | None, end: str | None) -> None:
    wait = await message.answer("Готовлю вкладку для правок…")
    try:
        r = await asyncio.to_thread(_edit_sheet_export, start, end)
    except Exception as e:
        log.exception("Не удалось выгрузить вкладку правок")
        await wait.edit_text(_sheet_error_text(e))
        return
    await wait.edit_text(
        f"✅ Вкладка «{edit_sheet.TAB}» готова: {r['rows']} операций, "
        + (f"с {_d_iso(r['start'])} по {_d_iso(r['end'])}" if r.get("end") else f"с {_d_iso(r['start'])}") + ".\n\n"
        "В таблице заполните «Новую категорию» (выбор из списка), при необходимости «Плановая» и «Разбить», "
        "потом вернитесь и напишите /apply_sheet. Подсказка по столбцам — справа на вкладке, в столбце K.")


PERIOD_HELP = ("Примеры: <code>сентябрь</code>, <code>сентябрь октябрь</code>, <code>09.2026</code>, "
               "<code>01.09.2026 30.09.2026</code>, <code>01.09.2026</code> (с этой даты и дальше).")


@router.message(Command("edit_sheet"))
async def cmd_edit_sheet(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not sheets_engine.is_enabled():
        await message.answer("Google Таблица не подключена (нет credentials.json или GOOGLE_SPREADSHEET_NAME в .env).")
        return
    if not command.args:                    # из меню: спрашиваем период кнопками или текстом и ждём
        await state.set_state(EditPeriod.period)
        await message.answer("За какой период подготовить вкладку для правок? Нажмите кнопку или напишите период.\n"
                             + PERIOD_HELP + "\nОтмена: /cancel", reply_markup=_period_kb())
        return
    try:
        d1, d2 = parse_period(command.args)
    except ValueError:
        await message.answer("Не поняла период. " + PERIOD_HELP)
        return
    await _run_edit_sheet(message, d1.isoformat(), d2.isoformat() if d2 else None)


@router.message(EditPeriod.period, F.text, ~F.text.startswith("/"))
async def edit_period_text(message: Message, state: FSMContext) -> None:
    try:
        d1, d2 = parse_period(message.text)
    except ValueError:
        await message.answer("Не поняла период. " + PERIOD_HELP + "\nИли нажмите кнопку выше. Отмена: /cancel")
        return
    await state.clear()
    await _run_edit_sheet(message, d1.isoformat(), d2.isoformat() if d2 else None)


@router.callback_query(F.data.startswith("ep:"))
async def on_period_button(cb: CallbackQuery, state: FSMContext) -> None:
    if _allowed and cb.from_user.id not in _allowed:
        await cb.answer()
        return
    await state.clear()
    start, end = _period_for(cb.data.split(":", 1)[1])
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await _run_edit_sheet(cb.message, start, end)


@router.message(Command("apply_sheet"))
async def cmd_apply_sheet(message: Message) -> None:
    if not sheets_engine.is_enabled():
        await message.answer("Google Таблица не подключена.")
        return
    wait = await message.answer("Читаю вкладку и применяю правки…")
    try:
        st = await asyncio.to_thread(_edit_sheet_apply)
    except Exception as e:
        log.exception("Не удалось применить правки из таблицы")
        text = ("На таблице нет вкладки «%s». Сначала /edit_sheet." % edit_sheet.TAB
                if type(e).__name__ == "WorksheetNotFound" else _sheet_error_text(e))
        await wait.edit_text(text)
        return
    lines = [f"✅ Применено. Категорий изменено: {st['category']}, плановых отметок: {st['planned']}, разбивок: {st['split']}."]
    if st["errors"]:
        lines.append(f"⚠ Не разобрала строк: {st['errors']}. Они помечены в столбце «Результат» — исправьте и повторите /apply_sheet.")
    lines.append("Чтобы обновить вкладки «Выписки» и «Итоги по месяцам», отправьте /sheet; новую вкладку правок получите снова через /edit_sheet.")
    await wait.edit_text("\n".join(lines))


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
            start = parse_user_date(command.args).isoformat()
        except ValueError:
            await message.answer("Дата вида <code>01.04.2026</code>: <code>/advice 01.04.2026</code>")
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
        lines += ["", "♻️ — уже есть в базе (из выписки, ручных трат или прошлого скриншота), не сохраняю."]
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
    p = {"user": message.from_user.id, "txns": txns, "dupes": dupes, "removed": set(), "msg": wait, "token": token}
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


@router.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot) -> None:
    """Голосовое после скриншота: расставляет категории в открытом списке операций."""
    if _allowed and message.from_user.id not in _allowed:
        return
    p = next((x for x in reversed(list(_pending_shots.values())) if x["user"] == message.from_user.id), None)
    if p is None:
        await message.answer("Голосовые я пока понимаю только после скриншота: пришлите скриншот операций, "
                             "а потом голосовое с категориями.")
        return
    if not os.getenv("GEMINI_API_KEY"):
        await message.answer("Для голоса нужен GEMINI_API_KEY в .env (см. README).")
        return
    media = message.voice or message.audio
    buf = io.BytesIO()
    await bot.download(media, destination=buf)
    wait = await message.answer("Слушаю…")
    items = [f"{i + 1}. {t.merchant} — {_ru(t.amount_kop)}" + ("" if t.fingerprint not in p["dupes"] else " (дубль, не трогать)")
             for i, t in enumerate(p["txns"])]
    mime = (getattr(media, "mime_type", None) or "audio/ogg").split(";")[0]
    try:
        res = await asyncio.to_thread(st_voice.understand, buf.getvalue(), mime, items, nz.all_categories())
    except RuntimeError as e:
        await wait.edit_text(escape(str(e)))
        return
    cats = nz.all_categories()
    changed = []
    for i, cat in res["assign"].items():
        if p["txns"][i].fingerprint in p["dupes"]:
            continue
        _apply_category(p["txns"][i], cats.index(cat))
        changed.append(f"{i + 1} → {escape(cat)}")
    for i in res["remove"]:
        if p["txns"][i].fingerprint not in p["dupes"]:
            p["removed"].add(i)
            changed.append(f"{i + 1} → не сохранять")
    lines = []
    if res["transcript"]:
        lines.append(f"Услышала: «{escape(res['transcript'])}»")
    lines.append("Поменяла: " + (", ".join(changed) if changed else "ничего"))
    if res["unknown"]:
        lines.append("Не знаю такой категории: " + escape(", ".join(res["unknown"])) + ". Выберите кнопкой «Категория».")
    if res["unclear"]:
        lines.append("Не поняла: " + escape(res["unclear"]))
    sug_kb = None
    if res["suggest"]:
        lines.append("Предлагаю: " + ", ".join(f"{i + 1} → {escape(c)}" for i, c in res["suggest"].items())
                     + ". Нажмите кнопку, чтобы принять.")
        sug_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"{i + 1} → {c}", callback_data=f"vs:{p['token']}:{i}-{cats.index(c)}")]
            for i, c in res["suggest"].items() if p["txns"][i].fingerprint not in p["dupes"]])
    await wait.edit_text("\n".join(lines), reply_markup=sug_kb)
    try:
        await p["msg"].edit_text(_shot_text(p), reply_markup=_shot_kb(p["token"], p))
    except Exception:  # сообщение со списком могли удалить
        await message.answer(_shot_text(p), reply_markup=_shot_kb(p["token"], p))


@router.callback_query(F.data.startswith("vs:"))
async def on_voice_suggest(cb: CallbackQuery) -> None:
    if _allowed and cb.from_user.id not in _allowed:
        await cb.answer()
        return
    _, token, pair = cb.data.split(":", 2)
    p = _pending_shots.get(token)
    if not p or p["user"] != cb.from_user.id:
        await cb.answer("Этот список уже закрыт. Пришлите скриншот заново.", show_alert=True)
        return
    i, k = (int(x) for x in pair.split("-"))
    _apply_category(p["txns"][i], k)
    await cb.answer("Применила")
    try:
        await p["msg"].edit_text(_shot_text(p), reply_markup=_shot_kb(token, p))
    except Exception:
        await cb.message.answer(_shot_text(p), reply_markup=_shot_kb(token, p))
    await cb.message.edit_text(cb.message.html_text + f"\n✅ {i + 1} → {escape(nz.all_categories()[k])}")


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


_AWAIT_HANDLERS = {
    "balance": cmd_balance, "reserve": cmd_reserve, "living": cmd_living, "plan_add": cmd_plan_add,
    "plan_del": cmd_plan_del, "debt_edit": cmd_debt_edit, "del_debt": cmd_del_debt, "cat": cmd_cat,
    "del_category": cmd_del_category,
}


@router.message.outer_middleware()
async def _drop_pending_args(handler, event: Message, data: dict):
    """Новая команда отменяет ожидание значения для предыдущей."""
    state = data.get("state")
    if state is not None and event.text and event.text.startswith("/"):
        if await state.get_state() == AwaitArgs.text.state:
            await state.clear()
    return await handler(event, data)


# ---------- Запуск ----------

async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN. Скопируйте .env.example в .env и укажите токен.")

    await db.init_db()
    try:
        await asyncio.to_thread(_db_call, recat.load_custom)     # свои категории из базы
    except sqlite3.Error:
        log.exception("Не удалось прочитать свои категории")
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
        BotCommand(command="add_category", description="Добавить свою категорию"),
        BotCommand(command="edit_sheet", description="Правка категорий в Google Таблице"),
        BotCommand(command="apply_sheet", description="Применить правки из Google Таблицы"),
        BotCommand(command="recat", description="Поправить категории у магазина"),
        BotCommand(command="sheet", description="Выгрузить операции в Google Таблицу"),
        BotCommand(command="advice", description="Комментарии и советы по тратам"),
        BotCommand(command="add_debt", description="Добавить долг"),
        BotCommand(command="debts", description="Долги, кредитки, дедлайны"),
        BotCommand(command="debt_edit", description="Поменять данные по долгу"),
        BotCommand(command="plan", description="Прогноз денег по дням"),
        BotCommand(command="plan_add", description="Добавить приход или трату в план"),
        BotCommand(command="balance", description="Сколько денег сейчас"),
        BotCommand(command="living", description="Обычные траты в месяц для плана"),
        BotCommand(command="summary", description="Сводка по месяцам"),
        BotCommand(command="budget", description="Лимиты по категориям"),
        BotCommand(command="today", description="Сколько можно потратить сегодня"),
        BotCommand(command="week", description="Траты за неделю против обычного"),
        BotCommand(command="cat", description="Разбор категории"),
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
