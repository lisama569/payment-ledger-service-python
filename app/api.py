"""HTTP 路由与错误映射。

写接口：
  POST /payments/authorize
  POST /payments/capture
  POST /payments/refund
  POST /payments/void
  GET  /payments/{payment_id}

每个请求使用独立连接；写事务以 BEGIN IMMEDIATE 开始，因此并发请求在
SQLite 层被串行化，配合 idempotency_records 的 UNIQUE 约束保证
并发重复提交只记账一次。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import (
    CurrencyMismatchError,
    InvalidAmountError,
    PaymentError,
    PaymentNotFoundError,
)
from .mock_gateway import MockGatewayError
from .models import (
    ActionResult,
    AuthorizeRequest,
    CaptureRequest,
    PaymentView,
    RefundRequest,
    VoidRequest,
    error_body,
)
from .service import PaymentService
from .storage import DEFAULT_DB_PATH, connect, init_db

logger = logging.getLogger("payment_ledger")

def create_app(
    db_path: str | Path | None = None,
    service_factory: Callable[[], PaymentService] | None = None,
) -> FastAPI:
    db_path = db_path if db_path is not None else DEFAULT_DB_PATH

    def make_service() -> PaymentService:
        if service_factory is not None:
            return service_factory()
        conn = connect(db_path)
        init_db(conn)
        return PaymentService(conn)

    app = FastAPI(title="payment-ledger-service", version="0.1.0")
    app.state.make_service = make_service

    def service() -> PaymentService:
        svc = make_service()
        try:
            yield svc
        finally:
            svc.conn.close()

    @app.exception_handler(PaymentError)
    async def payment_error_handler(request: Request, exc: PaymentError) -> JSONResponse:
        logger.info(
            "business rejection path=%s code=%s payment_id=%s message=%s",
            request.url.path,
            exc.code,
            exc.payment_id,
            exc.message,
        )
        if isinstance(exc, PaymentNotFoundError):
            status_code = 404
        elif isinstance(exc, (InvalidAmountError, CurrencyMismatchError)):
            status_code = 422
        else:
            status_code = 409
        return JSONResponse(status_code=status_code, content=error_body(exc))

    @app.exception_handler(MockGatewayError)
    async def gateway_error_handler(request: Request, exc: MockGatewayError) -> JSONResponse:
        logger.warning(
            "gateway failure path=%s kind=%s message=%s",
            request.url.path,
            exc.kind,
            exc,
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": f"gateway_{exc.kind}",
                    "message": str(exc),
                    "payment_id": None,
                }
            },
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/")
    def read_root() -> dict:
        # 保留骨架工程原有端点，避免回归
        return {"Hello": "World"}

    @app.post("/payments/authorize", response_model=ActionResult)
    def authorize(req: AuthorizeRequest, svc: PaymentService = Depends(service)) -> dict:
        logger.info("authorize input payment_id=%s amount=%s currency=%s key=%s",
                    req.payment_id, req.amount, req.currency, req.idempotency_key)
        result = svc.authorize(
            req.payment_id, req.amount, req.currency, req.idempotency_key
        )
        logger.info("authorize result payment_id=%s status=%s replayed=%s",
                    result["payment_id"], result["status"], result["replayed"])
        return result

    @app.post("/payments/capture", response_model=ActionResult)
    def capture(req: CaptureRequest, svc: PaymentService = Depends(service)) -> dict:
        logger.info("capture input payment_id=%s amount=%s currency=%s key=%s",
                    req.payment_id, req.amount, req.currency, req.idempotency_key)
        result = svc.capture(
            req.payment_id, req.amount, req.currency, req.idempotency_key
        )
        logger.info("capture result payment_id=%s status=%s captured=%s replayed=%s",
                    result["payment_id"], result["status"],
                    result["amount_captured"], result["replayed"])
        return result

    @app.post("/payments/refund", response_model=ActionResult)
    def refund(req: RefundRequest, svc: PaymentService = Depends(service)) -> dict:
        logger.info("refund input payment_id=%s amount=%s currency=%s key=%s",
                    req.payment_id, req.amount, req.currency, req.idempotency_key)
        result = svc.refund(
            req.payment_id, req.amount, req.currency, req.idempotency_key
        )
        logger.info("refund result payment_id=%s status=%s refunded=%s replayed=%s",
                    result["payment_id"], result["status"],
                    result["amount_refunded"], result["replayed"])
        return result

    @app.post("/payments/void", response_model=ActionResult)
    def void(req: VoidRequest, svc: PaymentService = Depends(service)) -> dict:
        logger.info("void input payment_id=%s key=%s", req.payment_id, req.idempotency_key)
        result = svc.void(req.payment_id, req.idempotency_key)
        logger.info("void result payment_id=%s status=%s voided=%s replayed=%s",
                    result["payment_id"], result["status"],
                    result["amount_voided"], result["replayed"])
        return result

    @app.get("/payments/{payment_id}", response_model=PaymentView)
    def get_payment(payment_id: str, svc: PaymentService = Depends(service)) -> dict:
        view = svc.get_payment(payment_id)
        view.pop("entries", None)  # 列表视图不含分录
        return view

    return app
