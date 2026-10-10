"""Отчёт по операциям из выписок (таблица transactions).

    python -m statements.report [--db finance.db] [--start 2026-04-01] [--big 20000]

Что считается:
  траты    = expense + refund (возвраты уменьшают траты); сюда входят комиссии и проценты по кредитке
  доход    = income (зарплата, кешбэк, разовые поступления)
  не считаются: переводы между своими счетами (transfer), служебные проводки (technical), наличные (cash_in)
  отдельно: переводы людям (p2p) и семье (family) — «чистые», то есть за вычетом полученного обратно
Крупные разовые покупки (по умолчанию от 20 000 ₽) выделены, чтобы они не искажали средние.
"""

from __future__ import annotations

import argparse
import calendar
import os
import sqlite3
from collections import defaultdict
from datetime import date
from html import escape

from . import config


def default_start(today: date | None = None) -> str:
    cfg = config.load().get("analysis_start")
    if cfg:
        return cfg
    today = today or date.today()
    y, m = today.year, today.month - 6
    while m <= 0:
        m, y = m + 12, y - 1
    return date(y, m, 1).isoformat()


def rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def build_report(conn: sqlite3.Connection, start: str | None = None, big_limit: int | None = None) -> dict | None:
    """Считает отчёт с даты start. Возвращает None, если за период нет операций."""
    start = start or default_start()
    big_limit = big_limit or int(config.load().get("big_purchase", 20000))
    if not _has_table(conn, "transactions"):
        return None
    rows = conn.execute(
        "SELECT date, type, amount_kop, category, merchant, subtype FROM transactions WHERE date >= ?", (start,)
    ).fetchall()
    if not rows:
        return None
    end = max(r[0] for r in rows)
    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)

    months = sorted({r[0][:7] for r in rows})

    def month_is_full(mon: str) -> bool:
        y, m = int(mon[:4]), int(mon[5:])
        return date(y, m, 1) >= start_d and date(y, m, calendar.monthrange(y, m)[1]) <= end_d

    full = [m for m in months if month_is_full(m)]
    by_m = {m: defaultdict(float) for m in months}
    cats: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    income_cats: dict[str, float] = defaultdict(float)
    big, merchants = [], defaultdict(lambda: [0.0, 0])
    interest = fees = 0.0
    for d, t, kop, cat, merchant, sub in rows:
        a = kop / 100.0
        mon = d[:7]
        if t in ("expense", "refund"):
            by_m[mon]["spend"] -= a
            cats[cat or "Прочее"]["total"] -= a
            cats[cat or "Прочее"][mon] -= a
            if sub == "interest":
                interest -= a
            elif sub == "fee":
                fees -= a
            if t == "expense" and -a >= big_limit:
                big.append((d, -a, cat, merchant))
            elif t == "expense" and merchant:
                merchants[merchant][0] -= a
                merchants[merchant][1] += 1
        elif t == "income":
            by_m[mon]["income"] += a
            income_cats[cat or "Прочее"] += a
        elif t == "p2p_out":
            by_m[mon]["p2p"] -= a
        elif t == "p2p_in":
            by_m[mon]["p2p"] -= a        # a > 0: уменьшает чистые переводы
        elif t == "family_out":
            by_m[mon]["family"] -= a
        elif t == "family_in":
            by_m[mon]["family"] -= a

    big.sort(key=lambda x: -x[1])
    big_months = defaultdict(float)
    for d, a, c, m in big:
        big_months[d[:7]] += a
    n_full = len(full)

    def avg(key: str) -> float | None:
        return sum(by_m[m][key] for m in full) / n_full if n_full else None

    spend_total = sum(by_m[m]["spend"] for m in months)
    big_sum = sum(a for _, a, _, _ in big)
    avg_spend = avg("spend")
    cat_rows = []
    for c, d in sorted(cats.items(), key=lambda kv: -kv[1]["total"]):
        full_sum = sum(d[m] for m in full)
        big_c = sum(a for dd, a, cc, _ in big if cc == c and dd[:7] in full)
        cat_rows.append({
            "category": c, "total": d["total"],
            "share": d["total"] / spend_total * 100 if spend_total else 0.0,
            "per_month": full_sum / n_full if n_full else None,
            "per_month_no_big": (full_sum - big_c) / n_full if n_full else None,
        })

    manual = None
    if _has_table(conn, "expenses"):
        r = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM expenses WHERE date >= ?", (start,)).fetchone()
        if r[0]:
            manual = (r[0], r[1])

    return {
        "start": start, "end": end, "months": months, "full_months": full, "by_m": by_m,
        "avg": {k: avg(k) for k in ("income", "spend", "p2p", "family")},
        "avg_spend_no_big": (avg_spend - sum(big_months[m] for m in full) / n_full) if n_full else None,
        "totals": {k: sum(by_m[m][k] for m in months) for k in ("income", "spend", "p2p", "family")},
        "big_limit": big_limit, "big": big, "big_sum": big_sum,
        "cats": cat_rows, "interest": interest, "fees": fees,
        "top_merchants": sorted(((m, v[0], v[1]) for m, v in merchants.items()), key=lambda x: -x[1])[:12],
        "income_cats": sorted(income_cats.items(), key=lambda kv: -kv[1]),
        "manual": manual,
    }


