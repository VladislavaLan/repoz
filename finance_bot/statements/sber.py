"""Парсер PDF-выписки Сбера по счёту кредитной карты.

Формат операции в выписке:
    DD.MM.YYYY HH:MM <категория> <сумма> <остаток>
    DD.MM.YYYY <код> <описание>   (описание может переноситься на след. строки)
    [Погашение процентов N]       (только у пополнений, относится к ним)

Порядок операций в файле НЕ хронологический, поэтому парсер от порядка не зависит.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import pdfplumber

NUM = r"\d{1,3}(?:[\s ]\d{3})*,\d{2}"
RE_OP = re.compile(
    rf"^(?P<d>\d{{2}}\.\d{{2}}\.\d{{4}}) (?P<t>\d{{2}}:\d{{2}}) "
    rf"(?P<cat>.+?) (?P<amt>\+?{NUM}) (?P<bal>{NUM})$"
)
RE_DESC = re.compile(r"^(?P<pd>\d{2}\.\d{2}\.\d{4}) (?P<code>\d{6}) (?P<txt>.*)$")
RE_INTEREST = re.compile(rf"^Погашение процентов (?P<amt>{NUM})$")
RE_NOISE = re.compile(
    r"^(Выписка по счёту|ДАТА ОПЕРАЦИИ|Дата обработки|и код авторизации|"
    r"Продолжение на следующей|\*$|Дата формирования)"
)


def money(s: str) -> Decimal:
    return Decimal(re.sub(r"[\s +]", "", s).replace(",", "."))


@dataclass
class Op:
    op_dt: datetime
    category: str
    amount: Decimal          # со знаком: + пополнение, - списание
    balance_after: Decimal
    proc_date: str = ""
    auth_code: str = ""
    description: str = ""
    interest_part: Decimal = Decimal("0")   # «Погашение процентов» у пополнения
    kind: str = ""
    page: int = 0
    _desc_lines: list[str] = field(default_factory=list, repr=False)

    @property
    def fingerprint(self) -> str:
        raw = f"sber|{self.op_dt:%Y%m%d%H%M}|{self.amount}|{self.auth_code}|{self.description}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


def classify(op: Op) -> str:
    d = op.description.lower()
    if op.amount > 0:
        if "пополнение счета" in d:
            return "transfer_in"       # свои деньги на погашение карты, не доход
        if "кредитного лимита" in d:
            return "limit_increase"    # технически, не доход
        if "возврат" in op.category.lower() or "отмена" in d:
            return "refund"
        return "income"
    return "expense"


def parse_header(text: str) -> dict:
    def grab(pattern: str) -> Decimal | None:
        m = re.search(pattern, text)
        return money(m.group(1)) if m else None

    return {
        "opening": grab(rf"Остаток на 01\.01\.\d{{4}} ({NUM})"),
        "topups": grab(rf"Пополнение ({NUM})"),
        "debits": grab(rf"Списание ({NUM})"),
        "bank_debits": grab(rf"с учётом списаний Банка ({NUM})"),
        "closing": (
            money(re.findall(rf"Остаток на \d{{2}}\.\d{{2}}\.\d{{4}} ({NUM})", text)[-1])
            if re.search(rf"Остаток на \d{{2}}\.\d{{2}}\.\d{{4}} ({NUM})", text)
            else None
        ),
    }


def parse(path: str) -> tuple[list[Op], dict]:
    ops: list[Op] = []
    cur: Op | None = None
    mode = ""  # "desc" — собираем перенос описания
    header_text = ""
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            if pno == 1:
                header_text = text
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                m = RE_OP.match(line)
                if m:
                    amt = money(m["amt"])
                    cur = Op(
                        op_dt=datetime.strptime(f"{m['d']} {m['t']}", "%d.%m.%Y %H:%M"),
                        category=m["cat"].strip(),
                        amount=amt if m["amt"].startswith("+") else -amt,
                        balance_after=money(m["bal"]),
                        page=pno,
                    )
                    ops.append(cur)
                    mode = ""
                    continue
                if cur is None:
                    continue
                m = RE_INTEREST.match(line)
                if m:
                    cur.interest_part = money(m["amt"])
                    mode = ""
                    continue
                m = RE_DESC.match(line)
                if m and not cur.auth_code:
                    cur.proc_date, cur.auth_code = m["pd"], m["code"]
                    cur._desc_lines = [m["txt"]]
                    mode = "desc"
                    continue
                if RE_NOISE.match(line):
                    mode = ""
                    continue
                if mode == "desc":
                    cur._desc_lines.append(line)

    for op in ops:
        txt = " ".join(op._desc_lines)
        txt = re.sub(r"\.?\s*Операция по (карте|счету)\s*\*{4}\d{4}\.?", "", txt)
        op.description = re.sub(r"\s+", " ", txt).strip(" .")
        op.kind = classify(op)
    return ops, parse_header(header_text)
