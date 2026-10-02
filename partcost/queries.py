"""查询：时点结存、工单成本与毛利、低毛利工单清单。"""

import sqlite3
from fractions import Fraction
from typing import List, Optional

from .core import PartCost, TimeLike, normalize_time
from .errors import NotFoundError

LOW_MARGIN_THRESHOLD = 0.20  # 低于 20% 算低毛利，正好 20% 不算


def balance_at(pc: "PartCost", part_id: str,
               at: Optional[TimeLike] = None) -> dict:
    """某零件在任一业务时点的结存。返回 {'qty': int, 'amount': int}（分）。"""
    if at is None:
        row = pc._conn.execute("""
            SELECT qty_after, amount_after FROM events
            WHERE part_id = ? AND voided = 0
            ORDER BY business_time DESC, id DESC LIMIT 1
        """, (part_id,)).fetchone()
    else:
        bt = normalize_time(at)
        row = pc._conn.execute("""
            SELECT qty_after, amount_after FROM events
            WHERE part_id = ? AND voided = 0 AND business_time <= ?
            ORDER BY business_time DESC, id DESC LIMIT 1
        """, (part_id, bt)).fetchone()
    if row is None:
        return {"qty": 0, "amount": 0}
    return {"qty": row["qty_after"], "amount": row["amount_after"]}


def _margin(revenue: int, labor: int, net_cost: int) -> Optional[float]:
    if revenue <= 0:
        return None
    return (revenue - labor - net_cost) / revenue


def work_order_profit(pc: "PartCost", work_order_id: str) -> dict:
    """单个工单的配件净成本与毛利率。"""
    wo = pc._conn.execute(
        "SELECT finish_date, revenue, labor FROM work_orders WHERE id = ?",
        (work_order_id,)).fetchone()
    if wo is None:
        raise NotFoundError(f"工单不存在: {work_order_id!r}")
    net = pc._conn.execute("""
        SELECT COALESCE(SUM(cost), 0) AS s FROM events
        WHERE work_order_id = ? AND voided = 0 AND type IN ('issue', 'return')
    """, (work_order_id,)).fetchone()["s"]
    return {
        "work_order_id": work_order_id,
        "finish_date": wo["finish_date"],
        "revenue": wo["revenue"],
        "labor": wo["labor"],
        "net_cost": net,
        "margin": _margin(wo["revenue"], wo["labor"], net),
    }


def low_margin_work_orders(pc: "PartCost", start_date: TimeLike,
                           end_date: TimeLike,
                           threshold: float = LOW_MARGIN_THRESHOLD) -> List[dict]:
    """完工日期在 [start_date, end_date] 内、毛利率低于 threshold 的工单清单。"""
    start = normalize_time(start_date)[:10]
    end = normalize_time(end_date)[:10]
    # 用整数交叉相乘比较，保证“正好 20% 不算低毛利”精确成立
    frac = Fraction(str(threshold))
    numer, denom = frac.numerator, frac.denominator
    rows = pc._conn.execute("""
        SELECT w.id, w.finish_date, w.revenue, w.labor,
               COALESCE(SUM(e.cost), 0) AS net_cost
        FROM work_orders w
        LEFT JOIN events e
               ON e.work_order_id = w.id AND e.voided = 0
              AND e.type IN ('issue', 'return')
        WHERE w.finish_date BETWEEN ? AND ?
          AND w.revenue > 0
        GROUP BY w.id
        HAVING (w.revenue - w.labor - COALESCE(SUM(e.cost), 0)) * ?
               < ? * w.revenue
        ORDER BY w.finish_date, w.id
    """, (start, end, denom, numer)).fetchall()
    return [
        {
            "work_order_id": r["id"],
            "finish_date": r["finish_date"],
            "revenue": r["revenue"],
            "labor": r["labor"],
            "net_cost": r["net_cost"],
            "margin": _margin(r["revenue"], r["labor"], r["net_cost"]),
        }
        for r in rows
    ]
