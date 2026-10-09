"""Генерация PNG-диаграмм расходов с помощью matplotlib."""

from __future__ import annotations

import asyncio
import io
from datetime import date

from matplotlib import colormaps
from matplotlib.figure import Figure  # без pyplot: потокобезопасно и не нужен GUI

from database import get_expenses_by_category

MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
MAX_SLICES = 7  # мелкие категории объединяются в «Прочее»


def _rub(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def month_bounds(today: date | None = None) -> tuple[date, date]:
    """Первый день текущего месяца и первый день следующего."""
    today = today or date.today()
    start = today.replace(day=1)
    end = date(start.year + 1, 1, 1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def render_pie_chart(data: dict[str, float], title: str) -> bytes:
    """Рисует круговую диаграмму и возвращает PNG в виде байтов."""
    items = sorted(data.items(), key=lambda kv: kv[1], reverse=True)
    if len(items) > MAX_SLICES:
        head, tail = items[: MAX_SLICES - 1], items[MAX_SLICES - 1:]
        items = head + [("Прочее", sum(v for _, v in tail))]

    values = [v for _, v in items]
    total = sum(values)

    fig = Figure(figsize=(8, 6), dpi=120)
    ax = fig.subplots()
    wedges, _, autotexts = ax.pie(
        values,
        autopct=lambda pct: f"{pct:.1f}%" if pct >= 3 else "",
        startangle=90,
        counterclock=False,
        pctdistance=0.78,
        wedgeprops={"width": 0.45, "edgecolor": "white", "linewidth": 2},
        colors=colormaps["tab10"].colors[: len(values)],
    )
    for text in autotexts:
        text.set_color("white")
        text.set_fontsize(9)
        text.set_fontweight("bold")

    ax.text(0, 0, _rub(total), ha="center", va="center", fontsize=16, fontweight="bold")
    ax.legend(
        wedges,
        [f"{label} — {_rub(value)}" for label, value in items],
        loc="center left",
        bbox_to_anchor=(1.0, 0.5),
        frameon=False,
    )
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.axis("equal")

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    return buffer.getvalue()


async def build_month_chart(today: date | None = None) -> tuple[bytes | None, dict[str, float]]:
    """PNG с расходами за текущий месяц (None, если расходов нет) и сами данные."""
    start, end = month_bounds(today)
    data = await get_expenses_by_category(start, end)
    if not data:
        return None, data
    title = f"Расходы: {MONTHS_RU[start.month - 1]} {start.year}"
    # Рендеринг блокирующий — выносим в поток, чтобы не тормозить event loop.
    png = await asyncio.to_thread(render_pie_chart, data, title)
    return png, data
