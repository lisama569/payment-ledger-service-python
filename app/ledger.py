"""Balanced double-entry postings for each money-moving operation.

Every operation's entries balance (sum(debits) == sum(credits)). Memo
entries for the authorization net to zero; real captures increase clearing
assets and merchant payables; refunds reverse clearing via REFUND_CLEARING,
so the open CUSTOMER_CLEARING position always equals captured - refunded.
"""
from __future__ import annotations

from .errors import DomainError, ErrorCode
from .models import Entry, OperationType

# ---- Accounts -----------------------------------------------------------
# Memo pair recording the authorized-but-not-yet-captured funds:
ACC_AUTHORIZED_FUNDS = "AUTHORIZED_FUNDS"      # memo debit
ACC_AUTHORIZED_RESERVE = "AUTHORIZED_RESERVE"  # memo credit
# Real accounts:
ACC_CUSTOMER_CLEARING = "CUSTOMER_CLEARING"    # funds the channel owes for captures
ACC_MERCHANT_PAYABLE = "MERCHANT_PAYABLE"      # what will be settled to the merchant
ACC_REFUND_CLEARING = "REFUND_CLEARING"        # refunds reversing captured funds


def build_entries(op_type: OperationType, amount: int) -> list[Entry]:
    """Return balanced entries for a successful money-moving operation."""
    if op_type is OperationType.AUTHORIZE:
        # Off-balance-sheet memo: money promised by the issuer.
        return [
            Entry(ACC_AUTHORIZED_FUNDS, debit=amount, credit=0),
            Entry(ACC_AUTHORIZED_RESERVE, debit=0, credit=amount),
        ]
    if op_type is OperationType.CAPTURE:
        # Real posting ... plus release of the captured slice of the
        # authorization memo, so the memo pair's net always equals uncaptured.
        return [
            Entry(ACC_CUSTOMER_CLEARING, debit=amount, credit=0),
            Entry(ACC_MERCHANT_PAYABLE, debit=0, credit=amount),
            Entry(ACC_AUTHORIZED_RESERVE, debit=amount, credit=0),
            Entry(ACC_AUTHORIZED_FUNDS, debit=0, credit=amount),
        ]
    if op_type is OperationType.REFUND:
        # Reverses part of the captured clearing position.
        return [
            Entry(ACC_REFUND_CLEARING, debit=amount, credit=0),
            Entry(ACC_CUSTOMER_CLEARING, debit=0, credit=amount),
        ]
    if op_type is OperationType.VOID:
        # Release the authorization memo (reverse the authorize pair).
        return [
            Entry(ACC_AUTHORIZED_RESERVE, debit=amount, credit=0),
            Entry(ACC_AUTHORIZED_FUNDS, debit=0, credit=amount),
        ]
    raise DomainError(ErrorCode.INVALID_STATE_TRANSITION, f"unknown operation type {op_type!r}")


def assert_balanced(entries: list[Entry]) -> None:
    debit = sum(e.debit for e in entries)
    credit = sum(e.credit for e in entries)
    if debit != credit:
        raise DomainError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"unbalanced ledger entries: total debit {debit} != total credit {credit}",
        )
