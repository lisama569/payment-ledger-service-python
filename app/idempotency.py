"""幂等记录存取。

约定：调用方必须已经持有 BEGIN IMMEDIATE 打开的写事务。
- 首次见到 key：插入 status='started' 占位行，返回 None（调用方继续执行业务）。
- 已 completed 且请求指纹一致：返回 Replay，调用方重放原结果，绝不再记账。
- 已 completed 但指纹不一致：抛 IdempotencyConflictError，明确拒绝。

status='started' 的行在正常运行中不可能残留（业务与该占位行在同一事务，
失败则整体回滚）；若观测到，按内部冲突拒绝，绝不静默继续。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .errors import IdempotencyConflictError


@dataclass(frozen=True)
class Replay:
    result_json: str


def fingerprint_for(
    action: str,
    payment_id: str,
    amount: int | None,
    currency: str | None,
) -> str:
    payload = {
        "action": action,
        "payment_id": payment_id,
        "amount": amount,
        "currency": currency,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def begin_idempotent(
    conn: sqlite3.Connection,
    idempotency_key: str,
    payment_id: str,
    action: str,
    request_fingerprint: str,
) -> Replay | None:
    row = conn.execute(
        "SELECT request_fingerprint, status, result_json "
        "FROM idempotency_records WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()

    if row is None:
        try:
            conn.execute(
                "INSERT INTO idempotency_records "
                "(idempotency_key, payment_id, action, request_fingerprint, status) "
                "VALUES (?, ?, ?, ?, 'started')",
                (idempotency_key, payment_id, action, request_fingerprint),
            )
        except sqlite3.IntegrityError:
            # 兜底：唯一约束竞态（正常已被 BEGIN IMMEDIATE 串行化），重读后递归一次。
            return begin_idempotent(
                conn, idempotency_key, payment_id, action, request_fingerprint
            )
        return None

    if row["status"] != "completed" or row["result_json"] is None:
        raise IdempotencyConflictError(
            "idempotency key has an in-flight record; retry later",
            payment_id=payment_id,
        )
    if row["request_fingerprint"] != request_fingerprint:
        raise IdempotencyConflictError(
            "idempotency key was already used with a different request body",
            payment_id=payment_id,
        )
    return Replay(result_json=row["result_json"])


def finish_idempotent(
    conn: sqlite3.Connection,
    idempotency_key: str,
    result_json: str,
) -> None:
    conn.execute(
        "UPDATE idempotency_records "
        "SET status = 'completed', result_json = ? WHERE idempotency_key = ?",
        (result_json, idempotency_key),
    )
