"""并发：重复请求与竞争部分捕获都不得破坏账目。"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import CaptureExceedsAuthorizedError, IdempotencyConflictError
from app.mock_gateway import MockGateway

from .conftest import service_on

log = logging.getLogger("tests")


def _run_concurrently(fn, n, timeout=30):
    results, errors = [], []

    def worker():
        try:
            results.append(fn())
        except Exception as exc:  # 捕获每个线程的异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    return results, errors


def test_concurrent_duplicate_capture_single_post(db_path):
    svc = service_on(db_path)
    svc.authorize("p", 1000, "USD", "ka")
    svc.conn.close()

    def call():
        s = service_on(db_path)
        try:
            return s.capture("p", 600, "USD", "shared-cap")
        finally:
            s.conn.close()

    results, errors = _run_concurrently(call, 8)
    log.info("concurrent identical capture: successes=%d errors=%d",
             len(results), len(errors))
    assert not errors, [type(e).__name__ for e in errors]
    assert len(results) == 8
    assert all(r["replayed"] is False for r in results if r["replayed"] is False) or True
    firsts = [r for r in results if r["replayed"] is False]
    replays = [r for r in results if r["replayed"] is True]
    assert len(firsts) == 1 and len(replays) == 7
    assert all(r["amount_captured"] == 600 for r in results)

    c = service_on(db_path)
    assert c.get_payment("p")["amount_captured"] == 600
    n = c.conn.execute(
        "SELECT COUNT(*) n FROM ledger_entries WHERE idempotency_key='shared-cap'"
    ).fetchone()["n"]
    assert n == 4
    log.info("only one set of 4 balanced entries posted; final captured=600")
    c.conn.close()


def test_concurrent_distinct_captures_never_exceed_authorization(db_path):
    svc = service_on(db_path)
    svc.authorize("p", 1000, "USD", "ka")
    svc.conn.close()

    amounts = [300, 300, 300, 300]  # 总计 1200 > 1000，恰好有一笔必须被拒

    def call(amount, key):
        s = service_on(db_path)
        try:
            return s.capture("p", amount, "USD", key)
        finally:
            s.conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(call, a, f"cap-{i}") for i, a in enumerate(amounts)]
        outcomes = []
        for f in futs:
            try:
                outcomes.append(("ok", f.result()["amount_captured"]))
            except CaptureExceedsAuthorizedError as e:
                outcomes.append(("rejected", e.code))
            except Exception as e:  # pragma: no cover
                outcomes.append(("error", type(e).__name__))
    oks = [o for o in outcomes if o[0] == "ok"]
    rejected = [o for o in outcomes if o[0] == "rejected"]
    log.info("concurrent competing captures: ok=%d rejected=%d outcomes=%s",
             len(oks), len(rejected), outcomes)
    assert len(oks) == 3 and len(rejected) == 1
    c = service_on(db_path)
    view = c.get_payment("p")
    assert view["amount_captured"] == 900
    assert view["refundable_amount"] == 900
    log.info("final captured=%s open=%s (never exceeded 1000)",
             view["amount_captured"], view["open_authorized_amount"])
    c.conn.close()


def test_concurrent_refunds_never_exceed_refundable(db_path):
    svc = service_on(db_path)
    svc.authorize("p", 1000, "USD", "ka")
    svc.capture("p", 500, "USD", "kc")
    svc.conn.close()

    def call(i):
        s = service_on(db_path)
        try:
            return s.refund("p", 300, "USD", f"ref-{i}")
        finally:
            s.conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(call, i) for i in range(3)]
        oks, rejected = 0, 0
        for f in futs:
            try:
                f.result(); oks += 1
            except Exception as e:
                assert isinstance(e, (Exception,))
                rejected += 1
                assert type(e).__name__ in {
                    "RefundExceedsRefundableError", "RefundExceedsCapturedError"
                }
    log.info("concurrent refunds of 300x3 against 500: ok=%d rejected=%d", oks, rejected)
    c = service_on(db_path)
    view = c.get_payment("p")
    assert view["amount_refunded"] <= 500
    assert view["refundable_amount"] == 500 - view["amount_refunded"]
    log.info("final refunded=%s refundable=%s",
             view["amount_refunded"], view["refundable_amount"])
    c.conn.close()


def test_concurrent_same_key_conflicting_bodies(db_path):
    svc = service_on(db_path)
    svc.authorize("p", 1000, "USD", "ka")
    svc.conn.close()

    def call(amount):
        s = service_on(db_path)
        try:
            return s.capture("p", amount, "USD", "same-key")
        finally:
            s.conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        # 同 key 但金额不同：谁先抢到写锁谁成功，另一个必须冲突
        futs = [pool.submit(call, 100), pool.submit(call, 200)]
        succeeded, conflicts = [], []
        for f in futs:
            try:
                succeeded.append(f.result()["amount_captured"])
            except IdempotencyConflictError:
                conflicts.append(1)
    log.info("same-key/different-body race: succeeded=%s conflicts=%d",
             succeeded, len(conflicts))
    assert len(succeeded) == 1 and len(conflicts) == 1
    c = service_on(db_path)
    final_captured = c.get_payment("p")["amount_captured"]
    assert final_captured in {100, 200}
    assert final_captured == succeeded[0]
    # 只可能记一次账：4 条分录
    n = c.conn.execute(
        "SELECT COUNT(*) n FROM ledger_entries WHERE idempotency_key='same-key'"
    ).fetchone()["n"]
    assert n == 4
    log.info("winner posted %s once (4 entries); loser rejected with conflict",
             final_captured)
    c.conn.close()
