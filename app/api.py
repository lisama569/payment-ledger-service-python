"""FastAPI HTTP layer."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import DomainError, ErrorCode
from .service import PaymentService

logger = logging.getLogger("payment.api")

# Status code per error code: client faults are 4xx, simulated channel faults 502.
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.PAYMENT_NOT_FOUND: 404,
    ErrorCode.INVALID_AMOUNT: 422,
    ErrorCode.CURRENCY_MISMATCH: 422,
    ErrorCode.INVALID_STATE_TRANSITION: 409,
    ErrorCode.CAPTURE_EXCEEDS_AUTHORIZED: 422,
    ErrorCode.CAPTURE_AMOUNT_NOT_POSITIVE: 422,
    ErrorCode.REFUND_EXCEEDS_REFUNDABLE: 422,
    ErrorCode.REFUND_AMOUNT_NOT_POSITIVE: 422,
    ErrorCode.NOTHING_TO_VOID: 409,
    ErrorCode.ALREADY_VOIDED: 409,
    ErrorCode.ALREADY_CAPTURED: 409,
    ErrorCode.PAYMENT_FULLY_SETTLED: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.GATEWAY_FAILURE: 502,
}


class AuthorizeBody(BaseModel):
    amount: float | int | str = Field(..., description="decimal amount in the currency")
    currency: str
    idempotency_key: str = Field(..., min_length=1)


class CaptureBody(BaseModel):
    amount: float | int | str
    currency: str
    idempotency_key: str = Field(..., min_length=1)


class RefundBody(BaseModel):
    amount: float | int | str
    currency: str
    idempotency_key: str = Field(..., min_length=1)


class VoidBody(BaseModel):
    idempotency_key: str = Field(..., min_length=1)


def create_app(service: PaymentService | None = None) -> FastAPI:
    svc = service or PaymentService()
    app = FastAPI(title="Payment Ledger Service (local simulation)")

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request, exc: DomainError):
        logger.info("rejected request code=%s message=%s", exc.code.value, exc.message)
        return JSONResponse(status_code=_HTTP_STATUS.get(exc.code, 400),
                            content=exc.to_dict())

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/payments/authorize")
    def authorize(body: AuthorizeBody):
        return svc.authorize(body.amount, body.currency, body.idempotency_key)

    @app.post("/v1/payments/{payment_id}/capture")
    def capture(payment_id: str, body: CaptureBody):
        return svc.capture(payment_id, body.amount, body.currency, body.idempotency_key)

    @app.post("/v1/payments/{payment_id}/refund")
    def refund(payment_id: str, body: RefundBody):
        return svc.refund(payment_id, body.amount, body.currency, body.idempotency_key)

    @app.post("/v1/payments/{payment_id}/void")
    def void(payment_id: str, body: VoidBody):
        return svc.void(payment_id, body.idempotency_key)

    @app.get("/v1/payments/{payment_id}")
    def get_payment(payment_id: str):
        return svc.get_payment(payment_id)

    return app
