"""Domain errors. Each rejection has a distinct, stable error code."""
from __future__ import annotations

import enum


class ErrorCode(str, enum.Enum):
    PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    CAPTURE_EXCEEDS_AUTHORIZED = "CAPTURE_EXCEEDS_AUTHORIZED"
    CAPTURE_AMOUNT_NOT_POSITIVE = "CAPTURE_AMOUNT_NOT_POSITIVE"
    REFUND_EXCEEDS_REFUNDABLE = "REFUND_EXCEEDS_REFUNDABLE"
    REFUND_AMOUNT_NOT_POSITIVE = "REFUND_AMOUNT_NOT_POSITIVE"
    NOTHING_TO_VOID = "NOTHING_TO_VOID"
    ALREADY_VOIDED = "ALREADY_VOIDED"
    ALREADY_CAPTURED = "ALREADY_CAPTURED"
    PAYMENT_FULLY_SETTLED = "PAYMENT_FULLY_SETTLED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    GATEWAY_FAILURE = "GATEWAY_FAILURE"


class DomainError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, payment_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.payment_id = payment_id

    def to_dict(self) -> dict:
        d = {"error_code": self.code.value, "message": self.message}
        if self.payment_id is not None:
            d["payment_id"] = self.payment_id
        return d
