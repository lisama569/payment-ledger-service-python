"""SQLite 持久化（本地文件，无任何外部服务）。

并发模型：
- 每个业务动作在单个数据库事务内完成（BEGIN IMMEDIATE ... COMMIT）；
- 幂等键有 UNIQUE 约束兜底，并发重复提交在事务串行化后要么执行一次、
  要么拿到已完成结果重放，绝不重复记账；
- 金额列全部为非负整数（最小货币单位）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id        TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    currency          TEXT NOT NULL,
    amount_authorized INTEGER NOT NULL CHECK (amount_authorized > 0),
    amount_captured   INTEGER NOT NULL NOT NULL DEFAULT 0 CHECK (amount_captured >= 0),
    amount_refunded   INTEGER NOT NULL NOT NULL DEFAULT 0 CHECK (amount_refunded >= 0),
    amount_voided     INTEGER NOT NULL NOT NULL DEFAULT 0 CHECK (amount_voided >= 0),
    closed            INTEGER NOT NULL NOT NULL DEFAULT 0 CHECK (closed IN (0, 1)),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (amount_captured + amount_voided <= amount_authorized),
    CHECK (amount_refunded <= amount_captured)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id      TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('authorize', 'capture', 'refund', 'void')),
    idempotency_key TEXT NOT NULL,
    action_seq      INTEGER NOT NULL,
    account         TEXT NOT NULL,
    debit           INTEGER NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit          INTEGER NOT NULL DEFAULT 0 CHECK (credit >= 0),
    CHECK (debit = 0 OR credit = 0),
    FOREIGN KEY (payment_id) REFERENCES payments(payment_id)
);
CREATE INDEX IF NOT EXISTS idx_entries_payment ON ledger_entries(payment_id, id);

CREATE TABLE IF NOT EXISTS idempotency_records (
    idempotency_key     TEXT PRIMARY KEY,
    payment_id          TEXT NOT NULL,
    action              TEXT NOT NULL CHECK (action IN ('authorize', 'capture', 'refund', 'void')),
    request_fingerprint TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started', 'completed')),
    result_json         TEXT
);
"""

DEFAULT_DB_PATH = Path(os.environ.get("PAYMENT_LEDGER_DB", "data/payment_ledger.db"))


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开一个连接。autocommit 模式，事务由调用方显式管理。"""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        isolation_level=None,  # autocommit：我们自己发 BEGIN/COMMIT
        timeout=10.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if path != Path(":memory:"):
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
