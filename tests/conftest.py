"""公共 fixtures：每个测试使用独立的临时 SQLite 文件库。"""

from __future__ import annotations

import logging
import pathlib
import tempfile

import pytest

from app.mock_gateway import MockGateway
from app.service import PaymentService
from app.storage import connect, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("tests")


@pytest.fixture()
def db_path() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="pledger-")) / "test.db"


@pytest.fixture()
def conn(db_path):
    c = connect(db_path)
    init_db(c)
    yield c
    c.close()


@pytest.fixture()
def service(conn):
    return PaymentService(conn, gateway=MockGateway())


def service_on(db_path: pathlib.Path, gateway: MockGateway | None = None) -> PaymentService:
    """在共享文件库上开一个全新连接（模拟另一个请求/进程）。"""
    c = connect(db_path)
    init_db(c)
    return PaymentService(c, gateway=gateway or MockGateway())
