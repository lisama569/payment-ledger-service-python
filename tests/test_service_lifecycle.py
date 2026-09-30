"""Lifecycle, illegal transitions and partial capture/refund rules."""
import logging

import pytest

from app.errors import DomainError, ErrorCode
from app.service import PaymentService

logger = logging.getLogger(__name__)


def make_service():
    return PaymentService()


def authorize(svc, amount=100.0, ccy="USD", key="k-auth-1"):
    resp = svc.authorize(amount, ccy, key)
    return resp["payment"]["payment_id"], resp


def assert_error(exc, code: ErrorCode):
    logger.info("expected rejection -> code=%s message=%s",
                exc.value.code.value, exc.value.message)
    assert exc.value.code is code


def test_authorize_then_full_capture_then_void_rejected():
    svc = make_service()
    pid, auth = authorize(svc, 100, "USD", "k-a")
    assert auth["payment"]["status"] == "AUTHORIZED"
    cap = svc.capture(pid, 100, "USD", "k-c")["payment"]
    logger.info("after full capture -> status=%s captured=%s refundable=%s",
                cap["status"], cap["captured_amount"], cap["refundable_amount"])
    assert cap["status"] == "CAPTURED"
    assert cap["refundable_amount"] == 10000

    with pytest.raises(DomainError) as exc:
        svc.void(pid, "k-v")
    assert_error(exc, ErrorCode.ALREADY_CAPTURED)

    # capturing again on fully captured is rejected distinctly
    with pytest.raises(DomainError) as exc:
        svc.capture(pid, 1, "USD", "k-c2")
    assert_error(exc, ErrorCode.PAYMENT_FULLY_SETTLED)


def test_partial_captures_and_partial_refunds_balances():
    svc = make_service()
    pid, _ = authorize(svc, 100, "USD", "k-a")

    c1 = svc.capture(pid, 30, "USD", "k-c1")["payment"]
    logger.info("capture 30 -> status=%s captured=%s", c1["status"], c1["captured_amount"])
    assert c1["status"] == "PARTIALLY_CAPTURED"

    c2 = svc.capture(pid, 50, "USD", "k-c2")["payment"]
    assert c2["status"] == "PARTIALLY_CAPTURED"
    assert c2["captured_amount"] == 8000

    # refund part of the captured funds
    r1 = svc.refund(pid, 20, "USD", "k-r1")["payment"]
    logger.info("refund 20 -> status=%s refunded=%s refundable=%s",
                r1["status"], r1["refunded_amount"], r1["refundable_amount"])
    assert r1["status"] == "PARTIALLY_REFUNDED"
    assert r1["refunded_amount"] == 2000
    assert r1["refundable_amount"] == 6000

    # capture after refund must be rejected (cannot refill refundable)
    with pytest.raises(DomainError) as exc:
        svc.capture(pid, 20, "USD", "k-c3")
    assert_error(exc, ErrorCode.INVALID_STATE_TRANSITION)

    r2 = svc.refund(pid, 60, "USD", "k-r2")["payment"]
    assert r2["status"] == "REFUNDED"
    assert r2["refunded_amount"] == 8000
    assert r2["refundable_amount"] == 0

    # refund after fully refunded exceeds refundable
    with pytest.raises(DomainError) as exc:
        svc.refund(pid, 1, "USD", "k-r3")
    assert_error(exc, ErrorCode.REFUND_EXCEEDS_REFUNDABLE)

    final = svc.get_payment(pid)["payment"]
    # ledger account nets reconcile with totals
    bal = final["account_balances"]
    assert bal["CUSTOMER_CLEARING"] == 0
    assert bal["MERCHANT_PAYABLE"] == -8000
    assert bal["REFUND_CLEARING"] == 8000
    assert bal["AUTHORIZED_FUNDS"] == 2000  # 100 authorized - 80 captured
    logger.info("final reconciliation OK: %s", bal)


def test_capture_exceeding_authorized_rejected():
    svc = make_service()
    pid, _ = authorize(svc, 10, "USD", "k-a")
    with pytest.raises(DomainError) as exc:
        svc.capture(pid, 10.01, "USD", "k-c")
    assert_error(exc, ErrorCode.CAPTURE_EXCEEDS_AUTHORIZED)


def test_void_then_capture_or_refund_rejected():
    svc = make_service()
    pid, _ = authorize(svc, 10, "USD", "k-a")
    svc.void(pid, "k-v")
    with pytest.raises(DomainError) as exc:
        svc.capture(pid, 1, "USD", "k-c")
    assert_error(exc, ErrorCode.ALREADY_VOIDED)
    with pytest.raises(DomainError) as exc:
        svc.refund(pid, 1, "USD", "k-r")
    assert_error(exc, ErrorCode.ALREADY_VOIDED)
    with pytest.raises(DomainError) as exc:
        svc.void(pid, "k-v2")
    assert_error(exc, ErrorCode.ALREADY_VOIDED)


def test_refund_before_capture_rejected():
    svc = make_service()
    pid, _ = authorize(svc, 10, "USD", "k-a")
    with pytest.raises(DomainError) as exc:
        svc.refund(pid, 10, "USD", "k-r")
    assert_error(exc, ErrorCode.REFUND_EXCEEDS_REFUNDABLE)


def test_currency_mismatch_rejected():
    svc = make_service()
    pid, _ = authorize(svc, 10, "USD", "k-a")
    with pytest.raises(DomainError) as exc:
        svc.capture(pid, 10, "EUR", "k-c")
    assert_error(exc, ErrorCode.CURRENCY_MISMATCH)


def test_non_positive_amounts_rejected():
    svc = make_service()
    with pytest.raises(DomainError) as exc:
        svc.authorize(0, "USD", "k-zero")
    assert_error(exc, ErrorCode.INVALID_AMOUNT)
    pid, _ = authorize(svc, 10, "USD", "k-a")
    with pytest.raises(DomainError) as exc:
        svc.capture(pid, 0, "USD", "k-c")
    assert_error(exc, ErrorCode.CAPTURE_AMOUNT_NOT_POSITIVE)
    svc.capture(pid, 5, "USD", "k-c2")
    with pytest.raises(DomainError) as exc:
        svc.refund(pid, -1, "USD", "k-r")
    assert_error(exc, ErrorCode.REFUND_AMOUNT_NOT_POSITIVE)


def test_every_operation_has_balanced_entries():
    svc = make_service()
    pid, _ = authorize(svc, 100, "USD", "k-a")
    svc.capture(pid, 40, "USD", "k-c1")
    svc.refund(pid, 10, "USD", "k-r1")
    p = svc.get_payment(pid)["payment"]
    for op in p["operations"]:
        d = sum(e["debit"] for e in op["entries"])
        c = sum(e["credit"] for e in op["entries"])
        assert d == c and d > 0
        logger.info("op %s amount=%s balanced debit=credit=%s",
                    op["type"], op["amount"], d)
