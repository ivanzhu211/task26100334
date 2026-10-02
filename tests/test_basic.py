"""基础规则：移动加权平均、四舍五入、退回、作废、恒等式。"""

import unittest

from partcost import PartCost, queries, ValidationError, NotFoundError


class BasicTest(unittest.TestCase):
    def setUp(self):
        self.pc = PartCost(":memory:")
        self.pc.upsert_work_order("WO1", "2026-01-10", 100000, 20000)

    def tearDown(self):
        self.pc.close()

    def test_moving_average(self):
        pc = self.pc
        pc.add_inbound("P", 10, 500, "2026-01-01")   # 结存 10 / 5000
        pc.add_inbound("P", 10, 700, "2026-01-02")   # 结存 20 / 12000
        e = pc.add_issue("P", "WO1", 5, "2026-01-03")
        # 领用成本 = 12000 * 5 / 20 = 3000
        row = pc._conn.execute(
            "SELECT cost FROM events WHERE id = ?", (e,)).fetchone()
        self.assertEqual(row["cost"], 3000)
        self.assertEqual(queries.balance_at(pc, "P"),
                         {"qty": 15, "amount": 9000})

    def test_rounding_half_up(self):
        pc = self.pc
        pc.add_inbound("P", 3, 100, "2026-01-01")    # 结存 3 / 300
        e = pc.add_issue("P", "WO1", 1, "2026-01-02")
        # 300 * 1 / 3 = 100
        row = pc._conn.execute(
            "SELECT cost, amount_after FROM events WHERE id = ?", (e,)).fetchone()
        self.assertEqual(row["cost"], 100)
        pc.add_inbound("P", 1, 1, "2026-01-03")      # 结存 3 / 201
        e2 = pc.add_issue("P", "WO1", 1, "2026-01-04")
        # 201 * 1 / 3 = 67
        row = pc._conn.execute(
            "SELECT cost FROM events WHERE id = ?", (e2,)).fetchone()
        self.assertEqual(row["cost"], 67)

    def test_issue_all_takes_all_amount(self):
        pc = self.pc
        pc.add_inbound("P", 3, 100, "2026-01-01")    # 300
        pc.add_issue("P", "WO1", 1, "2026-01-02")    # 100，余 2/200
        pc.add_issue("P", "WO1", 2, "2026-01-03")    # 领空，带走全部 200
        self.assertEqual(queries.balance_at(pc, "P"),
                         {"qty": 0, "amount": 0})

    def test_return_proportional(self):
        pc = self.pc
        pc.add_inbound("P", 10, 500, "2026-01-01")
        e = pc.add_issue("P", "WO1", 4, "2026-01-02")   # 成本 2000
        r = pc.add_return("P", e, 1, "2026-01-03")      # 退回 2000*1/4=500
        row = pc._conn.execute(
            "SELECT cost FROM events WHERE id = ?", (r,)).fetchone()
        self.assertEqual(row["cost"], -500)
        self.assertEqual(queries.balance_at(pc, "P"),
                         {"qty": 7, "amount": 3500})
        self.assertEqual(queries.work_order_profit(pc, "WO1")["net_cost"], 1500)

    def test_return_over_issue_rejected(self):
        pc = self.pc
        pc.add_inbound("P", 10, 500, "2026-01-01")
        e = pc.add_issue("P", "WO1", 4, "2026-01-02")
        pc.add_return("P", e, 3, "2026-01-03")
        with self.assertRaises(ValidationError):
            pc.add_return("P", e, 2, "2026-01-04")   # 3+2 > 4

    def test_return_before_issue_rejected(self):
        pc = self.pc
        pc.add_inbound("P", 10, 500, "2026-01-01")
        e = pc.add_issue("P", "WO1", 4, "2026-01-05")
        with self.assertRaises(ValidationError):
            pc.add_return("P", e, 1, "2026-01-04")

    def test_void_issue_voids_returns(self):
        pc = self.pc
        pc.add_inbound("P", 10, 500, "2026-01-01")
        e = pc.add_issue("P", "WO1", 4, "2026-01-02")
        r = pc.add_return("P", e, 1, "2026-01-03")
        pc.void_event(e)
        n = pc._conn.execute(
            "SELECT COUNT(*) c FROM events WHERE voided = 0").fetchone()["c"]
        self.assertEqual(n, 1)  # 只剩入库
        self.assertEqual(queries.balance_at(pc, "P"),
                         {"qty": 10, "amount": 5000})
        self.assertEqual(queries.work_order_profit(pc, "WO1")["net_cost"], 0)

    def test_void_inbound_recomputes(self):
        pc = self.pc
        a = pc.add_inbound("P", 10, 500, "2026-01-01")
        pc.add_inbound("P", 10, 700, "2026-01-02")
        e = pc.add_issue("P", "WO1", 5, "2026-01-03")   # 3000
        pc.void_event(a)
        # 只剩 10 件 @700，领 5 件成本 3500
        row = pc._conn.execute(
            "SELECT cost FROM events WHERE id = ?", (e,)).fetchone()
        self.assertEqual(row["cost"], 3500)

    def test_negative_stock_rejected_and_state_kept(self):
        pc = self.pc
        pc.add_inbound("P", 5, 500, "2026-01-01")
        pc.add_issue("P", "WO1", 5, "2026-01-02")
        before = list(pc._conn.execute(
            "SELECT * FROM events ORDER BY id"))
        with self.assertRaises(ValidationError):
            pc.add_issue("P", "WO1", 1, "2026-01-03")
        after = list(pc._conn.execute(
            "SELECT * FROM events ORDER BY id"))
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    def test_backdated_issue_causing_negative_rejected(self):
        pc = self.pc
        pc.add_inbound("P", 5, 500, "2026-01-01")
        pc.add_issue("P", "WO1", 5, "2026-01-02")   # 领空
        pc.add_inbound("P", 5, 500, "2026-01-03")
        # 在 01-02 前补录一笔领用，会让 01-02 的领用处结存变负
        with self.assertRaises(ValidationError):
            pc.add_issue("P", "WO1", 1, "2026-01-01T12:00:00")

    def test_void_inbound_causing_negative_rejected(self):
        pc = self.pc
        a = pc.add_inbound("P", 5, 500, "2026-01-01")
        pc.add_issue("P", "WO1", 5, "2026-01-02")
        with self.assertRaises(ValidationError):
            pc.void_event(a)
        # 状态不变
        self.assertEqual(queries.balance_at(pc, "P"),
                         {"qty": 0, "amount": 0})
        n = pc._conn.execute(
            "SELECT COUNT(*) c FROM events WHERE voided = 0").fetchone()["c"]
        self.assertEqual(n, 2)

    def test_not_found(self):
        pc = self.pc
        with self.assertRaises(NotFoundError):
            pc.void_event(999)
        with self.assertRaises(NotFoundError):
            pc.add_return("P", 999, 1, "2026-01-01")
        with self.assertRaises(NotFoundError):
            pc.add_issue("P", "NO_SUCH_WO", 1, "2026-01-01")
        with self.assertRaises(NotFoundError):
            queries.work_order_profit(pc, "NO_SUCH_WO")

    def test_balance_at_unknown_part(self):
        self.assertEqual(queries.balance_at(self.pc, "GHOST"),
                         {"qty": 0, "amount": 0})

    def test_margin_threshold_boundary(self):
        pc = self.pc
        # 收入 10000，人工 1000，配件 7000 -> 毛利正好 20%，不算低毛利
        pc.upsert_work_order("WO_E", "2026-01-10", 10000, 1000)
        pc.add_inbound("P", 7, 1000, "2026-01-01")
        pc.add_issue("P", "WO_E", 7, "2026-01-02")
        # 收入 10000，人工 1000，配件 7001 -> 毛利 19.99%，低毛利
        pc.upsert_work_order("WO_L", "2026-01-10", 10000, 1000)
        pc.add_inbound("Q", 1, 7001, "2026-01-01")
        pc.add_issue("Q", "WO_L", 1, "2026-01-02")
        low = queries.low_margin_work_orders(pc, "2026-01-01", "2026-01-31")
        ids = [w["work_order_id"] for w in low]
        self.assertNotIn("WO_E", ids)
        self.assertIn("WO_L", ids)


if __name__ == "__main__":
    unittest.main()
