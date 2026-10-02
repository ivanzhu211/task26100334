"""随机混合乱序录入/补录/退回/作废，与从头重算的对照模型逐步比对。"""

import random

import pytest

from parts_cost import Engine, PartsCostError

PARTS = [f"P{i:02d}" for i in range(8)]
WO_COUNT = 40
FAR_FUTURE = "2099-01-01T00:00:00"


def round_half_up(numerator, denominator):
    return (2 * numerator + denominator) // (2 * denominator)


class Oracle:
    """对照模型：把现存事件按业务时间从头重算。"""

    def __init__(self):
        self.events = {}          # id -> dict
        self.work_orders = {}     # id -> (finish_date, revenue, labor)
        self.qty_after = {}       # (part, 业务时间点) 列表在 recompute 时算
        self.balances = {}        # part -> [(bt, id, qty_after, amount_after)]
        self.wo_net = {}

    def add_event(self, **kw):
        self.events[kw["id"]] = kw

    def void(self, event_id):
        ev = self.events[event_id]
        ev["voided"] = True
        if ev["type"] == "issue":
            for other in self.events.values():
                if other.get("issue_event_id") == event_id:
                    other["voided"] = True

    def recompute(self):
        self.balances = {p: [] for p in PARTS}
        self.wo_net = {w: 0 for w in self.work_orders}
        by_part = {p: [] for p in PARTS}
        for ev in self.events.values():
            if not ev["voided"]:
                by_part[ev["part"]].append(ev)
        for part, evs in by_part.items():
            evs.sort(key=lambda e: (e["bt"], e["id"]))
            qty = amount = 0
            issues = {}
            for ev in evs:
                if ev["type"] == "in":
                    qty += ev["qty"]
                    amount += ev["qty"] * ev["price"]
                elif ev["type"] == "issue":
                    cost = round_half_up(amount * ev["qty"], qty)
                    qty -= ev["qty"]
                    amount -= cost
                    issues[ev["id"]] = (ev["qty"], cost)
                    self.wo_net[ev["wo"]] += cost
                else:
                    issue_qty, issue_cost = issues[ev["issue_event_id"]]
                    cost = round_half_up(issue_cost * ev["qty"], issue_qty)
                    qty += ev["qty"]
                    amount += cost
                    self.wo_net[ev["wo"]] -= cost
                assert qty >= 0, f"对照模型出现负库存: {part} {ev}"
                if qty == 0:
                    assert amount == 0
                self.balances[part].append((ev["bt"], ev["id"], qty, amount))

    def balance_at(self, part, t):
        qty = amount = 0
        for bt, _id, q, a in self.balances[part]:
            if bt <= t:
                qty, amount = q, a
            else:
                break
        return qty, amount

    def is_low(self, wo):
        finish, revenue, labor = self.work_orders[wo]
        return 5 * (revenue - labor - self.wo_net[wo]) < revenue


def random_time(rng, used_times):
    if used_times and rng.random() < 0.25:
        return rng.choice(used_times)
    t = (f"202{rng.randint(4, 5)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
         f"T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:00")
    used_times.append(t)
    return t


def check_work_orders(engine, oracle):
    for wo in oracle.work_orders:
        cost = engine.work_order_cost(wo)
        assert cost.parts_net_cents == oracle.wo_net[wo], f"工单 {wo} 净成本不符"
        assert cost.is_low_margin == oracle.is_low(wo), f"工单 {wo} 低毛利标记不符"
    low_engine = {w.work_order_id for w in
                  engine.low_margin_work_orders("2024-01-01", "2025-12-31")}
    low_oracle = {w for w in oracle.work_orders if oracle.is_low(w)}
    assert low_engine == low_oracle


def check_balances(engine, oracle, times):
    for part in PARTS:
        for t in times:
            bal = engine.balance_at(part, t)
            assert (bal.qty, bal.amount_cents) == oracle.balance_at(part, t), (
                f"零件 {part} 在 {t} 结存不符: "
                f"引擎=({bal.qty},{bal.amount_cents}) "
                f"对照={oracle.balance_at(part, t)}"
            )


def check_accounting_identity(engine, oracle):
    """入库金额合计 == 领用成本合计 - 退回金额合计 + 结存金额。"""
    for part in PARTS:
        in_total = issue_total = return_total = 0
        for ev in oracle.events.values():
            if ev["voided"] or ev["part"] != part:
                continue
            if ev["type"] == "in":
                in_total += ev["qty"] * ev["price"]
        for ev in oracle.events.values():
            if ev["voided"] or ev["part"] != part:
                continue
            if ev["type"] == "issue":
                issue_total += oracle_issue_cost(oracle, ev)
            elif ev["type"] == "return":
                return_total += oracle_return_cost(oracle, ev)
        bal = engine.balance_at(part, FAR_FUTURE)
        assert in_total == issue_total - return_total + bal.amount_cents


