"""库存事件的录入、作废与成本重算。"""

import sqlite3
from typing import Optional, Union
from datetime import date, datetime

from .db import connect
from .errors import NotFoundError, ValidationError

TimeLike = Union[str, date, datetime]


def normalize_time(t: TimeLike) -> str:
    """把业务时间规范成 ISO-8601 字符串（字典序即时间序）。"""
    if isinstance(t, datetime):
        return t.isoformat(timespec="seconds")
    if isinstance(t, date):
        return datetime(t.year, t.month, t.day).isoformat(timespec="seconds")
    if isinstance(t, str):
        try:
            return normalize_time(datetime.fromisoformat(t))
        except ValueError:
            try:
                d = date.fromisoformat(t)
            except ValueError:
                raise ValidationError(f"无法解析的业务时间: {t!r}") from None
            return normalize_time(d)
    raise ValidationError(f"不支持的业务时间类型: {type(t)!r}")


def _round_half_up(numer: int, denom: int) -> int:
    """非负整数按比例分摊，四舍五入到个位（分）。"""
    return (2 * numer + denom) // (2 * denom)


def _check_qty(qty: int) -> None:
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        raise ValidationError(f"数量必须是正整数: {qty!r}")


def _check_money(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{name}必须是非负整数（分）: {value!r}")


class PartCost:
    """配件成本核算库的主入口。由上层修理厂管理系统直接调用。"""

    def __init__(self, db_path: str):
        self._conn = connect(db_path)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PartCost":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def add_inbound(self, part_id: str, qty: int, unit_price: int,
                    business_time: TimeLike) -> int:
        """入库。返回事件 id。"""
        _check_qty(qty)
        _check_money(unit_price, "单价")
        bt = normalize_time(business_time)
        return self._mutate(part_id, """
            INSERT INTO events (part_id, type, qty, unit_price, business_time)
            VALUES (?, 'in', ?, ?, ?)
        """, (part_id, qty, unit_price, bt))

    def add_issue(self, part_id: str, work_order_id: str, qty: int,
                  business_time: TimeLike) -> int:
        """领用到工单。返回事件 id。"""
        _check_qty(qty)
        bt = normalize_time(business_time)
        cur = self._conn.execute(
            "SELECT 1 FROM work_orders WHERE id = ?", (work_order_id,))
        if cur.fetchone() is None:
            raise NotFoundError(f"工单不存在: {work_order_id!r}")
        return self._mutate(part_id, """
            INSERT INTO events (part_id, type, qty, business_time, work_order_id)
            VALUES (?, 'issue', ?, ?, ?)
        """, (part_id, qty, bt, work_order_id))

    def add_return(self, part_id: str, issue_event_id: int, qty: int,
                   business_time: TimeLike) -> int:
        """把某次领用中未用的零件退回库存。返回事件 id。"""
        _check_qty(qty)
        bt = normalize_time(business_time)
        row = self._conn.execute(
            "SELECT part_id, type, qty, business_time, work_order_id, voided "
            "FROM events WHERE id = ?",
            (issue_event_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"领用事件不存在: {issue_event_id}")
        if row["type"] != "issue":
            raise ValidationError(f"事件 {issue_event_id} 不是领用")
        if row["voided"]:
            raise ValidationError(f"领用事件 {issue_event_id} 已作废")
        if row["part_id"] != part_id:
            raise ValidationError("退回零件与原领用零件不一致")
        if bt < row["business_time"]:
            raise ValidationError("退回的业务时间不能早于原领用")
        returned = self._conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS s FROM events "
            "WHERE ref_id = ? AND voided = 0", (issue_event_id,)).fetchone()["s"]
        if returned + qty > row["qty"]:
            raise ValidationError(
                f"累计退回 {returned + qty} 超过领用数量 {row['qty']}")
        return self._mutate(part_id, """
            INSERT INTO events
                (part_id, type, qty, business_time, work_order_id, ref_id)
            VALUES (?, 'return', ?, ?, ?, ?)
        """, (part_id, qty, bt, row["work_order_id"], issue_event_id))

    def void_event(self, event_id: int) -> None:
        """作废事件；作废领用时其下退回一并作废。"""
        row = self._conn.execute(
            "SELECT part_id, type, voided FROM events WHERE id = ?",
            (event_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"事件不存在: {event_id}")
        if row["voided"]:
            return
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE events SET voided = 1 WHERE id = ?", (event_id,))
            if row["type"] == "issue":
                conn.execute(
                    "UPDATE events SET voided = 1 WHERE ref_id = ? AND voided = 0",
                    (event_id,))
            self._recompute_part(conn, row["part_id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def upsert_work_order(self, work_order_id: str, finish_date: TimeLike,
                          revenue: int, labor: int) -> None:
        """登记/更新工单的完工日期、不含税收入、人工成本。"""
        _check_money(revenue, "收入")
        _check_money(labor, "人工成本")
        fd = normalize_time(finish_date)[:10]
        self._conn.execute("""
            INSERT INTO work_orders (id, finish_date, revenue, labor)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                finish_date = excluded.finish_date,
                revenue = excluded.revenue,
                labor = excluded.labor
        """, (work_order_id, fd, revenue, labor))
        self._conn.commit()

    def _mutate(self, part_id: str, insert_sql: str, params: tuple) -> int:
        """插入一条事件并重算该零件；任何校验失败都整体回滚。"""
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(insert_sql, params)
            event_id = cur.lastrowid
            self._recompute_part(conn, part_id)
            conn.commit()
            return event_id
        except Exception:
            conn.rollback()
            raise

    def _recompute_part(self, conn: sqlite3.Connection, part_id: str) -> None:
        """按业务时间重算某零件全部现存事件的成本与结存，校验负库存。"""
        rows = conn.execute("""
            SELECT id, type, qty, unit_price, ref_id
            FROM events
            WHERE part_id = ? AND voided = 0
            ORDER BY business_time, id
        """, (part_id,)).fetchall()

        qty_on_hand = 0
        amount_on_hand = 0
        issue_cost: dict = {}   # 领用事件 id -> (成本, 数量)
        updates = []
        for r in rows:
            cost = None
            if r["type"] == "in":
                qty_on_hand += r["qty"]
                amount_on_hand += r["qty"] * r["unit_price"]
            elif r["type"] == "issue":
                if r["qty"] > qty_on_hand:
                    raise ValidationError(
                        f"零件 {part_id!r} 在事件 {r['id']} 处结存为负，操作被拒绝")
                if r["qty"] == qty_on_hand:
                    cost = amount_on_hand  # 领空时带走全部金额，保证结存为 0
                else:
                    cost = _round_half_up(amount_on_hand * r["qty"], qty_on_hand)
                qty_on_hand -= r["qty"]
                amount_on_hand -= cost
                issue_cost[r["id"]] = (cost, r["qty"])
            else:  # return
                src_cost, src_qty = issue_cost[r["ref_id"]]
                back = _round_half_up(src_cost * r["qty"], src_qty)
                cost = -back
                qty_on_hand += r["qty"]
                amount_on_hand += back
            if qty_on_hand == 0 and amount_on_hand != 0:
                raise AssertionError("结存数量为 0 时金额必须为 0")
            updates.append((cost, qty_on_hand, amount_on_hand, r["id"]))

        conn.executemany(
            "UPDATE events SET cost = ?, qty_after = ?, amount_after = ? WHERE id = ?",
            updates)
