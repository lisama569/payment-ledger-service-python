"""Idempotency: replays, key/content conflicts, retry after channel failure."""
import logging

import pytest

from app.errors import DomainError, ErrorCode
from app.service import PaymentService

logger = logging.getLogger(__name__)


def test_duplicate_authorize_returns_same_payment_and_books_once():
    svc = PaymentService()
    r1 = svc.authorize(100, "USD", "dup-key-1")
    r2 = svc.authorize(100, "USD", "dup-key-1")
    logger.info("same authorize key twice -> ids %s / %s, replayed=%s",
                r1["payment"]["payment_id"], r2["payment"]["payment_id"],
                r2["replayed"])
    assert r1["payment"]["payment_id"] == r2["payment"]["payment_id"]
    assert r2["replayed"] is True
    p = r2["payment"]
    assert len(p["operations"]) == 1
    assert p["authorized_amount"] == 10000


def test_same_key_different_body_is_rejected_for_each_op():
    svc = PaymentService()
    svc.authorize(100, "USD", "k-auth")
    with pytest.raises(DomainError) as exc:
        svc.authorize(101, "USD", "k-auth")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    logger.info("authorize same key different amount -> %s", exc.value.code.value)

    pid = svc.authorize(100, "USD", "k-auth")["payment"]["payment_id"]
    svc.capture(pid, 10, "USD", "k-cap")
    with pytest.raises(DomainError) as exc:
        svc.capture(pid, 11, "USD", "k-cap")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    logger.info("capture same key different amount -> %s", exc.value.code.value)

    with pytest.raises(DomainError) as exc:
        svc.capture(pid, 10, "EUR", "k-cap")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    logger.info("capture same key different currency -> %s", exc.value.code.value)

    svc.refund(pid, 5, "USD", "k-ref")
    with pytest.raises(DomainError) as exc:
        svc.refund(pid, 6, "USD", "k-ref")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT

    svc.void(pid, "k-void") if False else None
    pid2 = svc.authorize(50, "USD", "k-auth2")["payment"]["payment_id"]
    svc.void(pid2, "k-void")
    with pytest.raises(DomainError) as exc:
        svc.capture(pid2, 1, "USD", "k-void")  # key reused with another op shape
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    logger.info("key reused across different operations -> %s", exc.value.code.value)


def test_replay_does_not_rebook_after_partial_actions():
    svc = PaymentService()
    pid = svc.authorize(100, "USD", "ka")["payment"]["payment_id"]
    svc.capture(pid, 40, "USD", "kc")
    replay = svc.capture(pid, 40, "USD", "kc")["payment"]
    logger.info("capture replay -> captured=%s ops=%s",
                replay["captured_amount"], len(replay["operations"]))
    assert replay["captured_amount"] == 4000
    ops = [o for o in replay["operations"] if o["type"] == "CAPTURE"]
    assert len(ops) == 1


def test_channel_failure_then_retry_same_key_succeeds_once():
    svc = PaymentService()
    pid = svc.authorize(100, "USD", "ka")["payment"]["payment_id"]
    # Make the capture fail transiently at the simulated channel.
    svc.gateway.fail_once.add("kc-fail")
    with pytest.raises(DomainError) as exc:
        svc.capture(pid, 30, "USD", "kc-fail")
    assert exc.value.code is ErrorCode.GATEWAY_FAILURE
    logger.info("first capture failed (%s); state must be untouched",
                exc.value.code.value)
    untouched = svc.get_payment(pid)["payment"]
    assert untouched["captured_amount"] == 0
    assert untouched["status"] == "AUTHORIZED"
    assert len(untouched["operations"]) == 1

    ok = svc.capture(pid, 30, "USD", "kc-fail")["payment"]
    logger.info("retry capture after failure -> captured=%s status=%s",
                ok["captured_amount"], ok["status"])
    assert ok["captured_amount"] == 3000
    assert ok["status"] == "PARTIALLY_CAPTURED"

    # And again -> plain replay, still one booking
    again = svc.capture(pid, 30, "USD", "kc-fail")
    assert again["replayed"] is True
    assert again["payment"]["captured_amount"] == 3000


def test_gateway_ref_stable_across_replay():
    svc = PaymentService()
    r1 = svc.authorize(10, "USD", "kg")
    r2 = svc.authorize(10, "USD", "kg")
    assert r1["operation"]["gateway_ref"] == r2["payment"]["operations"][0]["gateway_ref"]
    logger.info("gateway ref stable on replay: %s",
                r1["operation"]["gateway_ref"])
