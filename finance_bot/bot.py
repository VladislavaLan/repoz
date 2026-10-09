"""Telegram-бот учёта личных финансов и погашения долгов по «Лавине»."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime
from html import escape

from dotenv import load_dotenv

load_dotenv()  # до импорта database, чтобы подхватить DB_PATH из .env

from aiogram import Bot, Dispatcher, F, Router  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.filters import Command, CommandObject, CommandStart  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.state import State, StatesGroup  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import BotCommand, BufferedInputFile, Message  # noqa: E402

import database as db  # noqa: E402
from chart_engine import build_month_chart  # noqa: E402
from debt_engine import avalanche_savings  # noqa: E402

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
    "/add_expense <code>сумма категория [описание]</code> — записать расход\n"
    "   например: <code>/add_expense 450 Еда обед в кафе</code>\n"
    "/stats — диаграмма расходов за текущий месяц\n\n"
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

@router.message(Command("add_expense"))
async def cmd_add_expense(message: Message, command: CommandObject) -> None:
    usage = "Формат: <code>/add_expense сумма категория [описание]</code>\nНапример: <code>/add_expense 450 Еда обед</code>"
    if not command.args:
        await message.answer(usage)
        return

    match = EXPENSE_RE.match(command.args)
    if not match:
        await message.answer(f"Не понял запись.\n{usage}")
        return

    amount = parse_number(match["amount"])
    if amount <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return

    category = match["category"].capitalize()
    description = (match["description"] or "").strip()
    await db.add_expense(amount, category, description)

    text = f"✅ Записал расход: <b>{money(amount)}</b> — {escape(category)}"
    if description:
        text += f" ({escape(description)})"
    await message.answer(text)


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


# ---------- Запуск ----------

async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN. Скопируйте .env.example в .env и укажите токен.")

    await db.init_db()

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Приветствие и меню"),
        BotCommand(command="add_expense", description="Записать расход"),
        BotCommand(command="stats", description="Диаграмма расходов за месяц"),
        BotCommand(command="add_debt", description="Добавить долг"),
        BotCommand(command="debts", description="Долги и план «Лавина»"),
        BotCommand(command="cancel", description="Отменить диалог"),
    ])

    log.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
