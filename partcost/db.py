"""SQLite 连接与建表。"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id       TEXT    NOT NULL,
    type          TEXT    NOT NULL CHECK (type IN ('in', 'issue', 'return')),
    qty           INTEGER NOT NULL CHECK (qty > 0),
    unit_price    INTEGER,
    business_time TEXT    NOT NULL,
    work_order_id TEXT,
    ref_id        INTEGER REFERENCES events(id),
    voided        INTEGER NOT NULL DEFAULT 0,
    cost          INTEGER,
    qty_after     INTEGER,
    amount_after  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_events_part_time
    ON events (part_id, business_time, id) WHERE voided = 0;

CREATE INDEX IF NOT EXISTS idx_events_wo
    ON events (work_order_id) WHERE voided = 0;

CREATE INDEX IF NOT EXISTS idx_events_ref
    ON events (ref_id) WHERE voided = 0;

CREATE TABLE IF NOT EXISTS work_orders (
    id          TEXT PRIMARY KEY,
    finish_date TEXT NOT NULL,
    revenue     INTEGER NOT NULL,
    labor       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wo_finish ON work_orders (finish_date);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并保证表结构存在。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
