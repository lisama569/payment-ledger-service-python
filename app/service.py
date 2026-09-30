"""支付领域服务。

一个业务动作 = 一个 BEGIN IMMEDIATE 事务，事务内依次完成：
幂等登记/校验 -> 状态与金额校验 -> 模拟渠道调用 -> 更新支付汇总
-> 写复式分录 -> 落地幂等结果 -> COMMIT。任何一步抛出，整体回滚。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .errors import (
    CaptureExceedsAuthorizedError,
    CaptureNotAllowedError,
    CurrencyMismatchError,
    InvalidAmountError,
    PaymentAlreadyExistsError,
    PaymentNotFoundError,
    RefundExceedsCapturedError,
    RefundExceedsRefundableError,
    VoidNotAllowedError,
)
from .idempotency import begin_idempotent, finish_idempotent, fingerprint_for
from .ledger import entries_for, is_balanced
from .mock_gateway import MockGateway

def _derive_status(captured: int, refunded: int, authorized: int, closed: bool) -> str:
    if captured == 0:
        # 未捕获即撤销：授权余额全部释放
        return "VOIDED" if closed else "AUTHORIZED"
    if closed:
        # 授权额度已全部用完（捕获 + 撤销之和等于授权）
        if refunded == 0:
            return "CAPTURED"
        return "REFUNDED" if refunded == captured else "PARTIALLY_REFUNDED"
    # 仍有未处置的授权额度
    if refunded > 0:
        return "PARTIALLY_CAPTURED_REFUNDED"
    return "PARTIALLY_CAPTURED"


class PaymentService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        gateway: MockGateway | None = None,
    ) -> None:
        self.conn = conn
        self.gateway = gateway or MockGateway()

    # ---------- 内部工具 ----------

    @contextmanager
    def _txn(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def _get_row(self, payment_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM payments WHERE payment_id = ?", (payment_id,)
        ).fetchone()

    def _view_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        captured, refunded = row["amount_captured"], row["amount_refunded"]
        authorized, voided = row["amount_authorized"], row["amount_voided"]
        closed = bool(row["closed"])
        return {
            "payment_id": row["payment_id"],
            "status": _derive_status(captured, refunded, authorized, closed),
            "currency": row["currency"],
            "amount_authorized": authorized,
            "amount_captured": captured,
            "amount_refunded": refunded,
            "amount_voided": voided,
            "refundable_amount": captured - refunded,
            "open_authorized_amount": authorized - captured - voided,
        }

    def _result(
        self,
        action: str,
        row: sqlite3.Row,
        entries: list[dict[str, Any]],
        replayed: bool,
        amount: int,
    ) -> dict[str, Any]:
        view = self._view_from_row(row)
        return {
            **view,
            "action": action,
            "replayed": replayed,
            "amount": amount,
            "entries": entries,
        }

    def _entries_for_payment(self, payment_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT account, debit, credit FROM ledger_entries "
            "WHERE payment_id = ? ORDER BY id",
            (payment_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _next_action_seq(self, payment_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(action_seq), 0) AS m FROM ledger_entries "
            "WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()
        return int(row["m"]) + 1

    def _write_entries(
        self,
        action: str,
        payment_id: str,
        idempotency_key: str,
        amount: int,
    ) -> list[dict[str, Any]]:
        entries = entries_for(action, amount)
        if not is_balanced(entries):
            raise RuntimeError("refusing to post unbalanced entries")
        seq = self._next_action_seq(payment_id)
        for e in entries:
            self.conn.execute(
                "INSERT INTO ledger_entries "
                "(payment_id, action, idempotency_key, action_seq, account, debit, credit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (payment_id, action, idempotency_key, seq, e.account, e.debit, e.credit),
            )
        return [
            {"account": e.account, "debit": e.debit, "credit": e.credit}
            for e in entries
        ]

    def _validate_invariants(self, payment_id: str) -> None:
        row = self._get_row(payment_id)
        assert row is not None
        a, c, r, v = (
            row["amount_authorized"],
            row["amount_captured"],
            row["amount_refunded"],
            row["amount_voided"],
        )
        if not (c + v <= a and r <= c and min(a, c, r, v) >= 0):
            raise RuntimeError(f"payment totals invariant broken for {payment_id}")
        sums = self.conn.execute(
            "SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS cr "
            "FROM ledger_entries WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()
        if sums["d"] != sums["cr"]:
            raise RuntimeError(f"ledger not balanced for {payment_id}")
        # customer_receivable 借余 = 已授权 - 已退款 - 已撤销
        #                         = 可退余额(c-r) + 仍开放的授权(a-c-v)
        bal = self.conn.execute(
            "SELECT COALESCE(SUM(debit - credit),0) AS b FROM ledger_entries "
            "WHERE payment_id = ? AND account = 'customer_receivable'",
            (payment_id,),
        ).fetchone()["b"]
        expected = a - r - v
        if bal != expected or expected != (c - r) + (a - c - v):
            raise RuntimeError(f"receivable/refundable mismatch for {payment_id}")

    def _run_idempotent(
        self,
        *,
        action: str,
        payment_id: str,
        amount: int | None,
        currency: str | None,
        idempotency_key: str,
        apply: Any,
    ) -> dict[str, Any]:
        fp = fingerprint_for(action, payment_id, amount, currency)
        with self._txn():
            replay = begin_idempotent(self.conn, idempotency_key, payment_id, action, fp)
            if replay is not None:
                stored = json.loads(replay.result_json)
                stored["replayed"] = True
                return stored
            result = apply()
            self._validate_invariants(payment_id)
            row = self._get_row(payment_id)
            assert row is not None
            effective_amount = result.get("amount")
            if effective_amount is None:
                effective_amount = amount if amount is not None else 0
            result = self._result(
                action, row, result["entries"], False, effective_amount
            )
            finish_idempotent(self.conn, idempotency_key, json.dumps(result, sort_keys=True))
            return result

    # ---------- 公共动作 ----------

    def authorize(
        self, payment_id: str, amount: int, currency: str, idempotency_key: str
    ) -> dict[str, Any]:
        if amount <= 0:
            raise InvalidAmountError("amount must be a positive integer")
        currency = currency.upper()

        def apply() -> dict[str, Any]:
            if self._get_row(payment_id) is not None:
                raise PaymentAlreadyExistsError(
                    f"payment {payment_id} already exists", payment_id=payment_id
                )
            self.gateway.process("authorize", payment_id, amount, currency)
            status = _derive_status(0, 0, amount, False)
            self.conn.execute(
                "INSERT INTO payments "
                "(payment_id, status, currency, amount_authorized) VALUES (?, ?, ?, ?)",
                (payment_id, status, currency, amount),
            )
            entries = self._write_entries(
                "authorize", payment_id, idempotency_key, amount
            )
            return {"entries": entries}

        return self._run_idempotent(
            action="authorize",
            payment_id=payment_id,
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
            apply=apply,
        )

    def capture(
        self, payment_id: str, amount: int, currency: str, idempotency_key: str
    ) -> dict[str, Any]:
        if amount <= 0:
            raise InvalidAmountError("amount must be a positive integer")
        currency = currency.upper()

        def apply() -> dict[str, Any]:
            row = self._require_open(payment_id)
            self._check_currency(row, currency)
            if row["closed"]:
                raise CaptureNotAllowedError(
                    f"payment {payment_id} is closed; no open authorization to capture",
                    payment_id=payment_id,
                )
            open_amount = (
                row["amount_authorized"] - row["amount_captured"] - row["amount_voided"]
            )
            if amount > open_amount:
                raise CaptureExceedsAuthorizedError(
                    f"capture {amount} exceeds open authorization {open_amount}",
                    payment_id=payment_id,
                )
            self.gateway.process(
                "capture", payment_id, amount, row["currency"]
            )
            self._bump(
                row,
                captured=row["amount_captured"] + amount,
                closed=row["closed"],
            )
            return {
                "entries": self._write_entries(
                    "capture", payment_id, idempotency_key, amount
                )
            }

        return self._run_idempotent(
            action="capture",
            payment_id=payment_id,
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
            apply=apply,
        )

    def refund(
        self, payment_id: str, amount: int, currency: str, idempotency_key: str
    ) -> dict[str, Any]:
        if amount <= 0:
            raise InvalidAmountError("amount must be a positive integer")
        currency = currency.upper()

        def apply() -> dict[str, Any]:
            row = self._require_open(payment_id)
            self._check_currency(row, currency)
            if row["amount_captured"] == 0:
                raise RefundExceedsCapturedError(
                    f"payment {payment_id} has no captured funds to refund",
                    payment_id=payment_id,
                )
            refundable = row["amount_captured"] - row["amount_refunded"]
            if amount > refundable:
                raise RefundExceedsRefundableError(
                    f"refund {amount} exceeds refundable amount {refundable}",
                    payment_id=payment_id,
                )
            self.gateway.process(
                "refund", payment_id, amount, row["currency"]
            )
            self._bump(
                row,
                refunded=row["amount_refunded"] + amount,
                closed=row["closed"],
            )
            return {
                "entries": self._write_entries(
                    "refund", payment_id, idempotency_key, amount
                )
            }

        return self._run_idempotent(
            action="refund",
            payment_id=payment_id,
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
            apply=apply,
        )

    def void(self, payment_id: str, idempotency_key: str) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            row = self._require_open(payment_id)
            open_amount = (
                row["amount_authorized"]
                - row["amount_captured"]
                - row["amount_voided"]
            )
            if open_amount <= 0:
                # 授权额度已全部捕获或撤销：没有可撤销的东西
                raise VoidNotAllowedError(
                    f"payment {payment_id} has no open authorization to void",
                    payment_id=payment_id,
                )
            self.gateway.process("void", payment_id, open_amount, row["currency"])
            self._bump(
                row,
                voided=row["amount_voided"] + open_amount,
                closed=True,
            )
            return {
                "amount": open_amount,
                "entries": self._write_entries(
                    "void", payment_id, idempotency_key, open_amount
                ),
            }

        return self._run_idempotent(
            action="void",
            payment_id=payment_id,
            amount=None,
            currency=None,
            idempotency_key=idempotency_key,
            apply=apply,
        )

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        row = self._get_row(payment_id)
        if row is None:
            raise PaymentNotFoundError(
                f"payment {payment_id} not found", payment_id=payment_id
            )
        view = self._view_from_row(row)
        view["entries"] = self._entries_for_payment(payment_id)
        return view

    # ---------- 共享校验/更新 ----------

    def _require_open(self, payment_id: str) -> sqlite3.Row:
        row = self._get_row(payment_id)
        if row is None:
            raise PaymentNotFoundError(
                f"payment {payment_id} not found", payment_id=payment_id
            )
        return row

    @staticmethod
    def _check_currency(row: sqlite3.Row, currency: str) -> None:
        if currency != row["currency"]:
            raise CurrencyMismatchError(
                f"request currency {currency} does not match payment currency "
                f"{row['currency']}",
                payment_id=row["payment_id"],
            )

    def _bump(
        self,
        row: sqlite3.Row,
        *,
        captured: int | None = None,
        refunded: int | None = None,
        voided: int | None = None,
        closed: bool,
    ) -> None:
        payment_id = row["payment_id"]
        new_captured = row["amount_captured"] if captured is None else captured
        new_refunded = row["amount_refunded"] if refunded is None else refunded
        new_voided = row["amount_voided"] if voided is None else voided
        # 已捕获 + 已撤销 达到授权额时，授权额度自动关闭
        new_closed = closed or (new_captured + new_voided >= row["amount_authorized"])
        new_status = _derive_status(
            new_captured, new_refunded, row["amount_authorized"], new_closed
        )
        self.conn.execute(
            "UPDATE payments SET amount_captured = ?, amount_refunded = ?, "
            "amount_voided = ?, closed = ?, status = ?, updated_at = datetime('now') "
            "WHERE payment_id = ?",
            (
                new_captured,
                new_refunded,
                new_voided,
                1 if new_closed else 0,
                new_status,
                payment_id,
            ),
        )
