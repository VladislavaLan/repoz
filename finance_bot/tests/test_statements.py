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
from statements import insights, manual, screenshot
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
        self.assertEqual(nz.categorize("MOSCOW OZON"), "Покупки")
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


if __name__ == "__main__":
    unittest.main()
