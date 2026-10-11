"""Планировщик денег: прогноз остатка по дням с приходами, плановыми тратами и кредитками.

Модель (простая и прозрачная):
  * каждый день вычитаются обычные траты (средние за полные месяцы без крупных и плановых);
  * в дни из плана добавляются приходы (+) и траты (-); пункты можно повторять каждый месяц;
  * в даты обязательных платежей по долгам (поле due_date) списывается минимальный платёж;
  * если денег не хватает, недостающее берётся с кредитной карты, у которой есть льготный период
    и свободный лимит (покупка на кредитку); если лимита нет — фиксируется нехватка;
  * в день прихода всё, что выше резерва, уходит на погашение карты с ближайшим дедлайном льготного периода.
Это оценка, а не обещание банка: реальные условия карт нужно сверять в приложении.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT    NOT NULL,
    title    TEXT    NOT NULL,
    amount   REAL    NOT NULL,            -- + приход, - трата
    monthly  INTEGER NOT NULL DEFAULT 0,  -- 1 = повторять каждый месяц в тот же день
    priority INTEGER NOT NULL DEFAULT 1   -- 1 неизбежно, 2 можно сдвинуть, 3 по желанию
);
CREATE TABLE IF NOT EXISTS plan_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

PRIORITY = {1: "неизбежно", 2: "можно сдвинуть", 3: "по желанию"}
DEFAULT_RESERVE = 30000.0


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(PLAN_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plan_items)")}
    if "workday" not in cols:                      # миграция старых баз
        conn.execute("ALTER TABLE plan_items ADD COLUMN workday INTEGER NOT NULL DEFAULT 0")


def get_setting(conn: sqlite3.Connection, key: str, default: float | None = None) -> float | None:
    ensure(conn)
    row = conn.execute("SELECT value FROM plan_settings WHERE key = ?", (key,)).fetchone()
    return float(row[0]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: float) -> None:
    ensure(conn)
    conn.execute("INSERT INTO plan_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, repr(float(value))))
    conn.commit()


def add_item(conn: sqlite3.Connection, day: date, title: str, amount: float, monthly: bool = False,
             priority: int = 1, workday: bool = False) -> int:
    ensure(conn)
    cur = conn.execute("INSERT INTO plan_items (date, title, amount, monthly, priority, workday) VALUES (?,?,?,?,?,?)",
                       (day.isoformat(), title, amount, int(monthly), priority, int(workday)))
    conn.commit()
    return cur.lastrowid


def delete_item(conn: sqlite3.Connection, item_id: int) -> bool:
    ensure(conn)
    cur = conn.execute("DELETE FROM plan_items WHERE id = ?", (item_id,))
    conn.commit()
    return cur.rowcount > 0


def list_items(conn: sqlite3.Connection) -> list[dict]:
    ensure(conn)
    return [dict(id=r[0], date=date.fromisoformat(r[1]), title=r[2], amount=r[3], monthly=bool(r[4]), priority=r[5],
                 workday=bool(r[6]))
            for r in conn.execute("SELECT id, date, title, amount, monthly, priority, workday FROM plan_items ORDER BY date, id")]


def _add_month(d: date, k: int) -> date:
    idx = d.month - 1 + k
    y, m = d.year + idx // 12, idx % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


# Нерабочие праздники по фиксированным датам (месяц, день). Переносы выходных по производственному
# календарю не учитываются: это приближение.
HOLIDAYS = {(1, d) for d in range(1, 9)} | {(2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)}


def is_workday(d: date) -> bool:
    return d.weekday() < 5 and (d.month, d.day) not in HOLIDAYS


def shift_to_workday(d: date) -> date:
    """Если день выходной или праздник, выплата приходит в ближайший рабочий день раньше."""
    while not is_workday(d):
        d -= timedelta(days=1)
    return d


def occurrences(item: dict, start: date, end: date) -> list[date]:
    """Даты срабатывания пункта плана в [start, end]."""
    shift = shift_to_workday if item.get("workday") else (lambda x: x)
    if not item["monthly"]:
        d = shift(item["date"])
        return [d] if start <= d <= end else []
    out, k = [], 0
    while True:
        base = _add_month(item["date"], k)
        if base > end + timedelta(days=15):
            return out
        d = shift(base)
        if start <= d <= end:
            out.append(d)
        k += 1


@dataclass
class Card:
    """Карта/долг в модели: остаток, лимит и дедлайн льготного периода."""
    name: str
    balance: float
    limit: float = 0.0
    grace_end: date | None = None
    min_payment: float = 0.0
    due_date: date | None = None
    rate: float = 0.0

    @property
    def free(self) -> float:
        return max(self.limit - self.balance, 0.0) if self.limit else 0.0


@dataclass
class Result:
    start: date
    end: date
    daily_living: float
    start_balance: float
    reserve: float
    events: list[tuple[date, str, float]] = field(default_factory=list)      # (дата, что, сумма)
    draws: list[tuple[date, str, float]] = field(default_factory=list)       # покупки на кредитку
    shortfall: list[tuple[date, float]] = field(default_factory=list)        # нехватка даже с кредиткой
    min_balance: float = 0.0
    min_date: date | None = None
    balance_end: float = 0.0
    cards_end: dict[str, float] = field(default_factory=dict)
    cards_peak: dict[str, float] = field(default_factory=dict)
    month_marks: list[tuple[date, float, dict[str, float]]] = field(default_factory=list)
    unpaid_at_deadline: dict[str, float] = field(default_factory=dict)       # остаток к дате конца льготы


def baseline_daily(report: dict | None) -> float:
    """Обычные траты в день: среднее за полные месяцы без крупных и плановых, иначе просто среднее."""
    if not report:
        return 0.0
    monthly = report.get("avg_spend_no_big") or report.get("avg", {}).get("spend") or 0.0
    return max(monthly, 0.0) / 30.4


def simulate(items: list[dict], cards: list[Card], start_balance: float, today: date, end: date,
             daily_living: float, reserve: float = DEFAULT_RESERVE) -> Result:
    res = Result(start=today, end=end, daily_living=daily_living, start_balance=start_balance, reserve=reserve)
    bal = start_balance
    cards = [Card(**vars(c)) for c in cards]            # работаем с копиями
    by_name = {c.name: c for c in cards}
    res.min_balance, res.min_date = bal, today
    res.cards_peak = {c.name: c.balance for c in cards}

    plan: dict[date, list[tuple[str, float]]] = {}
    for it in items:
        for d in occurrences(it, today, end):
            plan.setdefault(d, []).append((it["title"], it["amount"]))
    for c in cards:                                      # обязательные платежи по долгам
        if c.min_payment > 0 and c.due_date:
            for d in occurrences({"date": c.due_date, "monthly": True}, today, end):
                plan.setdefault(d, []).append((f"Платёж: {c.name}", -c.min_payment))

    day = today
    while day <= end:
        if day > today:
            bal -= daily_living
        income_today = False
        for title, amount in plan.get(day, []):
            res.events.append((day, title, amount))
            if title.startswith("Платёж: "):
                card = by_name[title[8:]]
                card.balance = max(card.balance + amount, 0.0)
            if amount > 0:
                income_today = True
            bal += amount
        if bal < 0:                                       # не хватает — берём с кредитки с льготой
            need = -bal
            for c in sorted(cards, key=lambda x: (x.grace_end is None, x.grace_end or date.max)):
                if need <= 0:
                    break
                if not c.grace_end or c.grace_end < day or c.free <= 0:
                    continue
                take = min(need, c.free)
                c.balance += take
                res.cards_peak[c.name] = max(res.cards_peak[c.name], c.balance)
                res.draws.append((day, c.name, take))
                need -= take
                bal += take
            if need > 0:
                res.shortfall.append((day, need))
        if income_today and bal > reserve:                # излишки — на карту с ближайшим дедлайном
            surplus = bal - reserve
            for c in sorted((c for c in cards if c.grace_end and c.grace_end >= day and c.balance > 0),
                            key=lambda x: x.grace_end):
                pay = min(surplus, c.balance)
                if pay > 0.5:
                    c.balance -= pay
                    bal -= pay
                    surplus -= pay
                    res.events.append((day, f"Погашение: {c.name} (останется {rub(c.balance)})", -pay))
                if surplus <= 0:
                    break
        if bal < res.min_balance:
            res.min_balance, res.min_date = bal, day
        if day == end or (day + timedelta(days=1)).month != day.month:
            res.month_marks.append((day, bal, {c.name: c.balance for c in cards}))
        for c in cards:
            if c.grace_end and day == c.grace_end:
                res.unpaid_at_deadline[c.name] = c.balance
        day += timedelta(days=1)
    res.balance_end = bal
    res.cards_end = {c.name: c.balance for c in cards}
    return res


def rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def _d(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def format_result(r: Result, cards: list[Card]) -> str:
    L = [
        "🗓 <b>План денег</b>",
        f"С {_d(r.start)} по {_d(r.end)}",
        f"Сейчас на счёте: {rub(r.start_balance)} · обычные траты ≈ {rub(r.daily_living * 30.4)} в месяц "
        f"({rub(r.daily_living)} в день) · после погашения карты оставляем на счёте {rub(r.reserve)}",
        "",
    ]
    shown = list(r.events)
    if shown:
        L.append("<b>События</b>")
        for d, title, amount in shown[:40]:
            L.append(f"• {_d(d)} {'+' if amount > 0 else '−'}{rub(abs(amount))} {title}")
        if len(shown) > 40:
            L.append(f"…и ещё {len(shown) - 40}")
        L.append("")
    if r.draws:
        L.append("<b>Когда не хватает денег, берём с кредитки</b>")
        for c in cards:
            if any(n == c.name for _, n, _ in r.draws):
                start_bal = c.balance
                peak = r.cards_peak.get(c.name, start_bal)
                L.append(f"• {c.name}: долг вырастет с {rub(start_bal)} до {rub(peak)} (пик), "
                         f"первая покупка {_d(min(d for d, n, _ in r.draws if n == c.name))}")
        L.append("")
    if r.shortfall:
        d, a = r.shortfall[0]
        L += [f"🚨 <b>Не хватает даже с кредиткой</b>: с {_d(d)}, до {rub(max(x for _, x in r.shortfall))}. "
              "Нужно сдвинуть траты или найти деньги.", ""]
    if r.draws:
        L.append(f"Свои деньги закончатся: <b>{_d(min(d for d, _, _ in r.draws))}</b>, дальше обычные траты идут с кредитки")
    else:
        L.append(f"Самый низкий остаток: <b>{rub(r.min_balance)}</b> ({_d(r.min_date)})")
    for c in cards:
        if c.grace_end:
            peak = r.cards_peak.get(c.name, c.balance)
            L.append(f"• {c.name}: пик долга {rub(peak)}" + (f" из {rub(c.limit)}" if c.limit else ""))
    L.append("")
    L.append("<b>Долги на конец месяца</b>")
    for d, bal, cs in r.month_marks:
        parts = " · ".join(f"{n}: {rub(v)}" for n, v in cs.items() if v > 0.5) or "долгов нет"
        L.append(f"{_d(d)}: счёт {rub(bal)} · {parts}")
    L.append("")
    for c in cards:
        if not c.grace_end or c.grace_end > r.end:
            continue
        left = r.unpaid_at_deadline.get(c.name, r.cards_end.get(c.name, 0.0))
        if left <= 0.5:
            L.append(f"✅ {c.name}: к {_d(c.grace_end)} долг закрыт при таком плане.")
        else:
            monthly_cost = left * c.rate / 100 / 12 if c.rate else 0
            tail = f" Проценты могут начислить за весь период (≈{rub(monthly_cost)} в месяц)." if monthly_cost else ""
            L.append(f"⚠️ {c.name}: к {_d(c.grace_end)} останется {rub(left)}.{tail} "
                     "Чтобы успеть, сократите обычные траты или сдвиньте пункты плана.")
    L += ["", "<i>Оценка по твоим данным, не обещание банка. Условия карт сверяйте в приложении.</i>"]
    return "\n".join(L)
