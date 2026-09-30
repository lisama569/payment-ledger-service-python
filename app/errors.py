"""业务错误类型。

每一种被拒绝的情形都有独立的错误码（见每个子类的 ``code``），
调用方可以据此区分“为什么被拒绝”，而不会被静默改写状态。
"""

from __future__ import annotations


class PaymentError(Exception):
    """所有可预期业务拒绝的基类。"""

    code: str = "payment_error"

    def __init__(self, message: str, *, payment_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.payment_id = payment_id


class PaymentNotFoundError(PaymentError):
    code = "payment_not_found"


class InvalidAmountError(PaymentError):
    code = "invalid_amount"


class CurrencyMismatchError(PaymentError):
    code = "currency_mismatch"


class IdempotencyConflictError(PaymentError):
    code = "idempotency_conflict"


class PaymentNotAuthorizedError(PaymentError):
    code = "payment_not_authorized"


class CaptureExceedsAuthorizedError(PaymentError):
    code = "capture_exceeds_authorized"


class VoidNotAllowedError(PaymentError):
    code = "void_not_allowed"


class RefundExceedsCapturedError(PaymentError):
    code = "refund_exceeds_captured"


class RefundExceedsRefundableError(PaymentError):
    code = "refund_exceeds_refundable"


class PaymentAlreadyExistsError(PaymentError):
    code = "payment_already_exists"


class CaptureNotAllowedError(PaymentError):
    code = "capture_not_allowed"
