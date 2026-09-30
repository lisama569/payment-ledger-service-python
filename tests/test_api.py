"""End-to-end HTTP tests."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.api import create_app

logger = logging.getLogger(__name__)


def client():
    return TestClient(create_app())


def test_root_flow_over_http():
    c = client()
    r = c.post("/v1/payments/authorize",
               json={"amount": "100.00", "currency": "usd", "idempotency_key": "A1"})
    assert r.status_code == 200, r.text
    pid = r.json()["payment"]["payment_id"]
    logger.info("HTTP authorize -> %s status=%s", pid, r.json()["payment"]["status"])

    r = c.post(f"/v1/payments/{pid}/capture",
               json={"amount": "60.00", "currency": "USD", "idempotency_key": "C1"})
    assert r.status_code == 200
    assert r.json()["payment"]["status"] == "PARTIALLY_CAPTURED"

    r = c.post(f"/v1/payments/{pid}/refund",
               json={"amount": "20.00", "currency": "USD", "idempotency_key": "R1"})
    assert r.status_code == 200
    body = r.json()["payment"]
    logger.info("HTTP refund 20 -> refunded=%s refundable=%s",
                body["refunded_amount"], body["refundable_amount"])
    assert body["refundable_amount"] == 4000

    g = c.get(f"/v1/payments/{pid}")
    assert g.status_code == 200
    assert g.json()["payment"]["account_balances"]["CUSTOMER_CLEARING"] == 4000


def test_illegal_transitions_have_distinct_codes():
    c = client()
    pid = c.post("/v1/payments/authorize",
                 json={"amount": 10, "currency": "USD", "idempotency_key": "A"})
    pid = pid.json()["payment"]["payment_id"]

    def post(url, payload):
        r = c.post(url, json=payload)
        logger.info("POST %s payload=%s -> HTTP %s body=%s",
                    url, payload, r.status_code, r.json())
        return r

    r = post(f"/v1/payments/{pid}/capture",
             {"amount": 11, "currency": "USD", "idempotency_key": "X"})
    assert r.status_code == 422
    assert r.json()["error_code"] == "CAPTURE_EXCEEDS_AUTHORIZED"

    c.post(f"/v1/payments/{pid}/void", json={"idempotency_key": "V"})
    r = post(f"/v1/payments/{pid}/capture",
             {"amount": 1, "currency": "USD", "idempotency_key": "Y"})
    assert r.status_code == 409
    assert r.json()["error_code"] == "ALREADY_VOIDED"

    r = post(f"/v1/payments/{pid}/refund",
             {"amount": 1, "currency": "USD", "idempotency_key": "Z"})
    assert r.json()["error_code"] == "ALREADY_VOIDED"

    r = c.get("/v1/payments/nope")
    assert r.status_code == 404
    assert r.json()["error_code"] == "PAYMENT_NOT_FOUND"
    logger.info("distinct error codes verified")


def test_idempotency_conflict_is_409():
    c = client()
    c.post("/v1/payments/authorize",
           json={"amount": 10, "currency": "USD", "idempotency_key": "K"})
    r = c.post("/v1/payments/authorize",
               json={"amount": 11, "currency": "USD", "idempotency_key": "K"})
    assert r.status_code == 409
    assert r.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
    logger.info("same key/different body -> 409 %s", r.json()["error_code"])


def test_http_concurrent_duplicate_capture_books_once():
    c = client()
    pid = c.post("/v1/payments/authorize",
                 json={"amount": 100, "currency": "USD", "idempotency_key": "A"}
                 ).json()["payment"]["payment_id"]
    barrier = threading.Barrier(12)
    payload = {"amount": 40, "currency": "USD", "idempotency_key": "CC"}

    def fire():
        barrier.wait()
        return c.post(f"/v1/payments/{pid}/capture", json=payload)

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = [f.result() for f in [pool.submit(fire) for _ in range(12)]]

    assert all(r.status_code == 200 for r in responses)
    firsts = [r for r in responses if r.json()["replayed"] is False]
    assert len(firsts) == 1
    p = c.get(f"/v1/payments/{pid}").json()["payment"]
    logger.info("12 concurrent identical captures -> captured=%s ops=%s",
                p["captured_amount"], len(p["operations"]))
    assert p["captured_amount"] == 4000
    assert len([o for o in p["operations"] if o["type"] == "CAPTURE"]) == 1
