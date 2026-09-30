"""持久化：关闭连接/重新打开后金额与分录恒等式依旧成立。"""

from __future__ import annotations

import logging

from .conftest import service_on

log = logging.getLogger("tests")


def test_state_and_entries_survive_reopen(db_path):
    svc = service_on(db_path)
    svc.authorize("p", 1000, "USD", "ka")
    svc.capture("p", 700, "USD", "kc1")
    svc.refund("p", 250, "USD", "kr1")
    svc.capture("p", 200, "USD", "kc2")
    svc.void("p", "kv")  # 撤销最后 100 开放授权
    svc.conn.close()
    log.info("wrote mixed lifecycle; reopening database file")

    svc2 = service_on(db_path)
    view = svc2.get_payment("p")
    log.info("reloaded status=%s authorized=%s captured=%s refunded=%s voided=%s "
             "refundable=%s open=%s",
             view["status"], view["amount_authorized"], view["amount_captured"],
             view["amount_refunded"], view["amount_voided"],
             view["refundable_amount"], view["open_authorized_amount"])
    assert view["status"] == "PARTIALLY_REFUNDED"
    assert view["amount_captured"] == 900
    assert view["amount_refunded"] == 250
    assert view["amount_voided"] == 100
    assert view["refundable_amount"] == 650
    assert view["open_authorized_amount"] == 0

    # 分录借贷总额相等
    sums = svc2.conn.execute(
        "SELECT SUM(debit) d, SUM(credit) c FROM ledger_entries WHERE payment_id='p'"
    ).fetchone()
    assert sums["d"] == sums["c"]
    # 每个动作（action_seq）自身平衡
    per = svc2.conn.execute(
        "SELECT action_seq, SUM(debit) d, SUM(credit) c FROM ledger_entries "
        "WHERE payment_id='p' GROUP BY action_seq"
    ).fetchall()
    assert all(r["d"] == r["c"] for r in per)
    log.info("reopened ledger: total debit=%s credit=%s across %d balanced actions",
             sums["d"], sums["c"], len(per))
    svc2.conn.close()


def test_idempotent_replay_after_reopen(db_path):
    svc = service_on(db_path)
    svc.authorize("p", 1000, "USD", "ka")
    svc.capture("p", 500, "USD", "kc")
    svc.conn.close()

    svc2 = service_on(db_path)
    r = svc2.capture("p", 500, "USD", "kc")
    assert r["replayed"] is True and r["amount_captured"] == 500
    log.info("replay after reopen: replayed=%s captured stays 500", r["replayed"])
    svc2.conn.close()
