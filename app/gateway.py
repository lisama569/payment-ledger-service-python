"""Simulated payment channel.

No network, no credentials. It models the two properties a real acquirer
exposes that the ledger relies on:

* a call can fail transiently (``fail_once`` test hook);
* retrying the *same client operation id* is recognized and never applied
  twice at the channel (idempotency at the external side).
"""
from __future__ import annotations

import threading

from .errors import DomainError, ErrorCode
from .money import format_money


class MockGateway:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processed: dict[str, str] = {}
        self._seq = 0
        # Test hook: operation ids that must fail exactly once, then succeed.
        self.fail_once: set[str] = set()

    def _next_ref(self) -> str:
        self._seq += 1
        return f"GW-{self._seq:010d}"

    def _process(self, op_id: str, label: str, detail: str) -> str:
        with self._lock:
            existing = self._processed.get(op_id)
            if existing is not None:
                # Duplicate delivery of the same client operation: replay ref.
                return existing
            if op_id in self.fail_once:
                self.fail_once.discard(op_id)
                raise DomainError(
                    ErrorCode.GATEWAY_FAILURE,
                    f"simulated channel failure during {label} ({detail})",
                )
            ref = self._next_ref()
            self._processed[op_id] = ref
            return ref

    def authorize(self, op_id: str, payment_id: str, amount: int, currency: str) -> str:
        detail = f"{format_money(amount, currency)} {currency} for {payment_id}"
        return self._process(op_id, "AUTHORIZE", detail)

    def capture(self, op_id: str, payment_id: str, amount: int, currency: str) -> str:
        detail = f"{format_money(amount, currency)} {currency} for {payment_id}"
        return self._process(op_id, "CAPTURE", detail)

    def refund(self, op_id: str, payment_id: str, amount: int, currency: str) -> str:
        detail = f"{format_money(amount, currency)} {currency} for {payment_id}"
        return self._process(op_id, "REFUND", detail)

    def void(self, op_id: str, payment_id: str) -> str:
        return self._process(op_id, "VOID", payment_id)