def _d(iso: str) -> str:
    return f"{iso[8:]}.{iso[5:7]}.{iso[:4]}"


def format_telegram(r: dict) -> str:
    t, a = r["totals"], r["avg"]
    lines = [
        "📈 <b>Отчёт по выпискам</b>",
        f"Период: {_d(r['start'])} — {_d(r['end'])}",
        "",
        f"Доход: <b>{rub(t['income'])}</b>",
        f"Траты (с учётом возвратов): <b>{rub(t['spend'])}</b>",
    ]
    if r["big"]:
        lines.append(f"   из них крупные разовые (от {rub(r['big_limit'])}): {rub(r['big_sum'])}, {len(r['big'])} шт.")
    lines.append(f"Переводы людям (чистые): {rub(t['p2p'])} · семье (чистые): {rub(t['family'])}")
    if r["full_months"]:
        fm = r["full_months"]
        lines += [
            "",
            f"<b>В среднем за полный месяц</b> ({fm[0]} … {fm[-1]}):",
            f"доход {rub(a['income'])} · траты {rub(a['spend'])} · без крупных {rub(r['avg_spend_no_big'])}",
        ]
    lines += ["", "<b>По категориям:</b>"]
    for c in r["cats"][:9]:
        lines.append(f"• {escape(c['category'])}: {rub(c['total'])} ({c['share']:.0f}%)")
    if r["interest"] or r["fees"]:
        lines += ["", f"Проценты по кредитке: {rub(r['interest'])} · комиссии: {rub(r['fees'])}"]
    if r["manual"]:
        n, s = r["manual"]
        lines += ["", f"<i>Ручных трат из бота за период: {n} на {rub(s)} — в отчёт не входят "
                      f"(могут дублировать покупки из выписок).</i>"]
    return "\n".join(lines)


def format_cli(r: dict) -> str:
    t, a = r["totals"], r["avg"]
    out = [f"Период: {r['start']} .. {r['end']}", "",
           "По месяцам (₽): доход | траты | переводы людям (чистые) | переводы семье (чистые)"]
    for m in r["months"]:
        d = r["by_m"][m]
        note = "" if m in r["full_months"] else "  (неполный месяц)"
        out.append(f"  {m}  {d['income']:>11,.0f} | {d['spend']:>11,.0f} | {d['p2p']:>9,.0f} | {d['family']:>9,.0f}{note}")
    if r["full_months"]:
        fm = r["full_months"]
        out += ["", f"Средние за полные месяцы {fm[0]}..{fm[-1]} ({len(fm)} мес.):",
                f"  доход {a['income']:,.0f} | траты {a['spend']:,.0f} (без крупных {r['avg_spend_no_big']:,.0f}) | "
                f"переводы людям {a['p2p']:,.0f} | семье {a['family']:,.0f}"]
    out += ["", f"Крупные разовые покупки (от {r['big_limit']:,}): {len(r['big'])} шт., {r['big_sum']:,.0f} ₽"]
    for d, amt, c, m in r["big"][:15]:
        out.append(f"  {d}  {amt:>11,.0f}  {c:<18} {m[:42]}")
    out += ["", "Траты по категориям: всего | % | в месяц | в месяц без крупных"]
    for c in r["cats"]:
        pm = f"{c['per_month']:>9,.0f}" if c["per_month"] is not None else "        -"
        pn = f"{c['per_month_no_big']:>9,.0f}" if c["per_month_no_big"] is not None else "        -"
        out.append(f"  {c['category']:<22}{c['total']:>12,.0f} {c['share']:5.1f}% | {pm} | {pn}")
    out += ["", f"Проценты по кредитке: {r['interest']:,.0f} ₽ | комиссии: {r['fees']:,.0f} ₽",
            "", "Топ получателей (без крупных разовых):"]
    for m, s, n in r["top_merchants"]:
        out.append(f"  {s:>10,.0f}  n={n:<3} {m[:46]}")
    out += ["", "Доходы:"] + [f"  {c:<16}{v:>12,.0f}" for c, v in r["income_cats"]]
    out += ["", f"Итого: доход {t['income']:,.0f} | траты {t['spend']:,.0f} | людям (чистые) {t['p2p']:,.0f} | "
                f"семье (чистые) {t['family']:,.0f}"]
    if r["manual"]:
        out.append(f"Ручных трат из бота: {r['manual'][0]} на {r['manual'][1]:,.0f} ₽ (в отчёт не входят)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Отчёт по операциям из выписок")
    ap.add_argument("--db", default=os.getenv("DB_PATH", "finance.db"))
    ap.add_argument("--start", default=None, help="дата начала YYYY-MM-DD")
    ap.add_argument("--big", type=int, default=None, help="порог крупной покупки, ₽")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        r = build_report(conn, args.start, args.big)
    finally:
        conn.close()
    if r is None:
        print("Нет операций из выписок за период. Сначала: python import_statements.py <папка_с_pdf>")
        return 1
    print(format_cli(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
