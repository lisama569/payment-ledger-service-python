"""非法状态流转 / 金额校验：必须被拒绝且原因可区分，状态不被静默改动。"""

from __future__ import annotations

import logging

import pytest

from app.errors import (
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

log = logging.getLogger("tests")


def test_capture_after_void_rejected(service):
    service.authorize("p", 1000, "USD", "ka")
    service.void("p", "kv")
    with pytest.raises(CaptureNotAllowedError) as ei:
        service.capture("p", 1, "USD", "kc")
    log.info("capture after void -> %s", ei.value.code)
    view = service.get_payment("p")
    assert view["status"] == "VOIDED" and view["amount_captured"] == 0


def test_partial_capture_then_void_rest_then_capture_rejected(service):
    service.authorize("p", 1000, "USD", "ka")
    service.capture("p", 300, "USD", "kc1")
    r = service.void("p", "kv")
    assert r["amount_voided"] == 700
    with pytest.raises(CaptureNotAllowedError) as ei:
        service.capture("p", 1, "USD", "kc2")
    log.info("capture after void-rest -> %s", ei.value.code)
    view = service.get_payment("p")
    assert view["open_authorized_amount"] == 0


def test_capture_exceeding_authorized_uses_distinct_code(service):
    service.authorize("p", 1000, "USD", "ka")
    service.capture("p", 600, "USD", "kc1")
    with pytest.raises(CaptureExceedsAuthorizedError) as ei:
        service.capture("p", 401, "USD", "kc2")
    log.info("over-capture 401 > open 400 -> %s", ei.value.code)
    # 未记账
    assert service.get_payment("p")["amount_captured"] == 600


def test_refund_before_any_capture_distinct_code(service):
    service.authorize("p", 1000, "USD", "ka")
    with pytest.raises(RefundExceedsCapturedError) as ei:
        service.refund("p", 1, "USD", "kr")
    log.info("refund with zero captured -> %s", ei.value.code)


def test_refund_over_refundable_distinct_code(service):
    service.authorize("p", 1000, "USD", "ka")
    service.capture("p", 600, "USD", "kc")
    service.refund("p", 500, "USD", "kr1")
    with pytest.raises(RefundExceedsRefundableError) as ei:
        service.refund("p", 101, "USD", "kr2")
    log.info("refund 101 > refundable 100 -> %s", ei.value.code)
    view = service.get_payment("p")
    assert view["amount_refunded"] == 500 and view["refundable_amount"] == 100


def test_void_twice_rejected(service):
    service.authorize("p", 1000, "USD", "ka")
    service.void("p", "kv1")
    with pytest.raises(VoidNotAllowedError) as ei:
        service.void("p", "kv2")
    log.info("second void -> %s", ei.value.code)


def test_duplicate_authorize_rejected(service):
    service.authorize("p", 1000, "USD", "ka1")
    with pytest.raises(PaymentAlreadyExistsError) as ei:
        service.authorize("p", 1000, "USD", "ka2")
    log.info("duplicate authorize -> %s", ei.value.code)


def test_currency_mismatch_rejected(service):
    service.authorize("p", 1000, "USD", "ka")
    with pytest.raises(CurrencyMismatchError) as ei:
        service.capture("p", 100, "EUR", "kc")
    log.info("capture EUR on USD payment -> %s", ei.value.code)
    with pytest.raises(CurrencyMismatchError) as ei:
        service.refund("p", 100, "GBP", "kr")
    log.info("refund GBP on USD payment -> %s", ei.value.code)


def test_non_positive_amount_rejected(service):
    with pytest.raises(InvalidAmountError):
        service.authorize("p", 0, "USD", "ka")
    with pytest.raises(InvalidAmountError):
        service.capture("p", -5, "USD", "kc")
    log.info("non-positive amounts rejected with invalid_amount")


def test_missing_payment_404(service):
    with pytest.raises(PaymentNotFoundError) as ei:
        service.capture("ghost", 1, "USD", "kc")
    log.info("capture on missing payment -> %s", ei.value.code)


def test_partial_captures_and_refunds_totals(service):
    service.authorize("p", 1000, "USD", "ka")
    r1 = service.capture("p", 300, "USD", "kc1")
    r2 = service.capture("p", 300, "USD", "kc2")
    assert r2["amount_captured"] == 600
    r = service.refund("p", 100, "USD", "kr1")
    assert r["refundable_amount"] == 500
    r = service.refund("p", 200, "USD", "kr2")
    assert r["refundable_amount"] == 300 and r["amount_refunded"] == 300
    log.info("partial ops totals captured=%s refunded=%s refundable=%s",
             r["amount_captured"], r["amount_refunded"], r["refundable_amount"])
