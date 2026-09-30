"""Randomized partial capture/refund sequences must never desynchronize the books."""
import logging
import random

from app.errors import DomainError, ErrorCode
from app.service import PaymentService

logger = logging.getLogger(__name__)


def test_random_capture_refund_sequence_always_consistent():
    rng = random.Random(20260930)
    svc = PaymentService()

    for trial in range(30):
        authorized = rng.randint(1, 20) * 100  # minor-units granularity via 1.00 USD steps
        key = f"trial-{trial}"
        pid = svc.authorize(authorized / 100, "USD", f"{key}-a")["payment"]["payment_id"]
        captured = 0
        refunded = 0

        for step in range(40):
            remaining_auth = authorized - captured
            refundable = captured - refunded
            choice = rng.choice(["capture", "refund", "bad-capture", "bad-refund",
                                  "capture-after-refund"])

            if choice == "capture" and remaining_auth > 0:
                if refunded > 0:
                    # Once any refund exists, captures are closed forever.
                    try:
                        svc.capture(pid, 1.00, "USD", f"{key}-ca{step}")
                        raise AssertionError("capture after refund must be rejected")
                    except DomainError as exc:
                        assert exc.code is ErrorCode.INVALID_STATE_TRANSITION
                    p = svc.get_payment(pid)["payment"]
                    log = "capture after refund refused"
                else:
                    amt = rng.randint(1, max(1, remaining_auth // 100)) * 100
                    amt = min(amt, remaining_auth)
                    if amt <= 0:
                        continue
                    p = svc.capture(pid, amt / 100, "USD", f"{key}-c{step}")["payment"]
                    captured += amt
                    log = f"capture {amt}"
            elif choice == "refund" and refundable > 0:
                amt = rng.randint(1, max(1, refundable // 100)) * 100
                amt = min(amt, refundable)
                if amt <= 0:
                    continue
                p = svc.refund(pid, amt / 100, "USD", f"{key}-r{step}")["payment"]
                refunded += amt
                log = f"refund {amt}"
            elif choice == "bad-capture" and remaining_auth > 0:
                amt = remaining_auth + 100
                try:
                    svc.capture(pid, amt / 100, "USD", f"{key}-bc{step}")
                    raise AssertionError("over-capture must be rejected")
                except DomainError as exc:
                    assert exc.code in (ErrorCode.CAPTURE_EXCEEDS_AUTHORIZED,
                                        ErrorCode.PAYMENT_FULLY_SETTLED,
                                        ErrorCode.INVALID_STATE_TRANSITION)
                p = svc.get_payment(pid)["payment"]
                log = f"bad-capture {amt} rejected"
            elif choice == "bad-refund":
                amt = refundable + 100  # always strictly above refundable, and >= 1.00
                try:
                    svc.refund(pid, amt / 100, "USD", f"{key}-br{step}")
                    raise AssertionError("over-refund must be rejected")
                except DomainError as exc:
                    assert exc.code is ErrorCode.REFUND_EXCEEDS_REFUNDABLE
                p = svc.get_payment(pid)["payment"]
                log = f"bad-refund rejected (refundable={refundable})"
            else:
                # capture attempt after refunds must be refused, not silently applied
                if refunded > 0 and remaining_auth > 0:
                    try:
                        svc.capture(pid, 100 / 100, "USD", f"{key}-car{step}")
                        raise AssertionError("capture after refund must be rejected")
                    except DomainError as exc:
                        assert exc.code is ErrorCode.INVALID_STATE_TRANSITION
                p = svc.get_payment(pid)["payment"]
                log = "post-refund capture refused"

            # Invariants after every single step:
            assert p["captured_amount"] == captured
            assert p["refunded_amount"] == refunded
            assert p["refundable_amount"] == captured - refunded
            bal = p["account_balances"]
            assert bal.get("CUSTOMER_CLEARING", 0) == captured - refunded
            assert bal.get("MERCHANT_PAYABLE", 0) == -captured
            assert bal.get("REFUND_CLEARING", 0) == refunded
            assert bal.get("AUTHORIZED_FUNDS", 0) == authorized - captured
            for op in p["operations"]:
                assert sum(e["debit"] for e in op["entries"]) == \
                       sum(e["credit"] for e in op["entries"])

        logger.info("trial %2d: auth=%d captured=%d refunded=%d status=%s | last=%s",
                    trial, authorized, captured, refunded, p["status"], log)
