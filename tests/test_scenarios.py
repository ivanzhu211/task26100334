"""验收场景：补录/作废影响历史成本与毛利，非法操作被拒绝且状态不变。"""

import unittest

from partcost import PartCost, queries, ValidationError


def snapshot(pc):
    ev = [tuple(r) for r in pc._conn.execute(
        "SELECT * FROM events ORDER BY id")]
    wo = [tuple(r) for r in pc._conn.execute(
        "SELECT * FROM work_orders ORDER BY id")]
    return ev, wo


class BackdatedScenarioTest(unittest.TestCase):
    """补录一笔早于领用的高价入库 -> 工单变低毛利；作废后恢复。"""

    def setUp(self):
        self.pc = PartCost(":memory:")
        self.pc.upsert_work_order("WO1", "2025-06-30", 100000, 20000)
        self.pc.add_inbound("FILTER", 10, 1000, "2025-01-05")
        self.pc.add_issue("FILTER", "WO1", 5, "2025-02-01")

    def tearDown(self):
        self.pc.close()

    def _low_ids(self):
        return [w["work_order_id"] for w in queries.low_margin_work_orders(
            self.pc, "2025-01-01", "2025-12-31")]

    def test_backdated_inbound_flips_margin_and_void_restores(self):
        pc = self.pc
        profit = queries.work_order_profit(pc, "WO1")
        self.assertEqual(profit["net_cost"], 5000)
        self.assertAlmostEqual(profit["margin"], 0.75)
        self.assertNotIn("WO1", self._low_ids())
        # 补录一笔更早的高价入库：10 件 @49000
        # 现存入库 10@1000 + 10@49000 = 20 件 500000，均价 25000
        late = pc.add_inbound("FILTER", 10, 49000, "2025-01-01")
        profit = queries.work_order_profit(pc, "WO1")
        self.assertEqual(profit["net_cost"], 125000)
        self.assertLess(profit["margin"], 0.20)
        self.assertIn("WO1", self._low_ids())
        # 作废这笔补录 -> 恢复合格
        pc.void_event(late)
        profit = queries.work_order_profit(pc, "WO1")
        self.assertEqual(profit["net_cost"], 5000)
        self.assertAlmostEqual(profit["margin"], 0.75)
        self.assertNotIn("WO1", self._low_ids())


class RejectKeepsStateTest(unittest.TestCase):
    """非法补录/作废被拒绝，拒绝前后所有查询结果不变。"""

    def setUp(self):
        self.pc = PartCost(":memory:")
        self.pc.upsert_work_order("WO1", "2025-06-30", 100000, 20000)
        self.pc.add_inbound("P", 5, 1000, "2025-01-01")
        self.pc.add_issue("P", "WO1", 5, "2025-02-01")   # 领空
        self.inbound2 = self.pc.add_inbound("P", 5, 2000, "2025-03-01")
        self.pc.add_issue("P", "WO1", 3, "2025-03-02")

    def tearDown(self):
        self.pc.close()

    def _all_queries(self):
        pc = self.pc
        return (
            [queries.balance_at(pc, "P", t) for t in
             ("2025-01-01", "2025-02-01", "2025-03-01", "2025-03-02",
              "2025-12-31")],
            queries.balance_at(pc, "P"),
            queries.work_order_profit(pc, "WO1"),
            queries.low_margin_work_orders(pc, "2025-01-01", "2025-12-31"),
        )

    def test_backdated_issue_rejected(self):
        pc = self.pc
        before_snap, before_q = snapshot(pc), self._all_queries()
        # 在 2025-01-15 补录领用 1 件，会让 02-01 领用处结存变负 -> 拒绝
        with self.assertRaises(ValidationError):
            pc.add_issue("P", "WO1", 1, "2025-01-15")
        self.assertEqual(snapshot(pc), before_snap)
        self.assertEqual(self._all_queries(), before_q)

    def test_void_inbound_rejected(self):
        pc = self.pc
        before_snap, before_q = snapshot(pc), self._all_queries()
        # 作废 03-01 的入库 -> 03-02 的领用处结存为负 -> 拒绝
        with self.assertRaises(ValidationError):
            pc.void_event(self.inbound2)
        self.assertEqual(snapshot(pc), before_snap)
        self.assertEqual(self._all_queries(), before_q)


if __name__ == "__main__":
    unittest.main()
