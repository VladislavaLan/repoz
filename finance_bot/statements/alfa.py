"""Парсер PDF-выписок Альфа-Банка: кредитная и дебетовая (зарплатная) карты.

Формат строки операции:
    DD.MM.YYYY <код> <описание ... >  <сумма> RUR
    (описание переносится на следующие строки, пока не начнётся новая строка с датой)

Раздел «HOLD Неподтвержденная операция» (после основной таблицы) в основную таблицу не входит,
но банк учитывает его в «Расходы» и в остатке, поэтому парсится отдельно.

Kind (нормализованный тип):
  card_expense / card_refund   операция по карте (покупка / возврат)
  payment                      платёж через СБП / в пользу организации (ЖКХ, МТС, метро, магазины по QR)
  p2p_out / p2p_in             переводы людям / от людей (СБП, «Перевод другу» и т. п.)
  own_transfer                 внутрибанковский перевод между своими счетами (в обе стороны)
  own_transfer_ext             «Перевод собственных средств» (на свой счёт в другом банке)
  cash_deposit                 внесение наличных через банкомат
  salary                       зарплата и аванс от работодателя (доход)
  income_other                 разовые поступления (подарок от работодателя и т. п.)
  cashback                     кешбэк
  fee                          комиссии банка
  tranche / principal_repayment  технические проводки кредитки (в расходы/доходы не идут)
  other                        не распознано — должно быть пусто
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import pdfplumber

NUM = r"-?\d{1,3}(?:[  ]\d{3})*,\d{2}"
RE_ROW = re.compile(
    rf"^(?P<posted>\d{{2}}\.\d{{2}}\.\d{{4}}) (?P<code>\S+) (?P<desc>.*?)\s*(?P<amt>{NUM}) RUR$"
)
RE_ROW_START = re.compile(r"^\d{2}\.\d{2}\.\d{4} \S+ ")
RE_HOLD_AMT = re.compile(rf"^HOLD .*?(?P<amt>{NUM}) RUR$")
RE_STOP = re.compile(r"^(А\.А\.|Уполномоченное лицо|\(подпись|Страница \d+ из|Дата проводки|в валюте счета)")
RE_TXN_DATE = re.compile(r"дата\s+совершения\s+операции:\s*(\d{2}\.\d{2}\.\d{2})")
RE_PLACE = re.compile(r"место\s+совершения\s+операции:\s*(.+)$", re.S)
RE_MCC = re.compile(r"MCC(\d{4})")
RE_CARD = re.compile(r"Операция по карте:\s*\d+\++(\d{4})")
RE_PAYEE = [
    re.compile(r"^Платеж \S+ в пользу (?P<p>[^,]+?)(?:,|$)"),
    re.compile(r"^Платеж \S+ в (?P<p>.+?) через Систему"),
    re.compile(r"^Платеж \S+ в (?P<p>.+?)(?:\. |$)"),
]
RE_PHONE = re.compile(r"(?:на|от) (\+7[\d\s()\-]*\d)")


def money(s: str) -> Decimal:
    return Decimal(re.sub(r"[\s ]", "", s).replace(",", "."))


@dataclass
class AlfaOp:
    posted: date
    code: str
    amount: Decimal                 # со знаком, как в выписке
    raw: str = ""
    kind: str = ""
    txn_date: date | None = None    # реальная дата покупки (если известна)
    merchant: str = ""              # магазин / получатель платежа
    city: str = ""
    mcc: str = ""
    card_last4: str = ""
    counterparty_last4: str = ""    # последние 4 цифры телефона получателя/отправителя перевода
    page: int = 0
    _lines: list[str] = field(default_factory=list, repr=False)

    @property
    def date(self) -> date:
        return self.txn_date or self.posted

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(f"alfa|{self.code}|{self.posted}|{self.amount}".encode()).hexdigest()[:16]


def _card_op(op: AlfaOp, t: str) -> None:
    op.kind = "card_expense" if op.amount < 0 else "card_refund"
    if m := RE_CARD.search(t):
        op.card_last4 = m[1]
    if m := RE_TXN_DATE.search(t):
        op.txn_date = datetime.strptime(m[1], "%d.%m.%y").date()
    if m := RE_PLACE.search(t):
        place = m[1].strip()
        if mm := RE_MCC.search(place):
            op.mcc = mm[1]
        place = re.sub(r"\(AP-\d+\)|\(ALFA PAY-\d+\)", "", RE_MCC.sub("", place))
        parts = [p.strip() for p in place.split("\\")]
        if len(parts) >= 4:
            op.city, op.merchant = parts[2], " ".join(parts[3:])
        else:
            op.merchant = parts[-1]
        op.merchant = re.sub(r"\s+", " ", op.merchant).strip()


def _classify(op: AlfaOp) -> None:
    t = op.raw
    neg = op.amount < 0
    if "Предоставление транша" in t:
        op.kind = "tranche"
    elif "Погашение ОД" in t:
        op.kind = "principal_repayment"
    elif "заработн" in t.lower():
        op.kind = "salary"   # «Заработная плата по реестру…», «Аванс по заработной плате…»
    elif t.startswith("Денежный подарок"):
        op.kind = "income_other"   # подарок/выплата от работодателя вне зарплатного реестра
    elif "Внутрибанковский перевод между счетами" in t:
        op.kind = "own_transfer"
    elif "Перевод собственных средств" in t:
        op.kind = "own_transfer_ext"
    elif re.search(r"перевод на сч[её]т Дог\.", t):   # перевод между своим текущим и накопительным счётом
        op.kind = "own_transfer"
    elif t.startswith("Операция по карте"):
        _card_op(op, t)
    elif "Кэшбэк" in t or "CashBack" in t or "cashback" in t.lower():
        op.kind = "cashback"
    elif t.startswith("Комиссия") or t.startswith("ВЫДАЧУ СПРАВКИ"):
        op.kind = "fee"
    elif t.startswith("Внесение средств"):
        op.kind = "cash_deposit"
    elif t.startswith("Платеж"):
        op.kind = "payment"
        for rx in RE_PAYEE:
            if m := rx.search(t):
                op.merchant = m["p"].strip()
                break
    elif t.startswith("Перевод") or t.startswith("Возврат ошибочно"):
        op.kind = "p2p_out" if neg else "p2p_in"
        if m := RE_PHONE.search(t):
            digits = re.sub(r"\D", "", m[1])
            op.counterparty_last4 = digits[-4:] if len(digits) >= 4 else ""
        if "Возврат ошибочно" in t:
            op.merchant = "возврат ошибочного перевода"
    else:
        op.kind = "other"


def parse_header(text: str) -> dict:
    def grab(label: str) -> Decimal | None:
        m = re.search(rf"{label}\s+({NUM}) RUR", text)
        return money(m[1]) if m else None

    m_type = re.search(r"Тип счета\s+(.+)", text)
    m_period = re.search(r"За период с (\S+) по (\S+)", text)
    return {
        "opening": grab("Входящий остаток"),
        "credits": grab("Поступления"),
        "debits": grab("Расходы"),
        "closing": grab("Исходящий остаток"),
        "pending": grab("Неподтвержденные операции"),
        "debt": grab("Общая задолженность к погашению"),
        "account_type": m_type[1].strip() if m_type else "",
        "period": (m_period[1], m_period[2]) if m_period else ("", ""),
    }


def parse(path: str) -> tuple[list[AlfaOp], list[Decimal], dict]:
    """Возвращает (операции основной таблицы, суммы неподтверждённых операций, шапка)."""
    ops: list[AlfaOp] = []
    holds: list[Decimal] = []
    cur: AlfaOp | None = None
    header_text = ""
    in_table = False
    in_hold = False
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            if pno == 1:
                header_text = text
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("Операции по счету"):
                    in_table = True
                    continue
                if not in_table:
                    continue
                if line.startswith("HOLD"):
                    in_hold = True
                    if m := RE_HOLD_AMT.match(line):
                        a = money(m["amt"])
                        if a != 0:
                            holds.append(a)
                    continue
                if in_hold:
                    # продолжения HOLD-строк; сумма всегда в первой строке
                    continue
                if RE_STOP.match(line):
                    continue
                m = RE_ROW.match(line)
                if m and RE_ROW_START.match(line):
                    cur = AlfaOp(
                        posted=datetime.strptime(m["posted"], "%d.%m.%Y").date(),
                        code=m["code"],
                        amount=money(m["amt"]),
                        page=pno,
                    )
                    cur._lines = [m["desc"]]
                    ops.append(cur)
                elif cur is not None:
                    cur._lines.append(line)
    for op in ops:
        op.raw = re.sub(r"\s+", " ", " ".join(op._lines)).strip()
        _classify(op)
    return ops, holds, parse_header(header_text)
