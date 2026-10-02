"""随机混合乱序录入、补录、退回、作废；每步后与“从头重算”的 oracle 对比。"""

import random
import unittest
from datetime import datetime, timedelta
from fractions import Fraction

from partcost import PartCost, queries, ValidationError

START = datetime(2024, 1, 1)
END = datetime(2025, 12, 31)
SPAN_HOURS = int((END - START).total_seconds() // 3600)


def rand_time(rng):
    return (START + timedelta(hours=rng.randrange(SPAN_HOURS))).isoformat()


def round_half_up(numer, denom):
    return (2 * numer + denom) // (2 * denom)


class Oracle:
    """独立重算：把现存事件按业务时间从头走一遍，并校验全部恒等式。"""

    def __init__(self, pc):
        self.parts = {}          # part_id -> {time -> (qty, amount)}
        self.wo_net = {}         # wo_id -> 净成本
        self._build(pc)

    def _build(self, pc):
        rows = [dict(r) for r in pc._conn.execute(
            "SELECT id, part_id, type, qty, unit_price, business_time,"
            "       work_order_id, ref_id FROM events WHERE voided = 0")]
        by_part = {}
        for r in rows:
            by_part.setdefault(r["part_id"], []).append(r)
        for part_id, evs in by_part.items():
            evs.sort(key=lambda r: (r["business_time"], r["id"]))
            qty = amount = 0
            sum_in = sum_issue = sum_return = 0
            issue_cost = {}
            timeline = {}
            for r in evs:
                if r["type"] == "in":
                    qty += r["qty"]
                    amount += r["qty"] * r["unit_price"]
                    sum_in += r["qty"] * r["unit_price"]
                elif r["type"] == "issue":
                    assert r["qty"] <= qty, "oracle: 负库存"
                    cost = amount if r["qty"] == qty else round_half_up(
                        amount * r["qty"], qty)
                    qty -= r["qty"]
                    amount -= cost
                    sum_issue += cost
                    issue_cost[r["id"]] = (cost, r["qty"])
                    self.wo_net[r["work_order_id"]] = \
                        self.wo_net.get(r["work_order_id"], 0) + cost
                else:
                    src_cost, src_qty = issue_cost[r["ref_id"]]
                    back = round_half_up(src_cost * r["qty"], src_qty)
                    qty += r["qty"]
                    amount += back
                    sum_return += back
                    self.wo_net[r["work_order_id"]] = \
                        self.wo_net.get(r["work_order_id"], 0) - back
                assert qty >= 0
                if qty == 0:
                    assert amount == 0, "oracle: 数量为 0 金额必须为 0"
                # 恒等式：入库合计 = 领用合计 - 退回合计 + 结存金额
                assert sum_in == sum_issue - sum_return + amount
                timeline[r["business_time"]] = (qty, amount)
            self.parts[part_id] = timeline

    def balance_at(self, part_id, at):
        best = (0, 0)
        for t, v in self.parts.get(part_id, {}).items():
            if t <= at:
                best = v
        return best

    def latest(self, part_id):
        ts = self.parts.get(part_id, {})
        return ts[max(ts)] if ts else (0, 0)


class RandomConsistencyTest(unittest.TestCase):
    def test_random_ops_match_oracle(self):
        rng = random.Random(20261003)
        pc = PartCost(":memory:")
        parts = [f"P{i}" for i in range(6)]
        wos = [f"WO{i}" for i in range(12)]
        for wo in wos:
            pc.upsert_work_order(
                wo,
                (START + timedelta(days=rng.randrange(730))).isoformat(),
                rng.randrange(5000, 200000), rng.randrange(0, 50000))
        live_issues = []   # [event_id, part_id, business_time, 剩余可退数量]
        voidable = []      # 未作废事件 id

        def dump():
            return [tuple(r) for r in pc._conn.execute(
                "SELECT * FROM events ORDER BY id")]

        def verify_full():
            oracle = Oracle(pc)
            for p in parts:
                times = {"2023-01-01", "2026-06-01"}
                times.update(oracle.parts.get(p, {}).keys())
                for t in times:
                    got = queries.balance_at(pc, p, t)
                    exp = oracle.balance_at(p, t)
                    self.assertEqual((got["qty"], got["amount"]), exp,
                                     f"balance {p} @ {t}")
                got = queries.balance_at(pc, p)
                self.assertEqual((got["qty"], got["amount"]), oracle.latest(p))
            for wo in wos:
                profit = queries.work_order_profit(pc, wo)
                self.assertEqual(profit["net_cost"],
                                 oracle.wo_net.get(wo, 0), f"net {wo}")
            low = queries.low_margin_work_orders(pc, "2024-01-01", "2025-12-31")
            wo_rows = {r["id"]: r for r in pc._conn.execute(
                "SELECT * FROM work_orders")}
            exp_low = []
            for wo in wos:
                w = wo_rows[wo]
                net = oracle.wo_net.get(wo, 0)
                margin = Fraction(w["revenue"] - w["labor"] - net,
                                  w["revenue"])
                if margin < Fraction(1, 5):
                    exp_low.append(wo)
            self.assertEqual(sorted(w["work_order_id"] for w in low),
                             sorted(exp_low))

        N = 400
        for step in range(N):
            op = rng.random()
            before = dump()
            try:
                if op < 0.35:  # 入库（时间随机，含补录到很早）
                    eid = pc.add_inbound(rng.choice(parts),
                                         rng.randrange(1, 15),
                                         rng.randrange(1, 3000),
                                         rand_time(rng))
                    voidable.append(eid)
                elif op < 0.70:  # 领用
                    eid = pc.add_issue(rng.choice(parts), rng.choice(wos),
                                       rng.randrange(1, 8), rand_time(rng))
                    voidable.append(eid)
                    row = pc._conn.execute(
                        "SELECT part_id, business_time, qty FROM events"
                        " WHERE id = ?", (eid,)).fetchone()
                    live_issues.append([eid, row["part_id"],
                                        row["business_time"], row["qty"]])
                elif op < 0.85:  # 退回
                    cand = [x for x in live_issues if x[3] > 0]
                    if not cand:
                        continue
                    iss = rng.choice(cand)
                    qty = rng.randrange(1, iss[3] + 1)
                    t0 = datetime.fromisoformat(iss[2])
                    lo = max(0, int((t0 - START).total_seconds() // 3600))
                    t = (START + timedelta(
                        hours=rng.randrange(lo, SPAN_HOURS))).isoformat()
                    eid = pc.add_return(iss[1], iss[0], qty, t)
                    voidable.append(eid)
                    iss[3] -= qty
                else:  # 作废
                    if not voidable:
                        continue
                    eid = rng.choice(voidable)
                    pc.void_event(eid)
                    gone = {r["id"] for r in pc._conn.execute(
                        "SELECT id FROM events WHERE voided = 1")}
                    voidable = [x for x in voidable if x not in gone]
                    live_issues = [x for x in live_issues if x[0] not in gone]
            except ValidationError:
                # 被拒绝后状态必须保持不变
                self.assertEqual(dump(), before, f"step {step} 状态变了")
                continue
            if step % 20 == 0 or step == N - 1:
                verify_full()
        pc.close()


if __name__ == "__main__":
    unittest.main()
