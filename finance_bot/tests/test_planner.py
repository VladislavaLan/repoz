import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import date

import database as db
from statements import planner, summary
from statements.schema import TRANSACTIONS_SCHEMA


class PlannerTests(unittest.TestCase):
    def _scenario(self, living_month=125000.0):
        items = [
            dict(date=date(2026, 10, 23), title="Аванс", amount=90000, monthly=True, priority=1),
            dict(date=date(2026, 11, 10), title="Зарплата", amount=170000, monthly=True, priority=1),
            dict(date=date(2026, 11, 6), title="ЭКО", amount=-100000, monthly=False, priority=1),
        ]
        cards = [
            planner.Card("Альфа", 151381, 300000, date(2027, 1, 31), 0, None, 58.49),
            planner.Card("Сбер", 613610, 0, None, 24250, date(2026, 11, 9), 0),
        ]
        return items, cards, planner.simulate(items, cards, 21888, date(2026, 10, 11), date(2027, 1, 31),
                                               living_month / 30.4, 30000)

    def test_monthly_occurrences(self):
        it = dict(date=date(2026, 1, 31), monthly=True)
        days = planner.occurrences(it, date(2026, 1, 1), date(2026, 4, 30))
        self.assertEqual(days, [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)])

    def test_draws_on_alfa_and_deadline(self):
        _, _, r = self._scenario()
        self.assertTrue(r.draws)                            # до зарплаты не хватает -> кредитка
        self.assertEqual({n for _, n, _ in r.draws}, {"Альфа"})
        self.assertLessEqual(r.cards_peak["Альфа"], 300000)
        self.assertIn("Альфа", r.unpaid_at_deadline)
        self.assertEqual(r.shortfall, [])

    def test_low_income_triggers_shortfall_or_unpaid(self):
        _, _, r = self._scenario(living_month=250000)
        self.assertTrue(r.shortfall or r.unpaid_at_deadline["Альфа"] > 0)

    def test_format_has_key_lines(self):
        _, cards, r = self._scenario()
        text = planner.format_result(r, cards)
        self.assertIn("План денег", text)
        self.assertIn("Альфа", text)

    def test_settings_and_items_roundtrip(self):
        conn = sqlite3.connect(":memory:")
        planner.set_setting(conn, "balance", 100.5)
        self.assertEqual(planner.get_setting(conn, "balance"), 100.5)
        i = planner.add_item(conn, date(2026, 11, 6), "ЭКО", -100000)
        self.assertEqual(planner.list_items(conn)[0]["title"], "ЭКО")
        self.assertTrue(planner.delete_item(conn, i))