def oracle_issue_cost(oracle, issue):
    # 重算该领用的成本：在重算结果中按 id 定位
    qty = amount = 0
    issues = {}
    for ev in sorted(
        (e for e in oracle.events.values()
         if not e["voided"] and e["part"] == issue["part"]),
        key=lambda e: (e["bt"], e["id"]),
    ):
        if ev["type"] == "in":
            qty += ev["qty"]
            amount += ev["qty"] * ev["price"]
        elif ev["type"] == "issue":
            cost = round_half_up(amount * ev["qty"], qty)
            qty -= ev["qty"]
            amount -= cost
            issues[ev["id"]] = (ev["qty"], cost)
            if ev["id"] == issue["id"]:
                return cost
        else:
            iq, ic = issues[ev["issue_event_id"]]
            qty += ev["qty"]
            amount += round_half_up(ic * ev["qty"], iq)
    raise AssertionError("领用事件不在现存事件中")


def oracle_return_cost(oracle, ret):
    issue = oracle.events[ret["issue_event_id"]]
    cost = oracle_issue_cost(oracle, issue)
    return round_half_up(cost * ret["qty"], issue["qty"])


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_mixed_operations_match_oracle(seed):
    rng = random.Random(seed)
    oracle = Oracle()
    used_times = []
    with Engine(":memory:") as engine:
        for part in PARTS:
            engine.register_part(part)
        for wo in range(1, WO_COUNT + 1):
            finish = f"202{rng.randint(4, 5)}-{rng.randint(1, 12):02d}-15"
            revenue = rng.randint(1000, 200000)
            labor = rng.randint(0, revenue)
            engine.register_work_order(wo, finish, revenue, labor)
            oracle.work_orders[wo] = (finish, revenue, labor)

        live_issues = []
        live_events = []
        steps = 400
        for step in range(steps):
            op = rng.random()
            applied = False
            if op < 0.35 or not live_events:
                part = rng.choice(PARTS)
                kw = dict(part=part, type="in", bt=random_time(rng, used_times),
                          qty=rng.randint(1, 20), price=rng.randint(1, 3000),
                          voided=False)
                try:
                    event_id = engine.add_inbound(
                        part, kw["qty"], kw["price"], kw["bt"])
                    applied = True
                except PartsCostError:
                    pass
            elif op < 0.70:
                part = rng.choice(PARTS)
                wo = rng.randint(1, WO_COUNT)
                kw = dict(part=part, type="issue", bt=random_time(rng, used_times),
                          qty=rng.randint(1, 10), wo=wo, voided=False)
                try:
                    event_id = engine.add_issue(part, wo, kw["qty"], kw["bt"])
                    applied = True
                except PartsCostError:
                    pass
            elif op < 0.85 and live_issues:
                issue_id = rng.choice(live_issues)
                issue = oracle.events[issue_id]
                bt = max(issue["bt"], random_time(rng, used_times))
                kw = dict(part=issue["part"], type="return", bt=bt,
                          qty=rng.randint(1, issue["qty"]), wo=issue["wo"],
                          issue_event_id=issue_id, voided=False)
                try:
                    event_id = engine.add_return(issue_id, kw["qty"], bt)
                    applied = True
                except PartsCostError:
                    pass
            elif live_events:
                event_id = rng.choice(live_events)
                try:
                    engine.void_event(event_id)
                    oracle.void(event_id)
                    live_events.remove(event_id)
                    if event_id in live_issues:
                        live_issues.remove(event_id)
                    for oid, oev in list(oracle.events.items()):
                        if oev.get("issue_event_id") == event_id:
                            if oid in live_events:
                                live_events.remove(oid)
                except PartsCostError:
                    pass
                oracle.recompute()
                check_work_orders(engine, oracle)
                continue

            if applied:
                kw["id"] = event_id
                oracle.add_event(**kw)
                live_events.append(event_id)
                if kw["type"] == "issue":
                    live_issues.append(event_id)

            oracle.recompute()
            check_work_orders(engine, oracle)
            sample_times = [random_time(rng, used_times) for _ in range(3)]
            sample_times.append(FAR_FUTURE)
            check_balances(engine, oracle, sample_times)
            if step % 40 == 0 or step == steps - 1:
                all_times = sorted({e["bt"] for e in oracle.events.values()})
                all_times.append(FAR_FUTURE)
                check_balances(engine, oracle, all_times)
                check_accounting_identity(engine, oracle)
