"""Тесты разбора выписок на выдуманных строках (реальные выписки в репозиторий не кладём).

Запуск из папки finance_bot:  python -m unittest discover -s tests -t . -v
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from statements import alfa, config, sber
from statements import normalize as nz
from statements import edit_sheet, insights, manual, recat, screenshot, sheets_export
from statements.importer import dedupe, write
from statements.report import build_report, format_telegram


class AlfaRows(unittest.TestCase):
    def test_row_regex_takes_amount_from_end_of_line(self):
        line = ("09.09.2026 CRD_5BR6UE Операция по карте: 220015++++++1718, на сумму: 40900.00 RUR, дата "
                "-40 900,00 RUR")
        m = alfa.RE_ROW.match(line)
        self.assertEqual(alfa.money(m["amt"]), Decimal("-40900.00"))
        self.assertEqual(m["code"], "CRD_5BR6UE")

    def _op(self, raw, amount):
        op = alfa.AlfaOp(posted=date(2026, 1, 5), code="X1", amount=Decimal(amount), raw=raw)
        alfa._classify(op)
        return op

    def test_card_purchase_parses_merchant_mcc_and_date(self):
        op = self._op(
            "Операция по карте: 220015++++++1718, на сумму: 100.00 RUR, дата совершения операции: 03.01.26, "
            "место совершения операции: 12345678\\RU\\MOSKVA\\OZON RU MCC5300", "-100.00")
        self.assertEqual(op.kind, "card_expense")
        self.assertEqual((op.mcc, op.merchant, op.city, op.card_last4), ("5300", "OZON RU", "MOSKVA", "1718"))
        self.assertEqual(op.txn_date, date(2026, 1, 3))

    def test_kinds(self):
        self.assertEqual(self._op("Заработная плата по реестру №1 от 05.01.2026", "1000").kind, "salary")
        self.assertEqual(self._op("Аванс по заработной плате по реестру №2", "500").kind, "salary")
        self.assertEqual(self._op("Внутрибанковский перевод между счетами, ИВАНОВ И. И.", "-10").kind, "own_transfer")
        self.assertEqual(self._op("Предоставление транша Дог. ABC от 030926", "10").kind, "tranche")
        self.assertEqual(self._op("Погашение ОД Дог. ABC от 030926", "-10").kind, "principal_repayment")
        self.assertEqual(self._op("Перевод C1 через Систему быстрых платежей на +79110001122. Без НДС.", "-5").kind,
                         "p2p_out")
        self.assertEqual(self._op("Перевод C1 через Систему быстрых платежей от +79110001122.", "5").kind, "p2p_in")
        self.assertEqual(self._op("Комиссия за перевод СБП", "-1").kind, "fee")
        self.assertEqual(self._op("Операция по карте: 1++++++1111, дата совершения операции: 03.01.26, "
                                  "место совершения операции: 1\\RU\\X\\SHOP MCC5411", "20").kind, "card_refund")

    def test_cloudtips_payout_is_income_not_refund(self):
        raw = ("Операция по карте: 1++++++1111, на сумму: 935.00 RUR, дата совершения операции: 01.04.26, "
               "место совершения операции: 25030184\\RU\\CloudTips Online 8398>Moskva MCC7399")
        op = self._op(raw, "935.00")
        t = nz.from_alfa([op], "alfa_0000")[0]
        self.assertEqual((t.type, t.category), ("income", "Чаевые (получено)"))
        shop = self._op(raw.replace("CloudTips Online 8398>Moskva", "OZON RU"), "935.00")
        self.assertEqual(nz.from_alfa([shop], "alfa_0000")[0].type, "refund")

    def test_phone_last4(self):
        op = self._op("Перевод C1 через Систему быстрых платежей на +7 (911) 000-11-22. Без НДС.", "-5")
        self.assertEqual(op.counterparty_last4, "1122")


class SberRows(unittest.TestCase):
    def test_row_and_money(self):
        m = sber.RE_OP.match("22.09.2026 11:09 Рестораны и кафе 1 990,00 3 829,85")
        self.assertEqual(m["cat"], "Рестораны и кафе")
        self.assertEqual(sber.money(m["amt"]), Decimal("1990.00"))
        self.assertEqual(sber.money(m["bal"]), Decimal("3829.85"))
        m = sber.RE_OP.match("10.08.2026 18:50 Прочие операции +30 000,00 214 012,40")
        self.assertTrue(m["amt"].startswith("+"))


class Categories(unittest.TestCase):
    def setUp(self):
        os.environ["STATEMENTS_CONFIG"] = "/nonexistent.json"
        config.reset()

    def test_keywords_mcc_and_bank_category(self):
        self.assertEqual(nz.categorize("MOSCOW OZON"), "Прочие товары")
        self.assertEqual(nz.categorize("YANDEX*5411*LAVKA"), "Продукты")
        self.assertEqual(nz.categorize("UNKNOWN SHOP", mcc="5814"), "Кафе и рестораны")
        self.assertEqual(nz.categorize("UNKNOWN SHOP", bank_category="Транспорт"), "Транспорт")
        self.assertEqual(nz.categorize("UNKNOWN SHOP"), "Прочее")

    def test_extra_rules_from_config_win(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write('{"extra_rules": [["MY LOCAL CAFE", "Кафе и рестораны"]], "phones": {"1122": "own"}}')
        os.environ["STATEMENTS_CONFIG"] = f.name
        config.reset()
        try:
            self.assertEqual(nz.categorize("MY LOCAL CAFE 1"), "Кафе и рестораны")
            op = alfa.AlfaOp(posted=date(2026, 1, 5), code="X", amount=Decimal("-5"),
                             raw="Перевод C1 через Систему быстрых платежей на +79110001122.")
            alfa._classify(op)
            self.assertEqual(nz.from_alfa([op], "alfa_0000")[0].type, "transfer")   # свой номер из конфига
        finally:
            os.unlink(f.name)
            os.environ["STATEMENTS_CONFIG"] = "/nonexistent.json"
            config.reset()


class ImportAndReport(unittest.TestCase):
    def _txns(self):
        mk = lambda fp, d, kop, t, cat, merchant="", sub="": nz.Txn(
            source="alfa_0000", fingerprint=fp, date=date.fromisoformat(d), posted=None, amount_kop=kop, type=t,
            category=cat, merchant=merchant, description="", subtype=sub)
        return [
            mk("a", "2026-04-02", 10_000_000, "income", "Зарплата"),
            mk("b", "2026-04-05", -150_000, "expense", "Продукты", "SHOP"),
            mk("c", "2026-04-06", 50_000, "refund", "Продукты", "SHOP"),
            mk("d", "2026-04-07", -3_000_000, "transfer", ""),
            mk("e", "2026-04-08", -2_500_000, "expense", "Здоровье", "CLINIC"),     # крупная разовая
            mk("f", "2026-05-02", 10_000_000, "income", "Зарплата"),
            mk("g", "2026-05-03", -200_000, "expense", "Продукты", "SHOP"),
            mk("h", "2026-05-04", -100_000, "p2p_out", ""),
            mk("i", "2026-05-05", 40_000, "p2p_in", ""),
        ]

    def test_dedupe_numbers_identical_rows(self):
        a, b = self._txns()[1], self._txns()[1]
        out = dedupe([a, b])
        self.assertEqual(out[0].fingerprint, "b")
        self.assertEqual(out[1].fingerprint, "b#2")

    def test_write_is_idempotent_and_report_totals(self):
        conn = sqlite3.connect(":memory:")
        self.assertEqual(write(conn, self._txns()), (9, 0))
        self.assertEqual(write(conn, self._txns()), (0, 9))      # повторная загрузка: дублей нет
        r = build_report(conn, "2026-04-01", big_limit=20_000)
        # траты = покупки 1500 + 25000 + 2000 минус возврат 500 (перевод себе 30000 не считается)
        self.assertAlmostEqual(r["totals"]["spend"], 1500 - 500 + 25_000 + 2000, places=2)
        self.assertEqual(len(r["big"]), 1)
        self.assertAlmostEqual(r["totals"]["p2p"], 600.0, places=2)                            # 1000 - 400
        self.assertEqual(r["full_months"], ["2026-04"])         # май неполный: данные кончаются 5 мая
        self.assertIn("Отчёт по выпискам", format_telegram(r))

    def test_report_without_table_or_rows(self):
        self.assertIsNone(build_report(sqlite3.connect(":memory:"), "2026-01-01"))


class Insights(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        rows = []
        for i, mon in enumerate(["2026-04", "2026-05", "2026-06"]):
            rows += [
                nz.Txn("s", f"in{i}", date.fromisoformat(mon + "-02"), None, 10_000_000, "income", "Зарплата", "", ""),
                nz.Txn("s", f"sub{i}", date.fromisoformat(mon + "-05"), None, -50_000, "expense", "Связь и подписки",
                       "SECRET VPN", ""),
                nz.Txn("s", f"f{i}", date.fromisoformat(mon + "-06"), None, -(100_000 + i * 100_000), "expense",
                       "Продукты", "SHOP" + str(i), ""),
            ]
        rows.append(nz.Txn("s", "big", date(2026, 6, 10), None, -3_000_000, "expense", "Здоровье", "KLINIKA", ""))
        write(conn, rows)
        return conn

    def test_comments_and_anonymous_summary(self):
        ins = insights.build_insights(self._conn(), "2026-04-01")
        text = "\n".join(insights.rule_comments(ins))
        self.assertIn("Регулярные списания", text)
        self.assertIn("Крупных разовых покупок: 1", text)
        summary = insights.ai_summary(ins)
        dumped = str(summary)
        for secret in ("SECRET VPN", "SHOP", "KLINIKA"):
            self.assertNotIn(secret, dumped)          # в нейросеть уходят только категории и суммы
        self.assertEqual(summary["big_purchases_count"], 1)

    def test_ai_needs_key(self):
        os.environ.pop("GEMINI_API_KEY", None)
        with self.assertRaises(RuntimeError):
            insights.ai_advice({})


SHOT = [
    "23:16 VPN 6", "История", "Пополнение Списание Даты",
    "Сегодня, 10 октября",
    "Лента −434,97 ₽ ©", "Продукты +4 ₽",
    "Перевод себе в другой ба... −3 700 ₽", "Переводы · СБП · Озон (Еком Банк)", "Перевод денежных средств",
    "Лента -4 516,97 P", "Продукты +45 ₽",
    "МТС 9000000007 -9 ₽", "Связь, интернет и ТВ",
    "Вчера, 9 октября",
    "Перевод в Сбербанк п... −24 300 ₽", "Финансовые операции", "Перевод собственных средств",
    "Перевод −30000 ₽", "Между своими счетами",
    "Иван П. −15 000 ₽", "Переводы · СБП · Сбербанк",
    "Зарплата +1 000 ₽", "Пополнение",
]


class Screenshots(unittest.TestCase):
    def setUp(self):
        os.environ["STATEMENTS_CONFIG"] = "/nonexistent.json"
        config.reset()

    def _txns(self):
        return screenshot.to_txns(screenshot.parse_lines(SHOT, date(2026, 10, 10)))

    def test_parse_classify(self):
        ts = self._txns()
        self.assertEqual([t.amount_kop for t in ts], [-43497, -370000, -451697, -900, -2430000, -3000000, -1500000, 100000])
        self.assertEqual([t.date.day for t in ts], [10, 10, 10, 10, 9, 9, 9, 9])
        self.assertEqual([t.type for t in ts], ["expense", "transfer", "expense", "expense", "transfer", "transfer",
                                                 "p2p_out", "income"])
        self.assertEqual(ts[0].category, "Продукты")
        self.assertEqual(ts[3].category, "Связь и подписки")      # по категории банка
        self.assertIn("…0007", ts[3].merchant)                    # номер телефона не сохраняем целиком
        self.assertNotIn("9000000007", ts[3].merchant)

    def test_people_from_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write('{"people": {"Иван П.": "family"}}')
        os.environ["STATEMENTS_CONFIG"] = f.name
        config.reset()
        try:
            self.assertEqual(self._txns()[6].type, "family_out")
        finally:
            os.unlink(f.name)
            os.environ["STATEMENTS_CONFIG"] = "/nonexistent.json"
            config.reset()

    def test_year_rolls_back_for_future_month(self):
        ops = screenshot.parse_lines(["31 декабря", "Кафе −100 ₽"], date(2026, 1, 5))
        self.assertEqual(ops[0].day, date(2025, 12, 31))

    def test_dupes_with_statements_both_ways(self):
        conn = sqlite3.connect(":memory:")
        shots = self._txns()
        stmt = nz.Txn("alfa_1", "st1", date(2026, 10, 9), None, -43497, "expense", "Продукты", "LENTA", "")
        write(conn, [stmt])
        dup = screenshot.find_statement_dupes(conn, shots)       # дата ±1 день, сумма та же
        self.assertEqual(dup, {shots[0].fingerprint})
        write(conn, [t for t in shots if t.fingerprint not in dup])
        stmt2 = nz.Txn("alfa_1", "st2", date(2026, 10, 10), None, -900, "expense", "Связь и подписки", "MTS", "")
        write(conn, [stmt2])
        self.assertEqual(screenshot.drop_screenshot_dupes(conn), 1)   # пришла выписка — строка скриншота убрана
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions WHERE source='screenshot' AND type!='duplicate'").fetchone()[0], 6)

    def test_ocr_error_is_readable(self):
        os.environ["TESSERACT_CMD"] = "/nonexistent"
        try:
            old, screenshot.shutil.which = screenshot.shutil.which, lambda _: None
            with self.assertRaises(RuntimeError):
                screenshot.ocr_lines("x.jpg")
        finally:
            screenshot.shutil.which = old
            os.environ.pop("TESSERACT_CMD", None)


class ManualExpenses(unittest.TestCase):
    def setUp(self):
        os.environ["STATEMENTS_CONFIG"] = "/nonexistent.json"
        config.reset()
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL, category TEXT, "
                          "description TEXT, date TEXT)")
        self.conn.execute("INSERT INTO expenses (amount, category, description, date) VALUES (450, 'Еда', 'обед', '2026-10-09')")
        self.conn.commit()

    def test_sync_is_idempotent_and_keeps_entered_category(self):
        self.assertEqual(manual.sync_manual(self.conn), 1)
        self.assertEqual(manual.sync_manual(self.conn), 0)
        row = self.conn.execute("SELECT amount_kop, category, type, source FROM transactions").fetchone()
        self.assertEqual(row, (-45000, "Еда", "expense", "manual"))

    def test_alias_from_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write('{"category_aliases": {"еда": "Продукты"}}')
        os.environ["STATEMENTS_CONFIG"] = f.name
        config.reset()
        try:
            manual.sync_manual(self.conn)
            self.assertEqual(self.conn.execute("SELECT category FROM transactions").fetchone()[0], "Продукты")
        finally:
            os.unlink(f.name)
            os.environ["STATEMENTS_CONFIG"] = "/nonexistent.json"
            config.reset()

    def test_statement_replaces_manual_row_and_it_does_not_come_back(self):
        manual.sync_manual(self.conn)
        write(self.conn, [nz.Txn("alfa_1", "st", date(2026, 10, 10), None, -45000, "expense", "Кафе и рестораны",
                                 "CAFE", "")])
        self.assertEqual(screenshot.drop_screenshot_dupes(self.conn), 1)
        manual.sync_manual(self.conn)                       # повторная синхронизация ничего не возвращает
        n = self.conn.execute("SELECT COUNT(*) FROM transactions WHERE type = 'expense'").fetchone()[0]
        self.assertEqual(n, 1)
        r = build_report(self.conn, "2026-10-01")
        self.assertAlmostEqual(r["totals"]["spend"], 450.0, places=2)    # трата посчитана один раз

    def test_category_list_has_main_categories(self):
        cats = nz.all_categories()
        for c in ("Продукты", "Транспорт", "Прочее"):
            self.assertIn(c, cats)


class FakeWS:
    id = 1
    def __init__(self): self.data = None; self.cleared = 0; self.ranges = {}
    def clear(self): self.cleared += 1
    def update(self, values, range_name, value_input_option):
        if range_name == "A1":
            self.data = values
        else:
            self.ranges[range_name] = values
    def get_all_values(self): return [[str(c) for c in r] for r in self.data]
    def freeze(self, rows): pass
    def format(self, rng, fmt): pass


class FakeSheet:
    url = "https://example.test/sheet"
    def __init__(self): self.tabs = {}
    def worksheet(self, title):
        if title not in self.tabs:
            raise KeyError(title)
        return self.tabs[title]
    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS()
        return self.tabs[title]
    def batch_update(self, body): self.last_batch = body


class SheetsExport(unittest.TestCase):
    def test_export_is_idempotent_and_summarises(self):
        conn = sqlite3.connect(":memory:")
        mk = lambda fp, d, kop, t, cat, src="alfa_0000": nz.Txn(src, fp, date.fromisoformat(d), None, kop, t, cat, "SHOP", "")
        write(conn, [mk("a", "2026-04-02", 10_000_000, "income", "Зарплата"), mk("b", "2026-04-05", -150_000, "expense", "Продукты"),
                     mk("c", "2026-04-06", 50_000, "refund", "Продукты"), mk("d", "2026-04-07", -500, "technical", ""),
                     mk("e", "2026-05-01", -200_000, "expense", "Продукты", "manual")])
        sheet = FakeSheet()
        r1 = sheets_export.export(conn, sheet, "2026-04-01")
        r2 = sheets_export.export(conn, sheet, "2026-04-01")
        self.assertEqual((r1["detail_rows"], r2["detail_rows"]), (4, 4))        # технические проводки не выгружаются
        self.assertEqual(sheet.tabs["Выписки"].cleared, 2)                      # вкладка перезаписывается, а не дополняется
        summary = {row[0]: row for row in sheet.tabs["Итоги по месяцам"].data}
        self.assertEqual(summary["Траты"][1:], [1000.0, 2000.0, 3000.0])        # апрель: 1500-500, май: 2000
        self.assertEqual(sheet.tabs["Выписки"].data[1][5], "Вручную")


if __name__ == "__main__":
    unittest.main()


class CashWithdrawal(unittest.TestCase):
    def test_atm_mcc_is_cash_category(self):
        self.assertEqual(nz.categorize("Alfa Iss", mcc="6011"), "Наличные")
        self.assertIn("Наличные", nz.all_categories())

    def test_product_categories(self):
        for merchant, mcc, want in [("GORZDRAV", "5912", "Аптека"), ("ZARA", "5651", "Одежда и обувь"),
                                    ("DNS SHOP", "5732", "Техника"), ("LEROY MERLIN", "5200", "Ремонт"),
                                    ("GOLDEN APPLE", "5977", "Косметика"), ("OZON RU", "5300", "Прочие товары"),
                                    ("SOME SHOP", "5719", "Товары для кухни"), ("KLINIKA", "8099", "Здоровье")]:
            self.assertEqual(nz.categorize(merchant, mcc=mcc), want, merchant)

    def test_old_category_name_is_renamed(self):
        self.assertNotIn("Покупки", nz.all_categories())
        self.assertEqual(nz.RENAMED.get("Покупки"), "Прочие товары")

    def test_genealogy_category(self):
        self.assertEqual(nz.categorize("ARKHIV"), "Родословная и архивы")

    def test_gifts_category(self):
        self.assertEqual(nz.categorize("CVETOCHNYJ SKLAD"), "Подарки")
        self.assertIn("Подарки", nz.all_categories())


class Recategorize(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        rows = [
            nz.Txn("s", "o1", date(2026, 9, 1), None, -150_000, "expense", "Прочие товары", "OZON RU", ""),
            nz.Txn("s", "o2", date(2026, 9, 5), None, -250_000, "expense", "Прочие товары", "OZON RU", ""),
            nz.Txn("s", "o3", date(2026, 9, 9), None, -90_000, "expense", "Прочие товары", "OZON FRESH", ""),
            nz.Txn("s", "k1", date(2026, 9, 7), None, -500_000, "expense", "Здоровье", "KLINIKA", ""),
            nz.Txn("s", "in", date(2026, 9, 2), None, 9_000_000, "income", "Зарплата", "", ""),
        ]
        write(conn, rows)
        return conn, rows

    def test_find_and_top(self):
        conn, _ = self._conn()
        found = recat.find_ops(conn, "ozon", "2026-09-01")
        self.assertEqual([o["fingerprint"] for o in found], [r.fingerprint for r in (self._conn()[1][2], self._conn()[1][1], self._conn()[1][0])])
        top = recat.top_merchants(conn, "Прочие товары", "2026-09-01")
        self.assertEqual(top[0][0], "OZON RU")
        self.assertAlmostEqual(top[0][1], 4000.0)

    def test_override_survives_reimport(self):
        conn, rows = self._conn()
        recat.set_category(conn, [rows[0].fingerprint], "Продукты")
        write(conn, rows)                               # повторная загрузка выписки сбрасывает категорию...
        recat.apply_overrides(conn)                     # ...а правки возвращают её
        cat = conn.execute("SELECT category FROM transactions WHERE fingerprint = ?", (rows[0].fingerprint,)).fetchone()[0]
        self.assertEqual(cat, "Продукты")

    def test_merchant_rule_applies_to_history_and_future(self):
        conn, rows = self._conn()
        recat.add_rule(conn, "ozon fresh", "Продукты")
        self.assertEqual(conn.execute("SELECT category FROM transactions WHERE fingerprint = ?", (rows[2].fingerprint,)).fetchone()[0], "Продукты")
        write(conn, rows)
        recat.apply_overrides(conn)
        self.assertEqual(conn.execute("SELECT category FROM transactions WHERE fingerprint = ?", (rows[2].fingerprint,)).fetchone()[0], "Продукты")

    def test_planned_is_excluded_from_no_big_figures(self):
        conn, rows = self._conn()
        rep0 = build_report(conn, "2026-09-01", big_limit=1_000_000)
        self.assertEqual(rep0["big"], [])
        recat.set_planned(conn, [rows[3].fingerprint], True)
        rep = build_report(conn, "2026-09-01", big_limit=1_000_000)
        self.assertEqual(len(rep["big"]), 1)
        health = next(c for c in rep["cats"] if c["category"] == "Здоровье")
        self.assertAlmostEqual(health["total"], 5000.0)
        self.assertAlmostEqual(health["no_big_total"], 0.0)
        self.assertIn("плановые", format_telegram(rep))


class SplitOps(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        rows = [nz.Txn("s", "oz", date(2026, 9, 12), None, -500_000, "expense", "Прочие товары", "OZON RU", ""),
                nz.Txn("s", "in", date(2026, 9, 2), None, 9_000_000, "income", "Зарплата", "", "")]
        write(conn, rows)
        return conn, rows

    def test_parse(self):
        cats = nz.all_categories()
        self.assertEqual(recat.parse_split("2000 Красота, 3000 Подарки", 500_000, cats),
                         [("Красота", 200_000), ("Подарки", 300_000)])
        self.assertEqual(recat.parse_split("2 000 красота, подарки", 500_000, cats)[1], ("Подарки", 300_000))
        with self.assertRaisesRegex(ValueError, "нет категории"):
            recat.parse_split("2000 Продукты, 2190", 419_029, cats)
        for bad in ("2000 Красота", "2000 Красота, 2000 Подарки", "2000 Красота, 3000 Нечто", "Красота, 5000 Подарки"):
            with self.assertRaises(ValueError):
                recat.parse_split(bad, 500_000, cats)

    def test_split_report_and_reimport(self):
        conn, rows = self._conn()
        recat.split(conn, "%s" % rows[0].fingerprint, [("Красота", 200_000), ("Подарки", 300_000)])
        rep = build_report(conn, "2026-09-01")
        cats = {c["category"]: c["total"] for c in rep["cats"]}
        self.assertAlmostEqual(cats["Красота"], 2000.0)
        self.assertAlmostEqual(cats["Подарки"], 3000.0)
        self.assertNotIn("Прочие товары", cats)
        self.assertAlmostEqual(rep["totals"]["spend"], 5000.0)         # общая сумма не меняется
        write(conn, rows)                                               # повторная загрузка выписки
        recat.apply_overrides(conn)
        self.assertAlmostEqual(build_report(conn, "2026-09-01")["totals"]["spend"], 5000.0)
        self.assertEqual(len(recat.find_ops(conn, "ozon", "2026-09-01")), 2)

    def test_unsplit_and_resplit(self):
        conn, rows = self._conn()
        fp = rows[0].fingerprint
        recat.split(conn, fp, [("Красота", 200_000), ("Подарки", 300_000)])
        recat.split(conn, fp, [("Подарки", 500_000 - 100_000), ("Красота", 100_000)])
        self.assertEqual(len(recat.find_ops(conn, "ozon", "2026-09-01")), 2)
        child = recat.find_ops(conn, "ozon", "2026-09-01")[0]["fingerprint"]
        self.assertEqual(recat.unsplit(conn, child), 2)
        found = recat.find_ops(conn, "ozon", "2026-09-01")
        self.assertEqual([(o["fingerprint"], o["category"]) for o in found], [(fp, "Прочие товары")])


class IgnoreCategory(unittest.TestCase):
    def test_ignored_rows_leave_the_report_and_can_be_restored(self):
        conn = sqlite3.connect(":memory:")
        rows = [nz.Txn("s", "t1", date(2026, 9, 3), None, -1_000, "expense", "Прочее", "LK.TEST.RU", ""),
                nz.Txn("s", "t2", date(2026, 9, 4), None, -200_000, "expense", "Продукты", "SHOP", "")]
        write(conn, rows)
        recat.set_category(conn, [rows[0].fingerprint], nz.IGNORE)
        self.assertAlmostEqual(build_report(conn, "2026-09-01")["totals"]["spend"], 2000.0)
        write(conn, rows)                              # повторная загрузка не возвращает тест в траты
        recat.apply_overrides(conn)
        self.assertAlmostEqual(build_report(conn, "2026-09-01")["totals"]["spend"], 2000.0)
        self.assertEqual(len(recat.find_ops(conn, "lk.test", "2026-09-01")), 1)     # его видно в /recat
        recat.set_category(conn, [rows[0].fingerprint], "Прочее")
        self.assertAlmostEqual(build_report(conn, "2026-09-01")["totals"]["spend"], 2010.0)

    def test_ignore_rule_for_merchant(self):
        conn = sqlite3.connect(":memory:")
        rows = [nz.Txn("s", "t1", date(2026, 9, 3), None, -1_000, "expense", "Прочее", "LK.TEST.RU", ""),
                nz.Txn("s", "t2", date(2026, 9, 4), None, -1_500, "expense", "Прочее", "LK.TEST.RU", "")]
        write(conn, rows)
        recat.add_rule(conn, "lk.test.ru", nz.IGNORE)
        self.assertAlmostEqual(build_report(conn, "2026-09-01")["totals"]["spend"], 0.0)


class CustomCategories(unittest.TestCase):
    def tearDown(self):
        nz.set_custom_categories([])

    def test_add_use_and_delete(self):
        conn = sqlite3.connect(":memory:")
        self.assertEqual(recat.add_category(conn, "  хобби  "), ("Хобби", True))
        self.assertIn("Хобби", nz.all_categories())
        self.assertEqual(recat.add_category(conn, "ХОББИ"), ("Хобби", False))           # без дублей
        self.assertEqual(recat.add_category(conn, "продукты"), ("Продукты", False))      # стандартная уже есть
        nz.set_custom_categories([])
        recat.load_custom(conn)                                                         # переживает перезапуск
        self.assertIn("Хобби", nz.all_categories())
        self.assertEqual(recat.delete_category(conn, "хобби"), "Хобби")
        self.assertNotIn("Хобби", nz.all_categories())
        self.assertIsNone(recat.delete_category(conn, "Продукты"))
        for bad in ("", "1", "Ё" * 31, "12345", "а/б"):
            with self.assertRaises(ValueError):
                recat.add_category(conn, bad)

    def test_split_accepts_custom_category(self):
        conn = sqlite3.connect(":memory:")
        recat.add_category(conn, "Хобби")
        self.assertEqual(recat.parse_split("1000 Хобби, Продукты", 150_000, nz.all_categories())[0], ("Хобби", 100_000))


class EditSheet(unittest.TestCase):
    def _setup(self):
        conn = sqlite3.connect(":memory:")
        rows = [nz.Txn("s", "oz1", date(2026, 9, 12), None, -500_000, "expense", "Прочие товары", "OZON RU", ""),
                nz.Txn("s", "oz2", date(2026, 9, 13), None, -90_000, "expense", "Прочие товары", "OZON FRESH", ""),
                nz.Txn("s", "kl", date(2026, 9, 14), None, -4_000_000, "expense", "Здоровье", "KLINIKA", ""),
                nz.Txn("s", "old", date(2026, 7, 1), None, -100, "expense", "Прочее", "OLD", "")]
        write(conn, rows)
        sheet = FakeSheet()
        edit_sheet.export(conn, sheet, "2026-09-01")
        return conn, sheet, sheet.tabs[edit_sheet.TAB]

    def test_export_and_apply(self):
        conn, sheet, ws = self._setup()
        self.assertEqual(len(ws.data), 4)                                   # шапка + 3 операции с сентября
        self.assertEqual(ws.data[0][4], "Новая категория")
        self.assertIn("requests", sheet.last_batch)                         # выпадающие списки заданы
        by = {r[1]: r for r in ws.data[1:]}
        by["OZON FRESH"][4] = "продукты"                                    # без учёта регистра
        by["OZON RU"][6] = "2000 Красота, Подарки"                          # разбивка
        by["KLINIKA"][5] = "да"                                             # плановая
        by["KLINIKA"][4] = "Здоровье!"                                      # опечатка -> ошибка, остальное применится
        stats = edit_sheet.apply(conn, sheet)
        self.assertEqual((stats["category"], stats["split"], stats["planned"], stats["errors"]), (1, 1, 1, 1))
        cats = {c["category"]: c["total"] for c in build_report(conn, "2026-09-01")["cats"]}
        self.assertAlmostEqual(cats["Продукты"], 900.0)
        self.assertAlmostEqual(cats["Красота"], 2000.0)
        self.assertAlmostEqual(cats["Подарки"], 3000.0)
        self.assertEqual(len(build_report(conn, "2026-09-01")["big"]), 1)   # клиника: крупная и плановая, считается один раз
        status = ws.ranges["H2:H4"]
        self.assertTrue(any("разбито" in r[0] for r in status))
        self.assertTrue(any("неизвестная категория" in r[0] for r in status))
        # повторное применение тех же данных ничего не ломает
        write(conn, [nz.Txn("s", "oz2", date(2026, 9, 13), None, -90_000, "expense", "Прочие товары", "OZON FRESH", "")])
        recat.apply_overrides(conn)
        cats = {c["category"]: c["total"] for c in build_report(conn, "2026-09-01")["cats"]}
        self.assertAlmostEqual(cats["Продукты"], 900.0)
