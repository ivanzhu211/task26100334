"""业务规则单元测试。"""

import pytest

from parts_cost import (
    Engine,
    InvalidQuantityError,
    NegativeStockError,
    ReturnBeforeIssueError,
    ReturnExceedsIssueError,
    UnknownEventError,
)


@pytest.fixture()
def engine():
    with Engine(":memory:") as eng:
        yield eng


def test_moving_weighted_average_rounding(engine):
    engine.register_part("P")
    engine.register_work_order(1, "2026-01-20", 100000, 0)
    # 3 件共 100 分，领 1 件成本 = round(100/3) = 33
    engine.add_inbound("P", 3, 0, "2026-01-01")  # 先入库 0 元
    engine.void_event(1)  # 作废，换用单价录入
    engine.add_inbound("P", 1, 34, "2026-01-01T00:00:01")
    engine.add_inbound("P", 1, 33, "2026-01-01T00:00:02")
    engine.add_inbound("P", 1, 33, "2026-01-01T00:00:03")
    issue = engine.add_issue("P", 1, 1, "2026-01-02")
    assert engine.balance_at("P", "2026-01-02").qty == 2
    assert engine.balance_at("P", "2026-01-02").amount_cents == 67
    # 再领 1 件：round(67/2) = 34（half-up）
    engine.add_issue("P", 1, 1, "2026-01-03")
    bal = engine.balance_at("P", "2026-01-03")
    assert (bal.qty, bal.amount_cents) == (1, 33)
    # 领完最后 1 件，结存金额必须正好为 0
    engine.add_issue("P", 1, 1, "2026-01-04")
    bal = engine.balance_at("P", "2026-01-04")
    assert (bal.qty, bal.amount_cents) == (0, 0)
    assert engine.work_order_cost(1).parts_net_cents == 100
    assert issue


def test_backdated_inbound_reprices_later_issues(engine):
    engine.register_part("P")
    engine.register_work_order(1, "2026-02-01", 10000, 0)
    engine.add_inbound("P", 10, 100, "2026-01-01")
    engine.add_issue("P", 1, 5, "2026-01-10")  # 成本 500
    assert engine.work_order_cost(1).parts_net_cents == 500
    # 补录一笔更早的高价入库：结存 20 件 3000 分，领 5 件成本变 750
    late = engine.add_inbound("P", 10, 200, "2026-01-05")
    assert engine.work_order_cost(1).parts_net_cents == 750
    assert engine.balance_at("P", "2026-01-10").amount_cents == 2250
    # 作废补录，成本回到 500
    engine.void_event(late)
    assert engine.work_order_cost(1).parts_net_cents == 500
    assert engine.balance_at("P", "2026-01-10").amount_cents == 500


def test_return_follows_original_issue_cost(engine):
    engine.register_part("P")
    engine.register_work_order(1, "2026-02-01", 10000, 0)
    engine.add_inbound("P", 10, 100, "2026-01-01")
    issue = engine.add_issue("P", 1, 4, "2026-01-10")  # 成本 400
    engine.add_return(issue, 2, "2026-01-11")  # 退回 400*2/4 = 200
    assert engine.work_order_cost(1).parts_net_cents == 200
    assert engine.balance_at("P", "2026-01-11").qty == 8
    assert engine.balance_at("P", "2026-01-11").amount_cents == 800
    # 补录高价入库使原领用成本变为 600，退回金额跟着变 300
    engine.add_inbound("P", 10, 200, "2026-01-05")
    assert engine.work_order_cost(1).parts_net_cents == 300
    # 1000 + 2000 - 600 + 300 = 2700
    assert engine.balance_at("P", "2026-01-11").amount_cents == 2700