class DbTests(unittest.TestCase):
    def test_spend_by_category_and_debt_update(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "t.db")
                await db.init_db(path)
                conn = sqlite3.connect(path)
                conn.executescript(TRANSACTIONS_SCHEMA)
                rows = [("a", "2026-10-02", -1000_00, "expense", "Продукты"),
                        ("b", "2026-10-03", 200_00, "refund", "Продукты"),
                        ("c", "2026-10-04", -5000_00, "technical", "Прочее"),
                        ("d", "2026-10-05", -300_00, "expense", "Кафе и рестораны")]
                for fp, dt, kop, t, cat in rows:
                    conn.execute("INSERT INTO transactions (source, fingerprint, date, amount_kop, type, category) "
                                 "VALUES ('x', ?, ?, ?, ?, ?)", (fp, dt, kop, t, cat))
                conn.commit()
                conn.close()
                got = await db.get_spend_by_category(date(2026, 10, 1), date(2026, 11, 1), path)
                self.assertEqual(got, {"Продукты": 800.0, "Кафе и рестораны": 300.0})
                debt_id = await db.add_debt("Альфа", 100.0, 58.49, 0.0, None, path)
                self.assertTrue(await db.update_debt(debt_id, path, limit_amount=300000.0, card="1718"))
                debt = (await db.get_debts(path))[0]
                self.assertEqual((debt.limit_amount, debt.card), (300000.0, "1718"))
        asyncio.run(run())

    def test_summary(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(TRANSACTIONS_SCHEMA)
        for i, (dt, kop, t, sub) in enumerate([("2026-09-05", -1000_00, "expense", ""),
                                               ("2026-09-06", -50_00, "expense", "interest"),
                                               ("2026-09-07", 5000_00, "income", "")]):
            conn.execute("INSERT INTO transactions (source, fingerprint, date, amount_kop, type, category, subtype) "
                         "VALUES ('sber_3986', ?, ?, ?, ?, 'Продукты', ?)", (str(i), dt, kop, t, sub))
        text = summary.build(conn, "2026-09-01")
        self.assertIn("Сводка", text)
        self.assertIn("sber_3986", text)


if __name__ == "__main__":
    unittest.main()


class WorkdayTests(unittest.TestCase):
    def test_shift_and_monthly(self):
        self.assertEqual(planner.shift_to_workday(date(2026, 10, 25)), date(2026, 10, 23))   # воскресенье -> пятница
        self.assertEqual(planner.shift_to_workday(date(2027, 1, 10)), date(2026, 12, 31))    # январские праздники
        it = dict(date=date(2026, 10, 25), monthly=True, workday=True)
        days = planner.occurrences(it, date(2026, 10, 11), date(2027, 1, 31))
        self.assertEqual(days, [date(2026, 10, 23), date(2026, 11, 25), date(2026, 12, 25), date(2027, 1, 25)])


class CategoryDiveTests(unittest.TestCase):
    def test_dive(self):
        import sqlite3
        from statements import category_dive
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE transactions (date TEXT, amount_kop INTEGER, type TEXT, category TEXT, merchant TEXT)")
        rows = [("2026-09-01", -100000, "expense", "Продукты", "Пятёрочка"),
                ("2026-09-05", -100000, "expense", "Продукты", "Лента"),
                ("2026-10-01", -150000, "expense", "Продукты", "Пятёрочка"),
                ("2026-10-02", -150000, "expense", "Продукты", "Лента"),
                ("2026-10-03", -150000, "expense", "Продукты", "Лента")]
        c.executemany("INSERT INTO transactions VALUES (?,?,?,?,?)", rows)
        t = category_dive.build(c, "Продукты", "2026-09-01")
        self.assertIn("2026-09", t)
        self.assertIn("числа покупок", t)
        self.assertIsNone(category_dive.build(c, "Кафе", "2026-09-01"))


class WeekTests(unittest.TestCase):
    def test_week(self):
        import sqlite3
        from datetime import date, timedelta
        from statements import week
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE transactions (date TEXT, amount_kop INTEGER, type TEXT, category TEXT, merchant TEXT, fingerprint TEXT)")
        today = date(2026, 10, 12)
        rows = []
        for k in range(1, 7):   # 6 обычных недель по 3000 ₽ продуктов
            rows.append(((today - timedelta(days=7 * k)).isoformat(), -300000, "expense", "Продукты", "x", f"a{k}"))
        rows.append((today.isoformat(), -800000, "expense", "Продукты", "x", "n1"))       # эта неделя 8000
        rows.append((today.isoformat(), -5000000, "expense", "Техника", "x", "big"))      # крупная — вне расчёта
        c.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?)", rows)
        t = week.build(c, today=today)
        self.assertIn("🔴 Продукты", t)
        self.assertIn("обычно 3 000", t)
        self.assertIn("Не входят в расчёт", t)
        c2 = sqlite3.connect(":memory:")
        c2.execute("CREATE TABLE transactions (date TEXT, amount_kop INTEGER, type TEXT, category TEXT, merchant TEXT, fingerprint TEXT)")
        c2.execute("INSERT INTO transactions VALUES ('2026-10-10',-100000,'expense','Продукты','x','z')")
        self.assertIn("Норма пока не считается", week.build(c2, today=today))


class BudgetTests(unittest.TestCase):
    def _db(self):
        import sqlite3
        from datetime import date, timedelta
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE transactions (date TEXT, amount_kop INTEGER, type TEXT, category TEXT, merchant TEXT, fingerprint TEXT)")
        today = date(2026, 10, 12)
        rows = [((today - timedelta(days=7 * k)).isoformat(), -300000, "expense", "Продукты", "x", f"a{k}") for k in range(1, 7)]
        rows.append(("2026-10-12", -500000, "expense", "Продукты", "x", "n1"))
        rows.append(("2026-10-03", -1200000, "expense", "Продукты", "x", "n2"))
        c.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?)", rows)
        return c, today

    def test_status_and_today(self):
        from statements import budget
        c, today = self._db()
        budget.set_limit(c, "Продукты", 20000)
        t = budget.status(c, today)
        self.assertIn("Продукты", t)
        self.assertIn("осталось", t)    # 17 000 из 20 000 -> 85%
        self.assertIn("🟡", t)
        self.assertIn("Всего по лимитам", budget.today_text(c, today))
        self.assertTrue(budget.warnings(c, today))

    def test_auto(self):
        from statements import budget
        c, today = self._db()
        res = budget.auto(c, today)
        self.assertEqual(res["Продукты"], 13000.0)   # 3000 в неделю * 30,4 / 7 ≈ 13 029 -> 13 000
        self.assertEqual(budget.list_limits(c)["Продукты"], 13000.0)
