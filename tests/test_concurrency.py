"""Concurrent duplicate submissions must never double-book."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.service import PaymentService

logger = logging.getLogger(__name__)


def _run_concurrent(fn, n: int):
    """Call fn() from n threads released simultaneously by a barrier."""
    barrier = threading.Barrier(n)
    results: list = []
    errors: list[Exception] = []

    def worker():
        barrier.wait()
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - we assert on what was captured
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(worker) for _ in range(n)]
        for f in futures:
            f.result()
    return results, errors


def test_concurrent_capture_same_key_books_once():
    svc = PaymentService()
    pid = svc.authorize(100, "USD", "ka")["payment"]["payment_id"]

    results, errors = _run_concurrent(
        lambda: svc.capture(pid, 30, "USD", "kc-same"), 16)
    logger.info("concurrent capture: %s ok, %s errors", len(results), len(errors))
    assert errors == []
    assert len(results) == 16
    firsts = [r for r in results if r["replayed"] is False]
    replays = [r for r in results if r["replayed"] is True]
    assert len(firsts) == 1
    assert len(replays) == 15
    p = svc.get_payment(pid)["payment"]
    assert p["captured_amount"] == 3000
    assert len([o for o in p["operations"] if o["type"] == "CAPTURE"]) == 1
    # ledger stays balanced
    assert p["account_balances"]["CUSTOMER_CLEARING"] == 3000
    logger.info("captured_total=%s ledger=%s (no double booking)",
                p["captured_amount"], p["account_balances"])


def test_concurrent_refund_same_key_deducts_refundable_once():
    svc = PaymentService()
    pid = svc.authorize(100, "USD", "ka")["payment"]["payment_id"]
    svc.capture(pid, 100, "USD", "kc")

    results, errors = _run_concurrent(
        lambda: svc.refund(pid, 25, "USD", "kr-same"), 16)
    logger.info("concurrent refund: %s ok, %s errors", len(results), len(errors))
    assert errors == []
    assert len([r for r in results if r["replayed"] is False]) == 1
    p = svc.get_payment(pid)["payment"]
    assert p["refunded_amount"] == 2500
    assert p["refundable_amount"] == 7500
    assert len([o for o in p["operations"] if o["type"] == "REFUND"]) == 1
    assert p["account_balances"]["CUSTOMER_CLEARING"] == 7500
    assert p["account_balances"]["REFUND_CLEARING"] == 2500


def test_concurrent_distinct_partial_captures_never_exceed_authorization():
    svc = PaymentService()
    pid = svc.authorize(100, "USD", "ka")["payment"]["payment_id"]

    # 8 concurrent captures of 20 each against a 100 auth: exactly 5 can win.
    def make(i):
        return lambda: svc.capture(pid, 20, "USD", f"kc-{i}")

    tasks = [make(i) for i in range(8)]
    barrier = threading.Barrier(len(tasks))
    accepted: list = []
    rejected: list[str] = []

    def worker(task):
        barrier.wait()
        try:
            accepted.append(task())
        except Exception as exc:
            rejected.append(exc.code.value)  # type: ignore[attr-defined]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker, t) for t in tasks]
        for f in futures:
            f.result()

    logger.info("distinct concurrent captures accepted=%s rejected=%s (%s)",
                len(accepted), len(rejected), rejected)
    assert len(accepted) == 5
    assert len(rejected) == 3
    # Either the payment reads as already fully settled, or the capture amount
    # exceeds the remaining uncaptured slice: both are legal, distinct rejections.
    assert all(code in ("CAPTURE_EXCEEDS_AUTHORIZED", "PAYMENT_FULLY_SETTLED")
               for code in rejected)
    p = svc.get_payment(pid)["payment"]
    assert p["captured_amount"] == 10000
    assert p["status"] == "CAPTURED"
    assert p["account_balances"]["CUSTOMER_CLEARING"] == 10000
    assert p["account_balances"]["AUTHORIZED_FUNDS"] == 0


def test_concurrent_void_and_capture_only_one_wins():
    svc = PaymentService()
    pid = svc.authorize(50, "USD", "ka")["payment"]["payment_id"]
    barrier = threading.Barrier(2)
    outcomes = {"voided": 0, "captured": 0, "rejected": []}

    def do_void():
        barrier.wait()
        try:
            svc.void(pid, "kv")
            outcomes["voided"] += 1
        except Exception as exc:  # noqa: BLE001
            outcomes["rejected"].append(("void", exc.code.value))  # type: ignore[attr-defined]

    def do_capture():
        barrier.wait()
        try:
            svc.capture(pid, 50, "USD", "kc")
            outcomes["captured"] += 1
        except Exception as exc:  # noqa: BLE001
            outcomes["rejected"].append(("capture", exc.code.value))  # type: ignore[attr-defined]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(do_void)
        f2 = pool.submit(do_capture)
        f1.result(); f2.result()

    logger.info("void vs capture race -> %s", outcomes)
    assert outcomes["voided"] + outcomes["captured"] == 1
    assert len(outcomes["rejected"]) == 1
    p = svc.get_payment(pid)["payment"]
    if outcomes["voided"]:
        assert p["status"] == "VOIDED"
        assert p["captured_amount"] == 0
        assert p["account_balances"]["AUTHORIZED_FUNDS"] == 0
    else:
        assert p["status"] == "CAPTURED"
        assert p["captured_amount"] == 5000