def test_void_issue_cascades_returns(engine):
    engine.register_part("P")
    engine.register_work_order(1, "2026-02-01", 10000, 0)
    engine.add_inbound("P", 10, 100, "2026-01-01")
    issue = engine.add_issue("P", 1, 4, "2026-01-10")
    ret = engine.add_return(issue, 2, "2026-01-11")
    engine.void_event(issue)
    assert engine.work_order_cost(1).parts_net_cents == 0
    assert engine.balance_at("P", "2026-01-11").qty == 10
    with pytest.raises(UnknownEventError):
        engine.void_event(ret)


def test_negative_stock_rejected_and_state_unchanged(engine):
    engine.register_part("P")
    engine.register_work_order(1, "2026-02-01", 10000, 0)
    inbound = engine.add_inbound("P", 5, 100, "2026-01-01")
    engine.add_issue("P", 1, 5, "2026-01-10")
    before = engine.balance_at("P", "2026-12-31")
    # 作废入库会让 1 月 10 日结存变负：拒绝
    with pytest.raises(NegativeStockError):
        engine.void_event(inbound)
    assert engine.balance_at("P", "2026-12-31") == before
    assert engine.work_order_cost(1).parts_net_cents == 500
    # 补录一笔业务时间更早的领用也会变负：拒绝
    with pytest.raises(NegativeStockError):
        engine.add_issue("P", 1, 1, "2026-01-05")
    assert engine.balance_at("P", "2026-12-31") == before
    # 合法补录不受影响
    engine.add_inbound("P", 1, 100, "2026-01-05")
    assert engine.balance_at("P", "2026-12-31").qty == 1


def test_return_constraints(engine):
    engine.register_part("P")
    engine.register_work_order(1, "2026-02-01", 10000, 0)
    engine.add_inbound("P", 10, 100, "2026-01-01")
    issue = engine.add_issue("P", 1, 4, "2026-01-10")
    with pytest.raises(ReturnBeforeIssueError):
        engine.add_return(issue, 1, "2026-01-09")
    engine.add_return(issue, 3, "2026-01-11")
    with pytest.raises(ReturnExceedsIssueError):
        engine.add_return(issue, 2, "2026-01-12")
    # 拒绝后状态不变，仍可退回剩余 1 件
    engine.add_return(issue, 1, "2026-01-12")
    assert engine.work_order_cost(1).parts_net_cents == 0


def test_low_margin_threshold(engine):
    # 收入 10000，人工 1000，配件 7000 -> 毛利 2000，正好 20%，不算低毛利
    engine.register_work_order(1, "2026-03-01", 10000, 1000)
    # 收入 10000，人工 1000，配件 7001 -> 毛利 1999 < 20%，低毛利
    engine.register_work_order(2, "2026-03-02", 10000, 1000)
    engine.register_part("P1")
    engine.register_part("P2")
    engine.add_inbound("P1", 70, 100, "2026-01-01")
    engine.add_issue("P1", 1, 70, "2026-02-01")
    engine.add_inbound("P2", 1, 7001, "2026-01-01")
    engine.add_issue("P2", 2, 1, "2026-02-01")
    low = engine.low_margin_work_orders("2026-03-01", "2026-03-31")
    assert [w.work_order_id for w in low] == [2]
    assert engine.work_order_cost(1).is_low_margin is False
    assert engine.work_order_cost(2).is_low_margin is True


def test_invalid_inputs(engine):
    engine.register_part("P")
    engine.register_work_order(1, "2026-02-01", 10000, 0)
    with pytest.raises(InvalidQuantityError):
        engine.add_inbound("P", 0, 100, "2026-01-01")
    with pytest.raises(InvalidQuantityError):
        engine.add_issue("P", 1, -2, "2026-01-01")
    with pytest.raises(UnknownEventError):
        engine.void_event(999)


def test_balance_before_any_event_is_zero(engine):
    engine.register_part("P")
    engine.add_inbound("P", 5, 100, "2026-01-01")
    bal = engine.balance_at("P", "2025-12-31")
    assert (bal.qty, bal.amount_cents) == (0, 0)
    bal = engine.balance_at("NOSUCH", "2026-01-01")
    assert (bal.qty, bal.amount_cents) == (0, 0)
