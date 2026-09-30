"""复式记账：每笔动作借贷平衡，全链路账户恒等式始终成立。"""

from __future__ import annotations

import logging

from app.ledger import (
    CUSTOMER_RECEIVABLE,
    entries_for,
    is_balanced,
)

log = logging.getLogger("tests")


def test_every_action_is_balanced():
    for action in ("authorize", "capture", "refund", "void"):
        entries = entries_for(action, 777)
        debit = sum(e.debit for e in entries)
        credit = sum(e.credit for e in entries)
        log.info("action=%s amount=777 debit=%s credit=%s balanced=%s",
                 action, debit, credit, is_balanced(entries))
        assert is_balanced(entries)
        # authorize/refund/void 各一对分录；capture 两对（含过渡户自平），各 2x
        assert debit == credit == (1554 if action == "capture" else 777)


def test_full_partial_lifecycle_invariants(service):
    log.info("input authorize=1000, captures=[600,400], refunds=[200], then full refund")
    service.authorize("p", 1000, "USD", "k-a")
    service.capture("p", 600, "USD", "k-c1")
    service.refund("p", 200, "USD", "k-r1")
    service.capture("p", 400, "USD", "k-c2")
    service.refund("p", 800, "USD", "k-r2")
    view = service.get_payment("p")
    log.info("result status=%s captured=%s refunded=%s refundable=%s",
             view["status"], view["amount_captured"], view["amount_refunded"],
             view["refundable_amount"])
    assert view["status"] == "REFUNDED"
    assert view["refundable_amount"] == 0

    rows = service.conn.execute(
        "SELECT account, SUM(debit-credit) AS bal FROM ledger_entries "
        "WHERE payment_id=? GROUP BY account", ("p",)
    ).fetchall()
    bals = {r["account"]: r["bal"] for r in rows}
    log.info("account balances=%s", bals)
    # 退完后：所有应收/应付/过渡户都归零，账本完全结平
    assert all(v == 0 for v in bals.values()), bals


def test_void_after_partial_capture_leaves_clean_balances(service):
    log.info("input authorize=1000 capture=300 void(rest)=700 refund=300")
    service.authorize("p", 1000, "USD", "k-a")
    service.capture("p", 300, "USD", "k-c")
    service.void("p", "k-v")
    service.refund("p", 300, "USD", "k-r")
    view = service.get_payment("p")
    log.info("result status=%s captured=%s voided=%s refunded=%s",
             view["status"], view["amount_captured"], view["amount_voided"],
             view["amount_refunded"])
    rows = service.conn.execute(
        "SELECT account, SUM(debit-credit) AS bal FROM ledger_entries "
        "WHERE payment_id=? GROUP BY account", ("p",)
    ).fetchall()
    bals = {r["account"]: r["bal"] for r in rows}
    log.info("account balances=%s", bals)
    assert all(v == 0 for v in bals.values()), bals
    # 每个动作内部分录都必须平衡
    per_action = service.conn.execute(
        "SELECT action, SUM(debit) d, SUM(credit) c FROM ledger_entries "
        "WHERE payment_id=? GROUP BY action_seq, action", ("p",)
    ).fetchall()
    for r in per_action:
        assert r["d"] == r["c"], dict(r)
        log.info("balanced action=%s sum=%s", r["action"], r["d"])
