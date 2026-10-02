"""SQLite 表结构定义与连接管理。"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    part_no     TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS work_orders (
    id              INTEGER PRIMARY KEY,
    finish_date     TEXT NOT NULL,          -- YYYY-MM-DD
    revenue_cents   INTEGER NOT NULL,       -- 不含税收入（分）
    labor_cents     INTEGER NOT NULL,       -- 人工成本（分）
    parts_net_cents INTEGER NOT NULL DEFAULT 0  -- 配件净成本（分），由模块维护
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,    -- 录入先后即 id 顺序
    part_no         TEXT NOT NULL REFERENCES parts(part_no),
    type            TEXT NOT NULL CHECK (type IN ('in', 'issue', 'return')),
    business_time   TEXT NOT NULL,          -- 业务时间 ISO 格式
    qty             INTEGER NOT NULL,
    unit_price_cents INTEGER,               -- 仅入库
    work_order_id   INTEGER REFERENCES work_orders(id),  -- 领用/退回所属工单
    issue_event_id  INTEGER REFERENCES events(id),       -- 仅退回：原领用事件
    voided          INTEGER NOT NULL DEFAULT 0,
    -- 以下为重算后缓存的结果
    cost_cents      INTEGER,                -- 入库=数量*单价；领用=领用成本；退回=退回金额
    qty_after       INTEGER,                -- 该事件生效后的结存数量
    amount_after    INTEGER                 -- 该事件生效后的结存金额（分）
);

CREATE INDEX IF NOT EXISTS idx_events_part_time
    ON events (part_no, business_time, id);
CREATE INDEX IF NOT EXISTS idx_events_wo
    ON events (work_order_id) WHERE voided = 0;
CREATE INDEX IF NOT EXISTS idx_wo_finish
    ON work_orders (finish_date);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并确保表结构存在。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
