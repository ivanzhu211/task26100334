"""规模与性能验收：30 万事件、热零件 1 万事件下的补录/作废/查询耗时。"""

import random
import time
from datetime import datetime, timedelta

import pytest

from parts_cost import Engine

PART_COUNT = 3000
WO_COUNT = 10000
EVENTS_PER_PART = 100
HOT_PART = "P-HOT"
HOT_EVENTS = 10000


def gen_part_events(rng, part_no, count, wo_ids):
    """生成一个零件五年内的合法事件流（直接落库，绕过 API 做批量建数）。"""
    rows = []
    running_qty = 0
    issues = []  # (event_seq_placeholder, qty, returned)
    # 业务时间单调递增，保证按业务时间重放时库存不为负
    avg_step = int(5 * 365.25 * 86400 / count)
    clock = datetime(2021, 1, 1)
    for i in range(count):
        clock += timedelta(seconds=rng.randint(60, 2 * avg_step))
        bt = clock.isoformat()
        roll = rng.random()
        if roll < 0.4 or running_qty == 0:
            qty = rng.randint(5, 30)
            price = rng.randint(10, 5000)
            rows.append((part_no, "in", bt, qty, price, None, None))
            running_qty += qty
        elif roll < 0.9:
            qty = rng.randint(1, running_qty)
            wo = rng.choice(wo_ids)
            rows.append((part_no, "issue", bt, qty, None, wo, None))
            issues.append([len(rows), qty, 0])  # 记录行号占位
            running_qty -= qty
        elif issues:
            chosen = rng.choice(issues)
            idx, qty, returned = chosen
            if returned < qty:
                ret_qty = rng.randint(1, qty - returned)
                issue_bt = rows[idx - 1][2]
                rows.append((part_no, "return", max(bt, issue_bt), ret_qty,
                             None, rows[idx - 1][5], -(idx)))  # 占位，后补
                chosen[2] += ret_qty
                running_qty += ret_qty
            else:
                qty_in = rng.randint(5, 30)
                rows.append((part_no, "in", bt, qty_in,
                             rng.randint(10, 5000), None, None))
                running_qty += qty_in
        else:
            qty_in = rng.randint(5, 30)
            rows.append((part_no, "in", bt, qty_in,
                         rng.randint(10, 5000), None, None))
            running_qty += qty_in
    return rows


@pytest.fixture(scope="module")
def big_engine(tmp_path_factory):
    rng = random.Random(42)
    db = tmp_path_factory.mktemp("perf") / "perf.db"
    engine = Engine(str(db))
    engine._conn.execute("PRAGMA foreign_keys = OFF")  # 批量建数后统一回填
    wo_ids = list(range(1, WO_COUNT + 1))
    with engine._conn:
        engine._conn.executemany(
            "INSERT INTO parts (part_no) VALUES (?)",
            [(f"P{i:04d}",) for i in range(PART_COUNT)] + [(HOT_PART,)],
        )
        engine._conn.executemany(
            "INSERT INTO work_orders (id, finish_date, revenue_cents,"
            " labor_cents) VALUES (?, ?, ?, ?)",
            [(w, f"202{rng.randint(1, 5)}-{rng.randint(1, 12):02d}-15",
              rng.randint(1000, 500000), rng.randint(0, 100000))
             for w in wo_ids],
        )
    # 逐零件生成事件并直接批量插入，退回的 issue_event_id 之后统一回填
    all_parts = [f"P{i:04d}" for i in range(PART_COUNT)] + [HOT_PART]
    for part in all_parts:
        count = HOT_EVENTS if part == HOT_PART else EVENTS_PER_PART
        rows = gen_part_events(rng, part, count, wo_ids)
        with engine._conn:
            cur = engine._conn.executemany(
                "INSERT INTO events (part_no, type, business_time, qty,"
                " unit_price_cents, work_order_id, issue_event_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            # 回填退回事件的 issue_event_id（占位为负的行内序号）
            first_id = engine._conn.execute(
                "SELECT min(id) FROM events WHERE part_no = ?", (part,)
            ).fetchone()[0]
            engine._conn.execute(
                "UPDATE events SET issue_event_id = ? - issue_event_id - 1"
                " WHERE part_no = ? AND type = 'return'",
                (first_id, part),
            )
            engine._recompute_part(part)
    engine._conn.execute("PRAGMA foreign_keys = ON")
    yield engine
    engine.close()


def test_dataset_scale(big_engine):
    total = big_engine._conn.execute("SELECT count(*) FROM events").fetchone()[0]
    assert total >= 300000
    hot = big_engine._conn.execute(
        "SELECT count(*) FROM events WHERE part_no = ?", (HOT_PART,)
    ).fetchone()[0]
    assert hot >= HOT_EVENTS


def test_backdated_inbound_under_one_second(big_engine):
    start = time.perf_counter()
    event_id = big_engine.add_inbound(HOT_PART, 100, 9999, "2024-06-15T08:00:00")
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"补录耗时 {elapsed:.3f}s"
    start = time.perf_counter()
    big_engine.void_event(event_id)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"作废耗时 {elapsed:.3f}s"


def test_void_year_old_event_under_one_second(big_engine):
    event_id = big_engine.add_inbound(HOT_PART, 50, 100, "2024-01-10T08:00:00")
    start = time.perf_counter()
    big_engine.void_event(event_id)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"作废一年前事件耗时 {elapsed:.3f}s"


def test_point_queries_under_50ms(big_engine):
    start = time.perf_counter()
    for _ in range(20):
        big_engine.balance_at(HOT_PART, "2024-06-01T00:00:00")
    assert (time.perf_counter() - start) / 20 < 0.05

    start = time.perf_counter()
    for _ in range(20):
        big_engine.work_order_cost(5000)
    assert (time.perf_counter() - start) / 20 < 0.05

    start = time.perf_counter()
    for _ in range(20):
        big_engine.low_margin_work_orders("2023-01-01", "2023-12-31")
    assert (time.perf_counter() - start) / 20 < 0.05
