"""骨架工程原有能力不回归。"""

from fastapi.testclient import TestClient

from app.api import create_app


def test_original_root_endpoint_still_works():
    client = TestClient(create_app(db_path=":memory:"))
    r = client.get("/")
    assert r.status_code == 200
    assert r.json() == {"Hello": "World"}


def test_health_endpoint():
    client = TestClient(create_app(db_path=":memory:"))
    assert client.get("/health").json() == {"status": "ok"}
