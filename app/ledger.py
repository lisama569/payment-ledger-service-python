"""复式记账分录生成。

账户（币种相同的金额才会记到同一笔支付上）：

- customer_receivable: 对持卡人/渠道的应收。授权借记；退款、撤销贷记冲销。
- merchant_payable:    授权形成的或有应付。捕获、撤销时借记冲销。
- pending_merchant_funds: 捕获清算过渡户，单笔捕获内自平。
- refundable_receivable: 可退余额镜像，其贷方余额恒等于可退金额。
- refund_holding:      退款暂存（当前退款直接冲销应收，该户保留扩展用）。

每种动作生成的分录都借贷相等；账本因此始终平衡：
    sum(debit) == sum(credit)
且对任意支付恒有：
    customer_receivable 借方余额 == refundable_receivable 贷方余额
    == amount_captured - amount_refunded   （可退/净结算金额）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    account: str
    debit: int
    credit: int

    @classmethod
    def dr(cls, account: str, amount: int) -> "Entry":
        return cls(account, amount, 0)

    @classmethod
    def cr(cls, account: str, amount: int) -> "Entry":
        return cls(account, 0, amount)


CUSTOMER_RECEIVABLE = "customer_receivable"
MERCHANT_PAYABLE = "merchant_payable"
PENDING_MERCHANT_FUNDS = "pending_merchant_funds"
REFUNDABLE_RECEIVABLE = "refundable_receivable"
REFUND_HOLDING = "refund_holding"


def authorization_entries(amount: int) -> list[Entry]:
    return [
        Entry.dr(CUSTOMER_RECEIVABLE, amount),
        Entry.cr(MERCHANT_PAYABLE, amount),
    ]


def capture_entries(amount: int) -> list[Entry]:
    return [
        Entry.dr(MERCHANT_PAYABLE, amount),
        Entry.cr(PENDING_MERCHANT_FUNDS, amount),
        Entry.dr(PENDING_MERCHANT_FUNDS, amount),
        Entry.cr(REFUNDABLE_RECEIVABLE, amount),
    ]


def refund_entries(amount: int) -> list[Entry]:
    return [
        Entry.dr(REFUNDABLE_RECEIVABLE, amount),
        Entry.cr(CUSTOMER_RECEIVABLE, amount),
    ]


def void_entries(amount: int) -> list[Entry]:
    return [
        Entry.dr(MERCHANT_PAYABLE, amount),
        Entry.cr(CUSTOMER_RECEIVABLE, amount),
    ]


_ENTRY_FACTORIES = {
    "authorize": authorization_entries,
    "capture": capture_entries,
    "refund": refund_entries,
    "void": void_entries,
}


def entries_for(action: str, amount: int) -> list[Entry]:
    return _ENTRY_FACTORIES[action](amount)


def is_balanced(entries: list[Entry]) -> bool:
    return sum(e.debit for e in entries) == sum(e.credit for e in entries)
