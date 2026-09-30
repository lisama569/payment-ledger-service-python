"""In-memory storage.

A single re-entrant lock (``store.lock``) serializes every state-changing
operation, which is what makes concurrent duplicate submissions safe:
"check idempotency key -> validate -> call gateway -> append entries ->
store response" runs as one critical section.
"""
from __future__ import annotations

import threading

from .models import Payment


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._payments: dict[str, Payment] = {}
        # idempotency_key -> (request fingerprint, cached response dict)
        self._idempotency: dict[str, tuple[str, dict]] = {}

    def get_payment(self, payment_id: str) -> Payment | None:
        return self._payments.get(payment_id)

    def put_payment(self, payment: Payment) -> None:
        self._payments[payment.payment_id] = payment

    def find_idempotent(self, key: str) -> tuple[str, dict] | None:
        return self._idempotency.get(key)

    def save_idempotent(self, key: str, fingerprint: str, response: dict) -> None:
        self._idempotency[key] = (fingerprint, response)
