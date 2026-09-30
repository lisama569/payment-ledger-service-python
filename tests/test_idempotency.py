"""幂等：重复提交不重复记账；同 key 不同内容必须明确拒绝。"""

from __future__ import annotations

import logging

import pytest

from app.errors import IdempotencyConflictError
from app.mock_gateway import MockGateway, MockGatewayTimeout

log = logging.getLogger("tests")


def test_repeated_capture_applied_once(service):
    service.authorize("p", 1000, "USD", "ka")
    first = service.capture("p", 600, "USD", "same-key")
    for _ in range(3):
        again = service.capture("p", 600, "USD", "same-key")
        assert again["replayed"] is True
        assert again["amount_captured"] == 600
        assert again["entries"] == first["entries"]
    view = service.get_payment("p")
    log.info("4x same capture key -> captured=%s (expected 600)", view["amount_captured"])
    assert view["amount_captured"] == 600
    n = service.conn.execute(
        "SELECT COUNT(*) n FROM ledger_entries WHERE idempotency_key='same-key'"
    ).fetchone()["n"]
    assert n == 4  # 只有首次的一次捕获的 4 条分录


@pytest.mark.parametrize("field,new_value", [("amount", 601), ("currency", "EUR"),
                                             ("payment_id", "other")])
def test_same_key_different_body_rejected(service, field, new_value):
    service.authorize("p", 1000, "USD", "ka")
    service.authorize("other", 1000, "USD", "ka-other")
    service.capture("p", 600, "USD", "k")
    payload = {"payment_id": "p", "amount": 600, "currency": "USD", "idempotency_key": "k"}
    payload[field] = new_value
    with pytest.raises(IdempotencyConflictError) as ei:
        service.capture(payload["payment_id"], payload["amount"],
                        payload["currency"], payload["idempotency_key"])
    log.info("same key with changed %s -> %s", field, ei.value.code)
    assert service.get_payment("p")["amount_captured"] == 600


def test_idempotency_keys_scoped_per_request_but_globally_unique(service):
    service.authorize("p1", 1000, "USD", "ka1")
    service.authorize("p2", 1000, "USD", "ka2")
    # 同一个 key 字符串不能被另一个支付复用
    with pytest.raises(IdempotencyConflictError) as ei:
        service.capture("p2", 100, "USD", "ka1")
    log.info("reuse key across payment -> %s", ei.value.code)


def test_retry_after_gateway_timeout_does_not_double_post(db_path):
    from tests.conftest import service_on

    svc = service_on(db_path)
    svc.authorize("p", 1000, "USD", "ka")
    svc.conn.close()

    flaky = service_on(db_path, MockGateway(script=["raise:timeout"]))
    with pytest.raises(MockGatewayTimeout):
        flaky.capture("p", 600, "USD", "kc")
    flaky.conn.close()
    log.info("attempt 1 failed with gateway timeout; expect zero residual records")

    c = connect_rowcount(db_path)
    assert c == 0  # 无 started 残留、无分录

    retry = service_on(db_path)
    r = retry.capture("p", 600, "USD", "kc")  # 同 key 重试
    log.info("retry -> captured=%s replayed=%s (expected 600/False)",
             r["amount_captured"], r["replayed"])
    assert r["amount_captured"] == 600 and r["replayed"] is False
    retry.conn.close()


def connect_rowcount(db_path):
    from app.storage import connect
    c = connect(db_path)
    n = c.execute(
        "SELECT (SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key='kc') "
        "+ (SELECT COUNT(*) FROM ledger_entries WHERE idempotency_key='kc') AS n"
    ).fetchone()["n"]
    c.close()
    return n


def test_declined_gateway_leaves_everything_untouched(db_path):
    from tests.conftest import service_on
    from app.mock_gateway import MockGatewayDeclined

    svc = service_on(db_path)
    svc.authorize("p", 1000, "USD", "ka")
    svc.capture("p", 500, "USD", "kc")
    svc.conn.close()
    bad = service_on(db_path, MockGateway(script=["raise:declined"]))
    with pytest.raises(MockGatewayDeclined):
        bad.refund("p", 50, "USD", "kr")
    bad.conn.close()
    svc = service_on(db_path)
    view = svc.get_payment("p")
    log.info("after declined refund captured=%s refunded=%s (expected 500/0)",
             view["amount_captured"], view["amount_refunded"])
    assert view["amount_captured"] == 500 and view["amount_refunded"] == 0
    svc.conn.close()
