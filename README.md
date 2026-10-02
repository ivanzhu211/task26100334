# 配件成本核算模块（parts_cost）

面向 3–5 工位独立修车厂的配件成本核算库。纯 Python + 单文件 SQLite，
无接口层，由修理厂管理系统上层直接 import 调用。

## 核算规则

- 一律按**业务时间**核算；业务时间相同的按录入先后排。
- 移动加权平均：入库时结存金额 += 数量×单价；领用成本 =
  当时结存金额 × 领用数量 ÷ 结存数量，四舍五入（half-up）到分。
- 退回按原领用成本回库：退回金额 = 原领用成本 × 退回数量 ÷ 原领用数量，
  四舍五入到分；原领用成本变化时退回金额与后续计价自动跟着变。
- 补录（业务时间在过去）或作废任何事件后，该零件全部历史事件立即重算，
  受影响的工单成本、毛利、低毛利清单同步更新。
- 任何业务时点结存数量不能为负：会导致负库存的录入/作废整笔拒绝，
  状态保持不变（抛 `NegativeStockError`）。
- 结存数量为 0 时结存金额必为 0；截至任意时点满足
  入库金额合计 = 领用成本合计 − 退回金额合计 + 结存金额。
- 工单毛利率 =（不含税收入 − 人工成本 − 配件净成本）÷ 收入；
  严格低于 20% 为低毛利工单（正好 20% 不算）。

## 用法

```python
from parts_cost import Engine

with Engine("shop.db") as eng:
    eng.register_part("FILTER-001")
    eng.register_work_order(
        work_order_id=1001, finish_date="2026-01-20",
        revenue_cents=100000, labor_cents=20000)

    # 入库：10 件，单价 10 元
    eng.add_inbound("FILTER-001", qty=10, unit_price_cents=1000,
                    business_time="2026-01-05T09:00:00")
    # 领用到工单
    issue_id = eng.add_issue("FILTER-001", work_order_id=1001, qty=4,
                             business_time="2026-01-08T14:00:00")
    # 退回未用的 1 件
    eng.add_return(issue_id, qty=1, business_time="2026-01-09T10:00:00")
    # 供应商账单补录（业务时间在过去），历史成本自动重算
    late_id = eng.add_inbound("FILTER-001", qty=10, unit_price_cents=1200,
                              business_time="2026-01-03T00:00:00")
    # 作废（作废领用时其下退回一并作废）
    eng.void_event(late_id)

    bal = eng.balance_at("FILTER-001", "2026-01-31T23:59:59")
    cost = eng.work_order_cost(1001)          # 净成本/毛利/是否低毛利
    low = eng.low_margin_work_orders("2026-01-01", "2026-01-31")
```

数量为正整数；金额一律为分（int）；业务时间为 ISO 格式字符串
（`YYYY-MM-DD` 或 `YYYY-MM-DDTHH:MM:SS`，按字符串排序即时间序）。

## 异常

`NegativeStockError`（负库存拒绝）、`ReturnExceedsIssueError`、
`ReturnBeforeIssueError`、`UnknownPartError`、`UnknownWorkOrderError`、
`UnknownEventError`、`InvalidQuantityError`、`InvalidAmountError`，
均继承 `PartsCostError`。抛错时整笔操作回滚，状态不变。

## 实现要点

- 每个事件落库时缓存其生效后的结存数量/金额与自身成本
  （`events.cost_cents / qty_after / amount_after`）。
- 补录/作废只重算该零件的事件流（常用件上万条也在 1 秒内），
  仅回写发生变化的行，并同步刷新相关工单的 `parts_net_cents`。
- 三类查询都是索引上的点查/范围查，不随历史变长而变慢：
  - `balance_at`：`events(part_no, business_time, id)` 索引取最后一行；
  - `work_order_cost`：主键点查；
  - `low_margin_work_orders`：`work_orders(finish_date)` 范围扫描。

## 测试

```bash
python3 -m pytest tests/ -q
```

- `tests/test_rules.py`：计价、退回、作废、负库存拒绝、阈值等规则；
- `tests/test_acceptance.py`：补录高价入库使工单变低毛利、作废后恢复，
  以及拒绝操作前后查询结果不变；
- `tests/test_random_differential.py`：随机混合乱序录入/补录/退回/作废，
  每步与“现存事件按业务时间从头重算”的对照模型全量比对；
- `tests/test_performance.py`：30 万事件、3000 零件、热零件 1 万事件下的
  补录/作废 < 1 秒、查询 < 50 毫秒。
