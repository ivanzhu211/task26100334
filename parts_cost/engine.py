"""核算引擎：事件录入、作废、重算与查询。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import connect
from .errors import (
    InvalidAmountError,
    InvalidQuantityError,
    NegativeStockError,
    ReturnBeforeIssueError,
    ReturnExceedsIssueError,
    UnknownEventError,
    UnknownPartError,
    UnknownWorkOrderError,
)

LOW_MARGIN_NUM = 1  # 低毛利阈值：毛利率 < 1/5 即 20%
LOW_MARGIN_DEN = 5


def _round_half_up(numerator: int, denominator: int) -> int:
    """非负整数除法，四舍五入（half-up）到整数。"""
    return (2 * numerator + denominator) // (2 * denominator)


def _check_qty(qty: int) -> None:
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        raise InvalidQuantityError(f"数量必须是正整数，收到 {qty!r}")


@dataclass(frozen=True)
class Balance:
    """某零件在某业务时点的结存。"""

    qty: int
    amount_cents: int


@dataclass(frozen=True)
class WorkOrderCost:
    """单个工单的成本与毛利。"""

    work_order_id: int
    parts_net_cents: int
    gross_profit_cents: int
    margin: float
    is_low_margin: bool


class Engine:
    """配件成本核算引擎。所有金额单位为分，业务时间为 ISO 格式字符串。"""

    def __init__(self, db_path: str):
        self._conn: sqlite3.Connection = connect(db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 主数据 ----

    def register_part(self, part_no: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO parts (part_no) VALUES (?)", (part_no,)
            )

    def register_work_order(
        self, work_order_id: int, finish_date: str,
        revenue_cents: int, labor_cents: int,
    ) -> None:
        if revenue_cents <= 0:
            raise InvalidAmountError("不含税收入必须为正数（分）")
        if labor_cents < 0:
            raise InvalidAmountError("人工成本不能为负（分）")
        with self._conn:
            self._conn.execute(
                "INSERT INTO work_orders (id, finish_date, revenue_cents,"
                " labor_cents) VALUES (?, ?, ?, ?)",
                (work_order_id, finish_date, revenue_cents, labor_cents),
            )

    # ---- 库存事件 ----

    def add_inbound(
        self, part_no: str, qty: int, unit_price_cents: int, business_time: str,
    ) -> int:
        """录入入库，返回事件 id。"""
        _check_qty(qty)
        if unit_price_cents < 0:
            raise InvalidAmountError("入库单价不能为负（分）")
        self._require_part(part_no)
        with self._conn:
            event_id = self._insert_event(
                part_no, "in", business_time, qty,
                unit_price_cents=unit_price_cents,
            )
            self._recompute_part(part_no)
        return event_id

    def add_issue(
        self, part_no: str, work_order_id: int, qty: int, business_time: str,
    ) -> int:
        """录入领用，返回事件 id。"""
        _check_qty(qty)
        self._require_part(part_no)
        self._require_work_order(work_order_id)
        with self._conn:
            event_id = self._insert_event(
                part_no, "issue", business_time, qty, work_order_id=work_order_id,
            )
            self._recompute_part(part_no)
        return event_id

    def add_return(
        self, issue_event_id: int, qty: int, business_time: str,
    ) -> int:
        """录入退回（挂在某次领用下），返回事件 id。"""
        _check_qty(qty)
        issue = self._get_event(issue_event_id)
        if issue["type"] != "issue" or issue["voided"]:
            raise UnknownEventError(f"领用事件 {issue_event_id} 不存在或已作废")
        if business_time < issue["business_time"]:
            raise ReturnBeforeIssueError("退回的业务时间不能早于原领用")
        with self._conn:
            event_id = self._insert_event(
                issue["part_no"], "return", business_time, qty,
                work_order_id=issue["work_order_id"],
                issue_event_id=issue_event_id,
            )
            self._recompute_part(issue["part_no"])
        return event_id

    def void_event(self, event_id: int) -> None:
        """作废事件；作废领用时其下退回一并作废。"""
        event = self._get_event(event_id)
        if event["voided"]:
            raise UnknownEventError(f"事件 {event_id} 已作废")
        with self._conn:
            self._conn.execute(
                "UPDATE events SET voided = 1 WHERE id = ?", (event_id,)
            )
            if event["type"] == "issue":
                self._conn.execute(
                    "UPDATE events SET voided = 1"
                    " WHERE issue_event_id = ? AND voided = 0",
                    (event_id,),
                )
            self._recompute_part(event["part_no"])

    # ---- 查询 ----

    def balance_at(self, part_no: str, business_time: str) -> Balance:
        row = self._conn.execute(
            "SELECT qty_after, amount_after FROM events"
            " WHERE part_no = ? AND voided = 0 AND business_time <= ?"
            " ORDER BY business_time DESC, id DESC LIMIT 1",
            (part_no, business_time),
        ).fetchone()
        if row is None:
            return Balance(qty=0, amount_cents=0)
        return Balance(qty=row["qty_after"], amount_cents=row["amount_after"])

    def work_order_cost(self, work_order_id: int) -> WorkOrderCost:
        row = self._conn.execute(
            "SELECT * FROM work_orders WHERE id = ?", (work_order_id,)
        ).fetchone()
        if row is None:
            raise UnknownWorkOrderError(f"工单 {work_order_id} 不存在")
        return self._to_work_order_cost(row)

    def low_margin_work_orders(
        self, finish_date_from: str, finish_date_to: str,
    ) -> list[WorkOrderCost]:
        rows = self._conn.execute(
            "SELECT * FROM work_orders"
            " WHERE finish_date BETWEEN ? AND ?"
            " AND ? * (revenue_cents - labor_cents - parts_net_cents)"
            "     < ? * revenue_cents"
            " ORDER BY finish_date, id",
            (finish_date_from, finish_date_to, LOW_MARGIN_DEN, LOW_MARGIN_NUM),
        ).fetchall()
        return [self._to_work_order_cost(row) for row in rows]

    # ---- 内部实现 ----

    def _require_part(self, part_no: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM parts WHERE part_no = ?", (part_no,)
        ).fetchone()
        if row is None:
            raise UnknownPartError(f"零件 {part_no!r} 未登记")

    def _require_work_order(self, work_order_id: int) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM work_orders WHERE id = ?", (work_order_id,)
        ).fetchone()
        if row is None:
            raise UnknownWorkOrderError(f"工单 {work_order_id} 不存在")

    def _get_event(self, event_id: int) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise UnknownEventError(f"事件 {event_id} 不存在")
        return row

    def _insert_event(
        self, part_no: str, type_: str, business_time: str, qty: int,
        unit_price_cents: int | None = None,
        work_order_id: int | None = None,
        issue_event_id: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO events (part_no, type, business_time, qty,"
            " unit_price_cents, work_order_id, issue_event_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (part_no, type_, business_time, qty,
             unit_price_cents, work_order_id, issue_event_id),
        )
        return int(cur.lastrowid)

    def _recompute_part(self, part_no: str) -> None:
        """按业务时间从头重算某零件的全部现存事件并回写缓存结果。

        任一事件生效后结存数量为负则抛 NegativeStockError，
        由外层事务回滚保证状态不变。
        """
        rows = self._conn.execute(
            "SELECT * FROM events WHERE part_no = ? AND voided = 0"
            " ORDER BY business_time, id",
            (part_no,),
        ).fetchall()

        running_qty = 0
        running_amount = 0
        issues: dict[int, tuple[int, int]] = {}  # 事件 id -> (领用数量, 领用成本)
        returned: dict[int, int] = {}            # 领用事件 id -> 累计退回数量
        updates: list[tuple[int, int, int, int]] = []

        for row in rows:
            if row["type"] == "in":
                cost = row["qty"] * row["unit_price_cents"]
                running_qty += row["qty"]
                running_amount += cost
            elif row["type"] == "issue":
                if row["qty"] > running_qty:
                    raise NegativeStockError(
                        f"零件 {part_no!r} 在 {row['business_time']} 结存"
                        f" {running_qty}，不足以领用 {row['qty']}"
                    )
                cost = _round_half_up(running_amount * row["qty"], running_qty)
                running_qty -= row["qty"]
                running_amount -= cost
                issues[row["id"]] = (row["qty"], cost)
            else:  # return
                issue_id = row["issue_event_id"]
                issue_qty, issue_cost = issues[issue_id]
                total_returned = returned.get(issue_id, 0) + row["qty"]
                if total_returned > issue_qty:
                    raise ReturnExceedsIssueError(
                        f"领用事件 {issue_id} 累计退回 {total_returned}"
                        f" 超过领用数量 {issue_qty}"
                    )
                returned[issue_id] = total_returned
                cost = _round_half_up(issue_cost * row["qty"], issue_qty)
                running_qty += row["qty"]
                running_amount += cost

            if (
                row["cost_cents"] != cost
                or row["qty_after"] != running_qty
                or row["amount_after"] != running_amount
            ):
                updates.append((cost, running_qty, running_amount, row["id"]))

        self._conn.executemany(
            "UPDATE events SET cost_cents = ?, qty_after = ?,"
            " amount_after = ? WHERE id = ?",
            updates,
        )
        self._refresh_work_order_costs(part_no)

    def _refresh_work_order_costs(self, part_no: str) -> None:
        """重算该零件涉及的所有工单的配件净成本。"""
        self._conn.execute(
            "UPDATE work_orders SET parts_net_cents = COALESCE(("
            "   SELECT SUM(CASE WHEN e.type = 'issue' THEN e.cost_cents"
            "                   ELSE -e.cost_cents END)"
            "   FROM events e"
            "   WHERE e.work_order_id = work_orders.id AND e.voided = 0"
            " ), 0)"
            " WHERE id IN ("
            "   SELECT DISTINCT work_order_id FROM events"
            "   WHERE part_no = ? AND work_order_id IS NOT NULL"
            " )",
            (part_no,),
        )

    @staticmethod
    def _to_work_order_cost(row: sqlite3.Row) -> WorkOrderCost:
        gross = row["revenue_cents"] - row["labor_cents"] - row["parts_net_cents"]
        margin = gross / row["revenue_cents"]
        return WorkOrderCost(
            work_order_id=row["id"],
            parts_net_cents=row["parts_net_cents"],
            gross_profit_cents=gross,
            margin=margin,
            is_low_margin=(
                gross * LOW_MARGIN_DEN < row["revenue_cents"] * LOW_MARGIN_NUM
            ),
        )
