"""Расчёт погашения долгов по стратегии «Лавина» (debt avalanche).

Суть стратегии: все долги обслуживаются минимальными платежами, а любые
свободные деньги направляются в долг с наибольшей процентной ставкой.
Когда он закрыт, его минимальный платёж «перетекает» в следующий по ставке.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from database import Debt

MAX_MONTHS = 600  # 50 лет — защита от бесконечной симуляции


@dataclass(slots=True)
class PayoffResult:
    months: int                      # за сколько месяцев закрыты все долги
    total_interest: float            # сколько переплачено процентов
    payoff_order: list[str]          # порядок закрытия долгов
    payoff_month: dict[int, int] = field(default_factory=dict)  # debt.id -> месяц закрытия
    finished: bool = True            # False, если долги не гасятся за MAX_MONTHS


@dataclass(slots=True)
class AvalancheReport:
    prioritized: list[Debt]          # долги в порядке приоритета оплаты
    baseline: PayoffResult           # без досрочного платежа
    with_extra: PayoffResult         # с досрочным платежом
    extra_payment: float

    @property
    def interest_saved(self) -> float:
        return round(self.baseline.total_interest - self.with_extra.total_interest, 2)

    @property
    def months_saved(self) -> int:
        return self.baseline.months - self.with_extra.months


def sort_avalanche(debts: Iterable[Debt]) -> list[Debt]:
    """Сортировка по убыванию ставки; при равной ставке — сначала меньший остаток."""
    return sorted(debts, key=lambda d: (-d.interest_rate, d.total_amount))


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def simulate_payoff(
    debts: Iterable[Debt],
    lump_sum: float = 0.0,
    monthly_extra: float = 0.0,
    start: date | None = None,
) -> PayoffResult:
    """Помесячная симуляция погашения по «Лавине».

    :param lump_sum: разовый досрочный платёж, вносится сразу (до начисления процентов).
    :param monthly_extra: дополнительная сумма сверх минимальных платежей каждый месяц.
    Ежемесячный бюджет = сумма всех мин. платежей + monthly_extra и остаётся
    постоянным: платежи закрытых долгов перенаправляются в следующий по приоритету.
    """
    start = start or date.today()
    ordered = sort_avalanche(d for d in debts if d.total_amount > 0)
    balances = {d.id: float(d.total_amount) for d in ordered}
    budget = sum(d.min_payment for d in ordered) + monthly_extra

    result = PayoffResult(months=0, total_interest=0.0, payoff_order=[])

    def close_paid(month: int) -> None:
        for d in ordered:
            if balances[d.id] <= 0.005 and d.id not in result.payoff_month:
                balances[d.id] = 0.0
                result.payoff_month[d.id] = month
                result.payoff_order.append(d.name)

    def pay_avalanche(amount: float) -> float:
        """Распределяет сумму по долгам в порядке приоритета, возвращает остаток."""
        for d in ordered:
            if amount <= 0:
                break
            pay = min(amount, balances[d.id])
            balances[d.id] -= pay
            amount -= pay
        return amount

    pay_avalanche(lump_sum)
    close_paid(0)

    month = 0
    while any(b > 0 for b in balances.values()):
        if month >= MAX_MONTHS:
            result.finished = False
            break
        month += 1
        current = _add_months(start, month - 1)

        # 1. Начисление процентов (кроме долгов в льготном периоде).
        for d in ordered:
            if balances[d.id] <= 0:
                continue
            if d.grace_end_date and current < d.grace_end_date:
                continue
            interest = balances[d.id] * d.interest_rate / 100 / 12
            balances[d.id] += interest
            result.total_interest += interest

        # 2. Минимальные платежи по всем открытым долгам.
        remaining = budget
        for d in ordered:
            pay = min(d.min_payment, balances[d.id], remaining)
            balances[d.id] -= pay
            remaining -= pay

        # 3. Остаток бюджета — в долг с наибольшей ставкой («Лавина»).
        pay_avalanche(remaining)
        close_paid(month)

    result.months = month
    result.total_interest = round(result.total_interest, 2)
    return result


def avalanche_savings(
    debts: Iterable[Debt],
    extra_payment: float,
    monthly_extra: float = 0.0,
    start: date | None = None,
) -> AvalancheReport:
    """Сравнивает сценарии без досрочного платежа и с ним.

    Возвращает приоритет оплаты по «Лавине» и экономию на процентах,
    которую даёт разовое досрочное погашение `extra_payment`.
    """
    debts = list(debts)
    return AvalancheReport(
        prioritized=sort_avalanche(debts),
        baseline=simulate_payoff(debts, 0.0, monthly_extra, start),
        with_extra=simulate_payoff(debts, extra_payment, monthly_extra, start),
        extra_payment=extra_payment,
    )
