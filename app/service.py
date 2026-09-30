"""Payment lifecycle service: state machine + idempotency + double-entry ledger.

Thread-safety model
-------------------
Every mutating request runs inside ``store.lock`` as one critical section:
idempotency lookup, validation, the gateway call, ledger posting and response
caching are atomic together. Concurrent duplicate submissions therefore
execute one after another; the first one books, the rest replay.

Failure model
-------------
The gateway is called *before* any local state is written. A simulated
channel failure changes nothing and is not cached, so retrying with the
same idempotency key is allowed and cannot double-book: the channel itself
dedupes by that key, and the store lock dedupes within this process.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from .errors import DomainError, ErrorCode
from .gateway import MockGateway
from .ledger import (
    ACC_AUTHORIZED_FUNDS,
    ACC_AUTHORIZED_RESERVE,
    ACC_CUSTOMER_CLEARING,
    ACC_MERCHANT_PAYABLE,
    ACC_REFUND_CLEARING,
    assert_balanced,
    build_entries,
)
from .models import (
    Entry,
    Operation,
    OperationType,
    Payment,
    PaymentStatus,
)
from .money import format_money, from_minor, to_minor, validate_currency
from .store import Store

logger = logging.getLogger("payment.service")


def _fingerprint(parts: dict) -> str:
    return json.dumps(parts, sort_keys=True, separators=(",", ":"))


class PaymentService:
    def __init__(self, store: Store | None = None, gateway: MockGateway | None = None) -> None:
        self.store = store or Store()
        self.gateway = gateway or MockGateway()
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        # Called while holding store.lock.
        self._seq += 1
        return f"{prefix}_{self._seq:08d}"

    # ------------------------------------------------------------------ DTOs
    @staticmethod
    def _entry_dto(e: Entry) -> dict:
        return {"account": e.account, "debit": e.debit, "credit": e.credit}

    def _op_dto(self, op: Operation) -> dict:
        return {
            "operation_id": op.operation_id,
            "type": op.op_type.value,
            "amount": op.amount,
            "currency": op.currency,
            "gateway_ref": op.gateway_ref,
            "idempotency_key": op.idempotency_key,
            "entries": [self._entry_dto(e) for e in op.entries],
        }

    def account_balances(self, payment: Payment) -> dict[str, int]:
        """Net debit balance per account across all entries of a payment."""
        balances: dict[str, int] = {}
        for op in payment.operations:
            for e in op.entries:
                balances[e.account] = balances.get(e.account, 0) + e.debit - e.credit
        return balances

    def _payment_dto(self, payment: Payment) -> dict:
        return {
            "payment_id": payment.payment_id,
            "currency": payment.currency,
            "status": payment.status.value,
            "authorized_amount": payment.authorized_amount,
            "captured_amount": payment.captured_amount,
            "refunded_amount": payment.refunded_amount,
            "refundable_amount": payment.refundable_amount,
            "authorized_display": format_money(payment.authorized_amount, payment.currency),
            "captured_display": format_money(payment.captured_amount, payment.currency),
            "refunded_display": format_money(payment.refunded_amount, payment.currency),
            "refundable_display": format_money(payment.refundable_amount, payment.currency),
            "operations": [self._op_dto(op) for op in payment.operations],
            "account_balances": self.account_balances(payment),
        }

    def _load(self, payment_id: str) -> Payment:
        payment = self.store.get_payment(payment_id)
        if payment is None:
            raise DomainError(
                ErrorCode.PAYMENT_NOT_FOUND,
                f"payment {payment_id!r} does not exist",
                payment_id=payment_id,
            )
        return payment

    def _idempotency_gate(self, key: str, fingerprint: str) -> dict | None:
        """Return cached response for a valid replay, None on first use.

        Raises IDEMPOTENCY_CONFLICT when the same key carries new content.
        """
        seen = self.store.find_idempotent(key)
        if seen is None:
            return None
        old_fp, response = seen
        if old_fp != fingerprint:
            raise DomainError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                f"idempotency key {key!r} was already used with a different request",
            )
        logger.info("idempotent replay key=%s -> returning cached response", key)
        return {**response, "replayed": True}

    def _finish(self, key: str, fingerprint: str, payment: Payment,
                op: Operation | None, replayed: bool) -> dict:
        assert_balanced(op.entries) if op else None
        self.assert_invariants(payment)
        response: dict = {"replayed": replayed, "payment": self._payment_dto(payment)}
        if op is not None:
            response["operation"] = self._op_dto(op)
        self.store.save_idempotent(key, fingerprint, response)
        return response

    # ------------------------------------------------------------ invariants
    def assert_invariants(self, p: Payment) -> None:
        """Cross-check amounts, status and ledger postings. Never returns silently
        if anything disagrees — raises INVALID_STATE_TRANSITION instead."""
        if not (0 <= p.refunded_amount <= p.captured_amount <= p.authorized_amount):
            raise DomainError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"amount invariant broken for {p.payment_id}: "
                f"0 <= refunded({p.refunded_amount}) <= captured({p.captured_amount}) "
                f"<= authorized({p.authorized_amount})",
                payment_id=p.payment_id,
            )
        # Status must agree with the amounts.
        captured_full = p.captured_amount == p.authorized_amount
        any_refund = p.refunded_amount > 0
        expected: set[PaymentStatus] = set()
        if p.status is PaymentStatus.AUTHORIZED:
            expected = {PaymentStatus.AUTHORIZED}
            ok = p.captured_amount == 0 and p.refunded_amount == 0
        elif p.status is PaymentStatus.PARTIALLY_CAPTURED:
            ok = 0 < p.captured_amount < p.authorized_amount and p.refunded_amount == 0
        elif p.status is PaymentStatus.CAPTURED:
            ok = captured_full and p.refunded_amount == 0
        elif p.status is PaymentStatus.PARTIALLY_REFUNDED:
            ok = any_refund and p.refunded_amount < p.captured_amount
        elif p.status is PaymentStatus.REFUNDED:
            ok = any_refund and p.refunded_amount == p.captured_amount
        elif p.status in (PaymentStatus.VOIDED, PaymentStatus.FAILED):
            ok = p.captured_amount == 0 and p.refunded_amount == 0
        else:
            ok = False
        if not ok:
            raise DomainError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"status {p.status.value} inconsistent with amounts for {p.payment_id}",
                payment_id=p.payment_id,
            )

        # Every individual operation must balance.
        for op in p.operations:
            assert_balanced(op.entries)

        # Ledger account nets must agree with the payment totals.
        bal = self.account_balances(p)
        uncaptured = p.authorized_amount - p.captured_amount if p.status is not PaymentStatus.VOIDED else 0
        # Balances are net debit (debit - credit); a pure credit account
        # therefore shows as a negative number.
        checks = {
            ACC_MERCHANT_PAYABLE: (-p.captured_amount, "net credit = captured"),
            ACC_REFUND_CLEARING: (p.refunded_amount, "net debit = refunded"),
            ACC_CUSTOMER_CLEARING: (p.refundable_amount, "net debit = captured - refunded"),
            ACC_AUTHORIZED_FUNDS: (uncaptured, "net debit = uncaptured memo"),
            ACC_AUTHORIZED_RESERVE: (-uncaptured, "net credit = uncaptured memo"),
        }
        for account, (want, why) in checks.items():
            got = bal.get(account, 0)
            if got != want:
                raise DomainError(
                    ErrorCode.INVALID_STATE_TRANSITION,
                    f"ledger mismatch on {account}: {got} != {want} ({why}) for {p.payment_id}",
                    payment_id=p.payment_id,
                )

    # -------------------------------------------------------------- authorize
    def authorize(self, amount: float | int | str | Decimal, currency: str,
                  idempotency_key: str) -> dict:
        code = validate_currency(currency)
        amount_minor = to_minor(amount, code)
        fp = _fingerprint({"op": "AUTHORIZE", "amount": amount_minor, "currency": code})
        logger.info("AUTHORIZE request amount=%s(%s) key=%s", amount, code, idempotency_key)

        with self.store.lock:
            cached = self._idempotency_gate(idempotency_key, fp)
            if cached is not None:
                return cached
            if amount_minor <= 0:
                raise DomainError(
                    ErrorCode.INVALID_AMOUNT,
                    f"authorization amount must be positive, got {amount}",
                )
            payment_id = self._next_id("pay")
            op_id = self._next_id("op")
            # Gateway first: failure here writes nothing, retry stays possible.
            gw_ref = self.gateway.authorize(idempotency_key, payment_id, amount_minor, code)

            payment = Payment(payment_id=payment_id, currency=code,
                              authorized_amount=amount_minor,
                              status=PaymentStatus.AUTHORIZED)
            op = Operation(
                operation_id=op_id, payment_id=payment_id,
                op_type=OperationType.AUTHORIZE, amount=amount_minor, currency=code,
                idempotency_key=idempotency_key, gateway_ref=gw_ref,
                entries=build_entries(OperationType.AUTHORIZE, amount_minor),
                created_at_seq=self._seq,
            )
            payment.operations.append(op)
            self.store.put_payment(payment)
            logger.info("AUTHORIZE accepted payment=%s gw=%s amount_minor=%s",
                        payment_id, gw_ref, amount_minor)
            return self._finish(idempotency_key, fp, payment, op, replayed=False)

    # ---------------------------------------------------------------- capture
    def capture(self, payment_id: str, amount: float | int | str | Decimal,
                currency: str, idempotency_key: str) -> dict:
        code = validate_currency(currency)
        amount_minor = to_minor(amount, code)
        fp = _fingerprint({"op": "CAPTURE", "payment_id": payment_id,
                           "amount": amount_minor, "currency": code})
        logger.info("CAPTURE request payment=%s amount=%s(%s) key=%s",
                    payment_id, amount, code, idempotency_key)

        with self.store.lock:
            cached = self._idempotency_gate(idempotency_key, fp)
            if cached is not None:
                return cached
            payment = self._load(payment_id)
            if payment.currency != code:
                raise DomainError(
                    ErrorCode.CURRENCY_MISMATCH,
                    f"request currency {code} != payment currency {payment.currency}",
                    payment_id=payment_id,
                )
            if amount_minor <= 0:
                raise DomainError(
                    ErrorCode.CAPTURE_AMOUNT_NOT_POSITIVE,
                    f"capture amount must be positive, got {amount}",
                    payment_id=payment_id,
                )
            status = payment.status
            if status is PaymentStatus.VOIDED:
                raise DomainError(ErrorCode.ALREADY_VOIDED,
                                  f"payment {payment_id} is voided; capture rejected",
                                  payment_id=payment_id)
            if status is PaymentStatus.FAILED:
                raise DomainError(ErrorCode.INVALID_STATE_TRANSITION,
                                  f"payment {payment_id} failed authorization; capture rejected",
                                  payment_id=payment_id)
            if status in (PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED):
                raise DomainError(ErrorCode.INVALID_STATE_TRANSITION,
                                  f"payment {payment_id} already has refunds; no further capture",
                                  payment_id=payment_id)
            if status is PaymentStatus.CAPTURED:
                raise DomainError(ErrorCode.PAYMENT_FULLY_SETTLED,
                                  f"payment {payment_id} is already fully captured",
                                  payment_id=payment_id)
            remaining = payment.authorized_amount - payment.captured_amount
            if amount_minor > remaining:
                raise DomainError(
                    ErrorCode.CAPTURE_EXCEEDS_AUTHORIZED,
                    f"capture {format_money(amount_minor, code)} exceeds remaining "
                    f"uncaptured {format_money(remaining, code)}",
                    payment_id=payment_id,
                )

            op_id = self._next_id("op")
            gw_ref = self.gateway.capture(idempotency_key, payment_id, amount_minor, code)

            payment.captured_amount += amount_minor
            payment.status = (PaymentStatus.CAPTURED if
                              payment.captured_amount == payment.authorized_amount
                              else PaymentStatus.PARTIALLY_CAPTURED)
            op = Operation(
                operation_id=op_id, payment_id=payment_id,
                op_type=OperationType.CAPTURE, amount=amount_minor, currency=code,
                idempotency_key=idempotency_key, gateway_ref=gw_ref,
                entries=build_entries(OperationType.CAPTURE, amount_minor),
                created_at_seq=self._seq,
            )
            payment.operations.append(op)
            self.store.put_payment(payment)
            logger.info("CAPTURE accepted payment=%s gw=%s captured_total=%s status=%s",
                        payment_id, gw_ref, payment.captured_amount, payment.status.value)
            return self._finish(idempotency_key, fp, payment, op, replayed=False)

    # ----------------------------------------------------------------- refund
    def refund(self, payment_id: str, amount: float | int | str | Decimal,
               currency: str, idempotency_key: str) -> dict:
        code = validate_currency(currency)
        amount_minor = to_minor(amount, code)
        fp = _fingerprint({"op": "REFUND", "payment_id": payment_id,
                           "amount": amount_minor, "currency": code})
        logger.info("REFUND request payment=%s amount=%s(%s) key=%s",
                    payment_id, amount, code, idempotency_key)

        with self.store.lock:
            cached = self._idempotency_gate(idempotency_key, fp)
            if cached is not None:
                return cached
            payment = self._load(payment_id)
            if payment.currency != code:
                raise DomainError(
                    ErrorCode.CURRENCY_MISMATCH,
                    f"request currency {code} != payment currency {payment.currency}",
                    payment_id=payment_id,
                )
            if amount_minor <= 0:
                raise DomainError(
                    ErrorCode.REFUND_AMOUNT_NOT_POSITIVE,
                    f"refund amount must be positive, got {amount}",
                    payment_id=payment_id,
                )
            if payment.status is PaymentStatus.VOIDED:
                raise DomainError(ErrorCode.ALREADY_VOIDED,
                                  f"payment {payment_id} is voided; refund rejected",
                                  payment_id=payment_id)
            if payment.captured_amount == 0:
                raise DomainError(
                    ErrorCode.REFUND_EXCEEDS_REFUNDABLE,
                    f"payment {payment_id} has no captured funds; nothing to refund",
                    payment_id=payment_id,
                )
            if amount_minor > payment.refundable_amount:
                raise DomainError(
                    ErrorCode.REFUND_EXCEEDS_REFUNDABLE,
                    f"refund {format_money(amount_minor, code)} exceeds refundable "
                    f"{format_money(payment.refundable_amount, code)}",
                    payment_id=payment_id,
                )

            op_id = self._next_id("op")
            gw_ref = self.gateway.refund(idempotency_key, payment_id, amount_minor, code)

            payment.refunded_amount += amount_minor
            payment.status = (PaymentStatus.REFUNDED if
                              payment.refunded_amount == payment.captured_amount
                              else PaymentStatus.PARTIALLY_REFUNDED)
            op = Operation(
                operation_id=op_id, payment_id=payment_id,
                op_type=OperationType.REFUND, amount=amount_minor, currency=code,
                idempotency_key=idempotency_key, gateway_ref=gw_ref,
                entries=build_entries(OperationType.REFUND, amount_minor),
                created_at_seq=self._seq,
            )
            payment.operations.append(op)
            self.store.put_payment(payment)
            logger.info("REFUND accepted payment=%s gw=%s refunded_total=%s status=%s",
                        payment_id, gw_ref, payment.refunded_amount, payment.status.value)
            return self._finish(idempotency_key, fp, payment, op, replayed=False)

    # ------------------------------------------------------------------- void
    def void(self, payment_id: str, idempotency_key: str) -> dict:
        fp = _fingerprint({"op": "VOID", "payment_id": payment_id})
        logger.info("VOID request payment=%s key=%s", payment_id, idempotency_key)

        with self.store.lock:
            cached = self._idempotency_gate(idempotency_key, fp)
            if cached is not None:
                return cached
            payment = self._load(payment_id)
            status = payment.status
            if status is PaymentStatus.VOIDED:
                raise DomainError(ErrorCode.ALREADY_VOIDED,
                                  f"payment {payment_id} is already voided",
                                  payment_id=payment_id)
            if payment.captured_amount > 0:
                raise DomainError(ErrorCode.ALREADY_CAPTURED,
                                  f"payment {payment_id} already has captures; "
                                  f"void rejected, use refund instead",
                                  payment_id=payment_id)
            if status is PaymentStatus.FAILED:
                raise DomainError(ErrorCode.NOTHING_TO_VOID,
                                  f"payment {payment_id} failed authorization; nothing to void",
                                  payment_id=payment_id)

            op_id = self._next_id("op")
            gw_ref = self.gateway.void(idempotency_key, payment_id)

            payment.status = PaymentStatus.VOIDED
            op = Operation(
                operation_id=op_id, payment_id=payment_id,
                op_type=OperationType.VOID, amount=payment.authorized_amount,
                currency=payment.currency, idempotency_key=idempotency_key,
                gateway_ref=gw_ref,
                entries=build_entries(OperationType.VOID, payment.authorized_amount),
                created_at_seq=self._seq,
            )
            payment.operations.append(op)
            self.store.put_payment(payment)
            logger.info("VOID accepted payment=%s gw=%s status=%s",
                        payment_id, gw_ref, payment.status.value)
            return self._finish(idempotency_key, fp, payment, op, replayed=False)

    # ------------------------------------------------------------- inspections
    def get_payment(self, payment_id: str) -> dict:
        with self.store.lock:
            payment = self._load(payment_id)
            self.assert_invariants(payment)
            return {"replayed": False, "payment": self._payment_dto(payment)}
