"""性能验收：万级事件零件的补录 < 1s；三类查询 < 50ms。"""

import random
import time
import unittest
from datetime import datetime, timedelta

from partcost import PartCost, queries

BASE = datetime(2021, 1, 1)


def seed():
    """一个热点零件 1 万条事件（5 年跨度）+ 2000 个工单。"""
    rng = random.Random(7)
    pc = PartCost(":memory:")
    wos = [f"WO{i}" for i in range(2000)]
    for w in wos:
        pc._conn.execute("INSERT INTO work_orders VALUES (?,?,?,?)",
                         (w, "2025-06-30", 100000, 20000))
    qty = amount = 0
    times = sorted(BASE + timedelta(minutes=rng.randrange(0, 5 * 365 * 24 * 60))
                   for _ in range(10000))
    events = []
    for t in times:
        if qty < 20 or rng.random() < 0.45:
            q, p = rng.randrange(10, 60), rng.randrange(100, 5000)
            qty += q
            amount += q * p
            events.append(("HOT", "in", q, p, t.isoformat(),
                           None, None, None, qty, amount))
        else:
            q = rng.randrange(1, min(10, qty) + 1)
            cost = amount if q == qty else (2 * amount * q + qty) // (2 * qty)
            qty -= q
            amount -= cost
            events.append(("HOT", "issue", q, None, t.isoformat(),
                           rng.choice(wos), None, cost, qty, amount))
    pc._conn.executemany(
        "INSERT INTO events (part_id, type, qty, unit_price, business_time,"
        " work_order_id, ref_id, cost, qty_after, amount_after)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", events)
    pc._conn.commit()
    return pc


class PerfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pc = seed()

    @classmethod
    def tearDownClass(cls):
        cls.pc.close()

    def test_backdated_mutation_under_1s(self):
        tic = time.perf_counter()
        self.pc.add_inbound("HOT", 5, 9999,
                            (BASE + timedelta(days=365)).isoformat())
        elapsed = time.perf_counter() - tic
        self.assertLess(elapsed, 1.0, f"补录耗时 {elapsed:.3f}s")

    def test_balance_at_under_50ms(self):
        tic = time.perf_counter()
        queries.balance_at(self.pc, "HOT", "2023-06-01")
        self.assertLess(time.perf_counter() - tic, 0.05)

    def test_work_order_profit_under_50ms(self):
        tic = time.perf_counter()
        queries.work_order_profit(self.pc, "WO7")
        self.assertLess(time.perf_counter() - tic, 0.05)

    def test_low_margin_under_50ms(self):
        tic = time.perf_counter()
        queries.low_margin_work_orders(self.pc, "2025-01-01", "2025-12-31")
        self.assertLess(time.perf_counter() - tic, 0.05)


if __name__ == "__main__":
    unittest.main()
