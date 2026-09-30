"""Smoke tests: the app object the original bootstrap exposed still builds."""
from fastapi.testclient import TestClient

import main
from app.api import create_app
from app.service import PaymentService


def test_module_app_exists_and_serves_health():
    # main.py exposes `app`, just like the original bootstrap
    assert main.app is not None
    client = TestClient(main.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_service_constructible_with_defaults():
    svc = PaymentService()
    assert svc.store is not None
    assert svc.gateway is not None
    assert create_app(svc) is not None
