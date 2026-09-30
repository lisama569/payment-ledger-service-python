"""HTTP 层：状态码映射、幂等重放、非法流转响应体可区分。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app

log = logging.getLogger("tests")


@pytest.fixture()
def client(db_path):
    return TestClient(create_app(db_path=db_path))


def auth(client, pid="p", amount=1000, ccy="USD", key="ka"):
    return client.post("/payments/authorize", json={
        "payment_id": pid, "amount": amount, "currency": ccy, "idempotency_key": key})


def test_authorize_and_get(client):
    r = auth(client)
    assert r.status_code == 200
    body = r.json()
    log.info("authorize -> %s", body["status"])
    assert body["status"] == "AUTHORIZED" and body["entries"]
    r = client.get("/payments/p")
    assert r.status_code == 200
    assert r.json()["open_authorized_amount"] == 1000


def test_duplicate_authorize_409_distinct_code(client):
    auth(client)
    r = auth(client, key="ka2")
    log.info("duplicate authorize http=%s code=%s",
             r.status_code, r.json()["error"]["code"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "payment_already_exists"


def test_not_found_404(client):
    r = client.get("/payments/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "payment_not_found"


def test_partial_capture_then_replay_over_http(client):
    auth(client)
    payload = {"payment_id": "p", "amount": 400, "currency": "USD",
               "idempotency_key": "kc"}
    r1 = client.post("/payments/capture", json=payload)
    r2 = client.post("/payments/capture", json=payload)
    log.info("capture first=%s replay=%s", r1.json()["replayed"], r2.json()["replayed"])
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["replayed"] is False
    assert r2.json()["replayed"] is True
    assert r2.json()["amount_captured"] == 400


def test_illegal_transitions_have_distinct_codes(client):
    auth(client)
    client.post("/payments/capture", json={
        "payment_id": "p", "amount": 600, "currency": "USD", "idempotency_key": "kc"})
    over_capture = client.post("/payments/capture", json={
        "payment_id": "p", "amount": 401, "currency": "USD", "idempotency_key": "kc2"})
    assert over_capture.status_code == 409
    assert over_capture.json()["error"]["code"] == "capture_exceeds_authorized"

    over_refund = client.post("/payments/refund", json={
        "payment_id": "p", "amount": 601, "currency": "USD", "idempotency_key": "kr"})
    assert over_refund.json()["error"]["code"] == "refund_exceeds_refundable"
    log.info("over-capture code=%s over-refund code=%s",
             over_capture.json()["error"]["code"],
             over_refund.json()["error"]["code"])

    # 再捕完剩余 400，然后撤销（已无开放授权）必须拒绝
    client.post("/payments/capture", json={
        "payment_id": "p", "amount": 400, "currency": "USD", "idempotency_key": "kc2b"})
    voided = client.post("/payments/void", json={"payment_id": "p", "idempotency_key": "kv"})
    assert voided.status_code == 409
    assert voided.json()["error"]["code"] == "void_not_allowed"
    log.info("void after full capture -> %s", voided.json()["error"]["code"])


def test_idempotency_conflict_409(client):
    auth(client)
    client.post("/payments/capture", json={
        "payment_id": "p", "amount": 100, "currency": "USD", "idempotency_key": "k"})
    r = client.post("/payments/capture", json={
        "payment_id": "p", "amount": 200, "currency": "USD", "idempotency_key": "k"})
    log.info("same key different amount -> http=%s code=%s",
             r.status_code, r.json()["error"]["code"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"


def test_currency_mismatch_422(client):
    auth(client)
    r = client.post("/payments/capture", json={
        "payment_id": "p", "amount": 10, "currency": "EUR", "idempotency_key": "k"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "currency_mismatch"


def test_void_full_flow(client):
    auth(client)
    r = client.post("/payments/void", json={"payment_id": "p", "idempotency_key": "kv"})
    assert r.status_code == 200
    body = r.json()
    log.info("void -> status=%s amount=%s", body["status"], body["amount"])
    assert body["status"] == "VOIDED" and body["amount"] == 1000
    again = client.post("/payments/void", json={"payment_id": "p", "idempotency_key": "kv"})
    assert again.json()["replayed"] is True
