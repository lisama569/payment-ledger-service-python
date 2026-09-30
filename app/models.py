"""Domain enums and data models."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class PaymentStatus(str, enum.Enum):
    AUTHORIZED = "AUTHORIZED"                  # authorized, nothing captured yet
    PARTIALLY_CAPTURED = "PARTIALLY_CAPTURED"  # 0 < captured < authorized
    CAPTURED = "CAPTURED"                      # captured == authorized, no refund
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"  # 0 < refunded < captured
    REFUNDED = "REFUNDED"                      # refunded == captured > 0
    VOIDED = "VOIDED"                          # auth voided, nothing captured
    FAILED = "FAILED"                          # authorization rejected by channel


class OperationType(str, enum.Enum):
    AUTHORIZE = "AUTHORIZE"
    CAPTURE = "CAPTURE"
    REFUND = "REFUND"
    VOID = "VOID"


@dataclass
class Entry:
    """One double-entry leg."""
    account: str
    debit: int
    credit: int


@dataclass
class Operation:
    operation_id: str
    payment_id: str
    op_type: OperationType
    amount: int  # minor units; 0 for VOID
    currency: str
    idempotency_key: str
    gateway_ref: str | None = None
    entries: list[Entry] = field(default_factory=list)
    created_at_seq: int = 0


@dataclass
class Payment:
    payment_id: str
    currency: str
    authorized_amount: int = 0
    captured_amount: int = 0
    refunded_amount: int = 0
    status: PaymentStatus = PaymentStatus.AUTHORIZED
    operations: list[Operation] = field(default_factory=list)

    @property
    def refundable_amount(self) -> int:
        """Captured funds not yet refunded. Refunds can never exceed this."""
        return self.captured_amount - self.refunded_amount
