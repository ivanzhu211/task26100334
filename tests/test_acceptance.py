"""验收场景：补录高价入库改变历史工单毛利，作废后恢复。"""

import pytest

from parts_cost import Engine, NegativeStockError


@pytest.fixture()
def engine():
    with Engine(":memory:") as eng:
        yield eng


def test_backdated_inbound_flips_margin_and_void_restores(engine):
    """补录一笔业务时间早于领用的高价入库 -> 合格工单变低毛利；作废后恢复。"""
    engine.register_part("FILTER")
    # 工单：收入 100000 分，人工 20000 分；配件成本 < 60000 才算合格
    engine.register_work_order(1, "2026-02-01", 100000, 20000)
    engine.add_inbound("FILTER", 10, 1000, "2026-01-01")  # 10 件 @10元
    engine.add_issue("FILTER", 1, 10, "2026-01-10")       # 成本 10000
    cost = engine.work_order_cost(1)
    assert cost.parts_net_cents == 10000
    assert cost.is_low_margin is False
    assert engine.low_margin_work_orders("2026-02-01", "2026-02-28") == []

    # 一周后供应商账单补录：1 月 5 日到货 10 件 @90元
    late = engine.add_inbound("FILTER", 10, 9000, "2026-01-05")
    # 领用成本重算为 (10000+90000)/20*10 = 50000 -> 毛利 30%，仍合格
    assert engine.work_order_cost(1).parts_net_cents == 50000
    assert engine.work_order_cost(1).is_low_margin is False

    # 再补录一笔更早的：1 月 3 日到货 10 件 @150元
    late2 = engine.add_inbound("FILTER", 10, 15000, "2026-01-03")
    # 领用成本 = 250000/30*10 四舍五入 = 83333 -> 毛利 (100000-20000-83333)/100000 < 0
    cost = engine.work_order_cost(1)
    assert cost.parts_net_cents == 83333
    assert cost.is_low_margin is True
    assert [w.work_order_id for w in
            engine.low_margin_work_orders("2026-02-01", "2026-02-28")] == [1]

    # 作废这两笔补录后恢复合格
    engine.void_event(late2)
    assert engine.work_order_cost(1).is_low_margin is False
    engine.void_event(late)
    cost = engine.work_order_cost(1)
    assert cost.parts_net_cents == 10000
    assert cost.is_low_margin is False
    assert engine.low_margin_work_orders("2026-02-01", "2026-02-28") == []


def test_rejected_operations_leave_state_untouched(engine):
    """会导致历史/之后时点结存为负的补录与作废被拒绝，且查询结果不变。"""
    engine.register_part("P")
    engine.register_work_order(1, "2026-02-01", 100000, 0)
    inbound = engine.add_inbound("P", 10, 500, "2026-01-01")
    engine.add_issue("P", 1, 8, "2026-01-10")
    engine.add_issue("P", 1, 2, "2026-01-20")

    snapshot = (
        engine.balance_at("P", "2026-01-05"),
        engine.balance_at("P", "2026-01-15"),
        engine.balance_at("P", "2026-12-31"),
        engine.work_order_cost(1),
    )

    # 作废入库会让 1 月 10 日之后结存为负
    with pytest.raises(NegativeStockError):
        engine.void_event(inbound)
    # 补录 1 月 5 日的领用 3 件会让之后时点结存为负（10-3-8<0）
    with pytest.raises(NegativeStockError):
        engine.add_issue("P", 1, 3, "2026-01-05")

    after = (
        engine.balance_at("P", "2026-01-05"),
        engine.balance_at("P", "2026-01-15"),
        engine.balance_at("P", "2026-12-31"),
        engine.work_order_cost(1),
    )
    assert after == snapshot
