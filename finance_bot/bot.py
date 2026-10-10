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
from statements import insights, screenshot  # noqa: E402
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
    "/add_expense <code>сумма категория [описание]</code> — записать расход\n"
    "   например: <code>/add_expense 450 Еда обед в кафе</code>\n"
    "/stats — диаграмма расходов за текущий месяц\n"
    "/report <code>[ГГГГ-ММ-ДД]</code> — отчёт по загруженным банковским выпискам\n"
    "📷 Пришлите скриншот истории операций из банка — я распознаю его и предложу сохранить\n"
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
    # Дублируем в Google Таблицу в фоне: ответ пользователю не ждёт сети.
    sheets_engine.schedule_append(
        date.today(), message.from_user.id, category, amount, description, "expense"
    )

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
    rows.append([InlineKeyboardButton(text="💾 Сохранить", callback_data=f"shot:{token}:save"),
                 InlineKeyboardButton(text="✖ Отмена", callback_data=f"shot:{token}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    _, token, action = cb.data.split(":")
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
    if sheets_engine.is_enabled():
        log.info("Google Sheets: запись в таблицу «%s» включена", os.getenv("GOOGLE_SPREADSHEET_NAME"))
    else:
        log.info("Google Sheets выключен (нет GOOGLE_SPREADSHEET_NAME или credentials.json)")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Приветствие и меню"),
        BotCommand(command="add_expense", description="Записать расход"),
        BotCommand(command="stats", description="Диаграмма расходов за месяц"),
        BotCommand(command="report", description="Отчёт по банковским выпискам"),
        BotCommand(command="advice", description="Комментарии и советы по тратам"),
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
