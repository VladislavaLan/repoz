"""Приведение операций Сбера и Альфы к единому виду + категоризация.

Типы (type):
  expense      обычная трата (карта, платежи, комиссии, проценты по кредитке)
  refund       возврат — уменьшает траты
  income       зарплата, подарок, кешбэк
  transfer     перевод между своими счетами / на свои счета в других банках (не доход и не расход)
  technical    служебные проводки кредитки Альфы (транши, погашение ОД), увеличение лимита Сбера
  family_out / family_in   переводы семье и от семьи (номера задаются в statements_config.json)
  p2p_out / p2p_in         переводы другим людям и от них
  cash_in      внесение наличных (источник неизвестен, не доход)

Здесь только общие правила. Названия конкретных заведений и телефоны — в statements_config.json.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from . import config

# Порядок важен: первое совпадение побеждает. Проверяется по названию магазина/описанию, регистр не важен.
# Свои правила добавляйте в statements_config.json (extra_rules): они проверяются раньше этих.
IGNORE = "Не считать (тесты)"      # служебная «категория»: операция остаётся в базе, но не входит в отчёты


RULES: list[tuple[str, str]] = [
    (r"ARKHIV|ARHIV|АРХИВ|ZAGS|ЗАГС", "Родословная и архивы"),
    (r"APTEK|АПТЕК|PHARM|ГОРЗДРАВ|GORZDRAV|ЗДРАВСИТИ|ЕАПТЕКА|ВИТАМИН|VITAMIN|APTEKA", "Аптека"),
    (r"ZARA|H&M|UNIQLO|ADIDAS|NIKE|ODEZHD|ОДЕЖД|OBUV|ОБУВЬ|SPORTMASTER|LAMODA|TOPFASHION", "Одежда и обувь"),
    (r"POSUD|ПОСУД|KUHN|КУХН|HOMEPLUS", "Товары для кухни"),
    (r"LEROY|LEROI|ЛЕРУА|OBI\b|ОБИ\b|CASTORAMA|PETROVICH|ПЕТРОВИЧ|REMONT|РЕМОНТ|STROY|СТРОЙ", "Ремонт"),
    (r"DNS|М\.?ВИДЕО|MVIDEO|ELDORADO|ЭЛЬДОРАДО|CITILINK|СИТИЛИНК|RE:STORE|RESTORE|TEHNOPARK|TECHNOPARK", "Техника"),
    (r"GOLDEN APPLE|ЗОЛОТОЕ ЯБЛОКО|LETUAL|ЛЭТУАЛЬ|RIVE GAUCHE|РИВ ГОШ|SEPHORA|ПОДРУЖКА|PODRUZHKA|KOSMET|КОСМЕТ", "Косметика"),
    (r"CVETOCH|CVETY|ЦВЕТОЧН|ЦВЕТЫ|FLOWER|BUKET|БУКЕТ|PODAROK|ПОДАРОК|PODARKI|ПОДАРКИ", "Подарки"),
    (r"OZON|WILDBERRIES|\bWB\b|ALIEXPRESS|LAMODA|TOPFASHION|MARKETPLACE", "Прочие товары"),
    (r"ZOOSALON|ЗООМАГ|PETSHOP|VETERIN|ВЕТКЛИНИК", "Питомцы"),
    (r"YANDEX\s*4121|YANDEX GO|\bUBER\b|CITYMOBIL|\bRZD\b|РЖД|AVTOBUS|ПЕТЕРБУРГСКИЙ МЕТРО|\bМЕТРО\b|ТРАНСПОРТА",
     "Транспорт"),
    (r"YANDEX\*?\s*5815|YANDEX PLUS|\bPLUS\b|KINOPOISK|SPOTIFY|NETFLIX|YOUTUBE|APPLE\.COM|GOOGLE|\bМТС\b|\bMTS\b|"
     r"BEELINE|MEGAFON|TELE2", "Связь и подписки"),
    (r"ЕИРЦ|EIRC|ЖКХ|ЖКУ|\bУК\b|КОММУНАЛ|ТСЖ", "Дом и ЖКХ"),
    (r"AVIASALES|ONETWOTRIP|TRIP\.COM|OSTROVOK|\bTUTU\b|BOOKING|AIRBNB|AEROFLOT|\bS7\b", "Путешествия"),
    (r"PARIKMAHER|SALON|BEAUTY|\bNAIL|BARBER|BROW", "Красота"),
    (r"GBUZ|CLINIC|КЛИНИК|STOMAT|ЛАБОРАТОР", "Здоровье"),
    (r"KAFE|CAFE|BISTRO|PEKARNYA|ПЕКАРНЯ|COFFEE|KOFE|BREAD|RESTORAN|PIZZ|SUSHI|BURGER|KFC|MCDONALD|STARBUCKS|"
     r"EATANDSPLIT", "Кафе и рестораны"),
    (r"LAVKA|SAMOKAT|SAMO\b|\b5411\b|\*5411\*|PYATEROCHKA|PJATEROCHKA|LENTA|MAGNIT|PEREKRESTOK|VKUSVILL|DIXY|"
     r"AUCHAN|\bSPAR\b|GLOBUS|PRODUKTY", "Продукты"),
]

SBER_CATEGORY = {
    "Рестораны и кафе": "Кафе и рестораны",
    "Супермаркеты": "Продукты",
    "Транспорт": "Транспорт",
    "Путешествия": "Путешествия",
    "Здоровье и красота": "Здоровье",
    "Коммунальные платежи, связь, интернет.": "Дом и ЖКХ",
    "Все для дома": "Прочие товары",
}


def _mcc_map() -> dict[str, str]:
    m: dict[str, str] = {}

    def put(cat: str, *codes: str | range) -> None:
        for c in codes:
            for x in (c if isinstance(c, range) else [c]):
                m[str(x)] = cat

    put("Продукты", "5411", "5422", "5441", "5451", "5462", "5499", "5921")
    put("Кафе и рестораны", "5811", "5812", "5813", "5814")
    put("Транспорт", "4111", "4112", "4121", "4131", "4784", "4789", "5541", "5542", "7523", "4011", "3990", "3991")
    put("Путешествия", "4511", "4722", "7011", "4411", "4582", "7012", range(3000, 3300), range(3500, 3800))
    put("Здоровье", "8011", "8021", "8031", "8041", "8042", "8043", "8049", "8050", "8062", "8071", "8099", "5122")
    put("Аптека", "5912")
    put("Красота", "7230", "7297", "7298")
    put("Косметика", "5977")
    put("Наличные", "6010", "6011")        # снятие в банкомате: куда ушли деньги, выписка не знает
    put("Питомцы", "5995", "0742")
    put("Связь и подписки", "4814", "4899", "5815", "5816", "5817", "5818", "7372")
    put("Дом и ЖКХ", "4900", "6513")
    put("Развлечения", "7832", "7922", "7929", "7991", "7996", "7999", "5735", "5970")
    put("Одежда и обувь", "5611", "5621", "5631", "5651", "5655", "5661", "5681", "5691", "5697", "5698", "5699",
        "5137", "5139")
    put("Техника", "5732", "5734", "5722", "5045", "5946")
    put("Ремонт", "5200", "5211", "5231", "5251", "5039", "5072", "1520", "1711", "1731", "1740", "1750", "1761", "1771")
    put("Товары для кухни", "5719")
    put("Прочие товары", "5311", "5300", "5945", "5942", "5947", "5999", "5712", "5948", "5964", "5965", "5969",
        "5310", "5331", "5399")
    return m


MCC = _mcc_map()


RENAMED = {"Покупки": "Прочие товары"}      # старое название категории -> новое (в том числе в ваших extra_rules)


def categorize(merchant: str, mcc: str = "", bank_category: str = "") -> str:
    cat = _categorize(merchant, mcc, bank_category)
    return RENAMED.get(cat, cat)


def _categorize(merchant: str, mcc: str = "", bank_category: str = "") -> str:
    text = merchant.upper()
    rules = [(rx, cat) for rx, cat in config.load().get("extra_rules", [])] + RULES
    for rx, cat in rules:
        if re.search(rx, text, re.IGNORECASE):
            return cat
    if mcc and mcc in MCC:
        return MCC[mcc]
    if bank_category in SBER_CATEGORY:
        return SBER_CATEGORY[bank_category]
    return "Прочее"


def all_categories() -> list[str]:
    """Известные категории (для выбора в боте)."""
    cats = {c for _, c in RULES} | set(MCC.values()) | set(SBER_CATEGORY.values())
    cats |= {c for _, c in config.load().get("extra_rules", [])}
    cats |= set(config.load().get("category_aliases", {}).values())
    cats |= {"Прочее", "Развлечения", "Проценты и комиссии", "Подарки", "Наличные", "Косметика", "Одежда и обувь",
             "Техника", "Аптека", "Ремонт", "Товары для кухни", "Прочие товары", "Родословная и архивы", IGNORE}
    cats.discard("Чаевые (получено)")
    return sorted({RENAMED.get(c, c) for c in cats})


@dataclass
class Txn:
    source: str
    fingerprint: str
    date: date
    posted: date | None
    amount_kop: int          # со знаком: + пришло, - ушло (с точки зрения счёта)
    type: str
    category: str
    merchant: str
    description: str
    mcc: str = ""
    bank_category: str = ""
    counterparty: str = ""
    card_last4: str = ""
    subtype: str = ""        # исходный тип парсера
    derived: int = 0         # 1 = строка добавлена нами (проценты), в сверку остатков не входит


def kop(d: Decimal) -> int:
    return int((d * 100).to_integral_value())


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def from_alfa(ops, source: str) -> list[Txn]:
    phones = config.load().get("phones", {})
    out: list[Txn] = []
    for o in ops:
        t = Txn(
            source=source, fingerprint=o.fingerprint, date=o.date, posted=o.posted,
            amount_kop=kop(o.amount), type="", category="", merchant=o.merchant,
            description=o.raw[:160], mcc=o.mcc, card_last4=o.card_last4, subtype=o.kind,
        )
        k = o.kind
        if k == "card_expense":
            t.type, t.category = "expense", categorize(o.merchant, o.mcc)
        elif k == "card_refund" and re.search(r"CLOUDTIPS", o.merchant, re.IGNORECASE):
            # выплата полученных чаевых на карту — это доход, а не возврат покупки
            t.type, t.category = "income", "Чаевые (получено)"
        elif k == "card_refund":
            t.type, t.category = "refund", categorize(o.merchant, o.mcc)
        elif k == "payment":
            t.type, t.category = "expense", categorize(o.merchant or o.raw)
        elif k == "fee":
            t.type, t.category = "expense", "Проценты и комиссии"
        elif k == "salary":
            t.type, t.category = "income", "Зарплата"
        elif k == "income_other":
            t.type, t.category = "income", "Прочие доходы"
        elif k == "cashback":
            t.type, t.category = "income", "Кешбэк"
        elif k in ("own_transfer", "own_transfer_ext"):
            t.type = "transfer"
        elif k in ("tranche", "principal_repayment"):
            t.type = "technical"
        elif k == "cash_deposit":
            t.type = "cash_in"
        elif k in ("p2p_out", "p2p_in"):
            role = phones.get(o.counterparty_last4, "")
            t.counterparty = f"..{o.counterparty_last4}" if o.counterparty_last4 else ""
            if role == "own":
                t.type = "transfer"
            elif role == "family":
                t.type = "family_out" if o.amount < 0 else "family_in"
            else:
                t.type = k
        else:
            t.type = "unknown"
        out.append(t)
    return out


def from_sber(ops, source: str) -> list[Txn]:
    out: list[Txn] = []
    for o in ops:
        t = Txn(
            source=source, fingerprint=o.fingerprint, date=o.op_dt.date(),
            posted=date.fromisoformat("-".join(reversed(o.proc_date.split(".")))) if o.proc_date else None,
            amount_kop=kop(o.amount), type="", category="", merchant=o.description,
            description=o.description[:160], bank_category=o.category, subtype=o.kind,
        )
        if o.kind == "expense":
            t.type, t.category = "expense", categorize(o.description, "", o.category)
        elif o.kind == "refund":
            t.type, t.category = "refund", categorize(o.description, "", "")
        elif o.kind == "transfer_in":
            t.type = "transfer"
        elif o.kind == "limit_increase":
            t.type = "technical"
        elif o.kind == "income":
            t.type, t.category = "income", "Прочие доходы"
        else:
            t.type = "unknown"
        out.append(t)
        if o.interest_part:
            out.append(Txn(
                source=source, fingerprint=_fp("sber-interest", o.fingerprint), date=o.op_dt.date(),
                posted=None, amount_kop=-kop(o.interest_part), type="expense", category="Проценты и комиссии",
                merchant="Проценты по кредитной карте Сбера", description="Погашение процентов",
                subtype="interest", derived=1,
            ))
    return out
