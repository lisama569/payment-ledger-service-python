"""API 请求/响应模型。

金额一律使用以“最小货币单位（minor unit）”计的正整数（例如 1234 表示 12.34，
具体小数位由币种决定），服务内部不做币种换算、不接受浮点金额。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .errors import PaymentError


class AuthorizeRequest(BaseModel):
    payment_id: str = Field(min_length=1)
    amount: int = Field(gt=0, description="授权金额，最小货币单位整数")
    currency: str = Field(min_length=3, max_length=3)
    idempotency_key: str = Field(min_length=1)


class CaptureRequest(BaseModel):
    payment_id: str = Field(min_length=1)
    amount: int = Field(gt=0, description="本次捕获金额")
    currency: str = Field(min_length=3, max_length=3)
    idempotency_key: str = Field(min_length=1)


class RefundRequest(BaseModel):
    payment_id: str = Field(min_length=1)
    amount: int = Field(gt=0, description="本次退款金额")
    currency: str = Field(min_length=3, max_length=3)
    idempotency_key: str = Field(min_length=1)


class VoidRequest(BaseModel):
    payment_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class PaymentView(BaseModel):
    payment_id: str
    status: str
    currency: str
    amount_authorized: int
    amount_captured: int
    amount_refunded: int
    amount_voided: int
    refundable_amount: int
    open_authorized_amount: int


class EntryView(BaseModel):
    account: str
    debit: int
    credit: int


class ActionResult(BaseModel):
    """一次成功（或幂等重放）动作的返回结果。"""

    payment_id: str
    action: str
    status: str
    replayed: bool
    amount: int
    currency: str
    amount_authorized: int
    amount_captured: int
    amount_refunded: int
    amount_voided: int
    refundable_amount: int
    open_authorized_amount: int
    entries: list[EntryView] = Field(default_factory=list)


class ErrorBody(BaseModel):
    error: dict


def error_body(exc: PaymentError) -> dict:
    return {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "payment_id": exc.payment_id,
        }
    }
