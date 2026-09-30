import logging

from app.ledger import (
    ACC_AUTHORIZED_FUNDS,
    ACC_AUTHORIZED_RESERVE,
    ACC_CUSTOMER_CLEARING,
    ACC_MERCHANT_PAYABLE,
    ACC_REFUND_CLEARING,
    assert_balanced,
    build_entries,
)
from app.models import OperationType

logger = logging.getLogger(__name__)


def _check(op_type, amount, expected_accounts, total_mult=1):
    entries = build_entries(op_type, amount)
    assert_balanced(entries)
    accounts = {e.account for e in entries}
    debit = sum(e.debit for e in entries)
    credit = sum(e.credit for e in entries)
    logger.info(
        "%s amount=%s -> accounts=%s debit=%s credit=%s balanced=True",
        op_type.value, amount, sorted(accounts), debit, credit,
    )
    assert accounts == expected_accounts
    assert debit == amount * total_mult
    assert credit == amount * total_mult
    return entries


def test_authorize_memo_pair_balanced():
    _check(OperationType.AUTHORIZE, 1000,
           {ACC_AUTHORIZED_FUNDS, ACC_AUTHORIZED_RESERVE})


def test_capture_entries_balanced():
    entries = _check(OperationType.CAPTURE, 400,
                     {ACC_CUSTOMER_CLEARING, ACC_MERCHANT_PAYABLE,
                      ACC_AUTHORIZED_FUNDS, ACC_AUTHORIZED_RESERVE},
                     total_mult=2)
    by = {e.account: e for e in entries}
    assert by[ACC_CUSTOMER_CLEARING].debit == 400
    assert by[ACC_MERCHANT_PAYABLE].credit == 400
    assert by[ACC_AUTHORIZED_FUNDS].credit == 400
    assert by[ACC_AUTHORIZED_RESERVE].debit == 400


def test_refund_reverses_clearing():
    entries = _check(OperationType.REFUND, 150,
                     {ACC_REFUND_CLEARING, ACC_CUSTOMER_CLEARING})
    by = {e.account: e for e in entries}
    assert by[ACC_REFUND_CLEARING].debit == 150
    assert by[ACC_CUSTOMER_CLEARING].credit == 150


def test_void_reverses_memo():
    _check(OperationType.VOID, 1000,
           {ACC_AUTHORIZED_FUNDS, ACC_AUTHORIZED_RESERVE})
