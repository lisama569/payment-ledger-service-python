"""模拟支付渠道：纯本地实现，不发起任何真实网络调用、不需要凭据。

用途：在 authorize / capture / refund / void 被调用时，按预设脚本返回
成功或抛出“渠道异常”（超时 / 拒绝 / 故障），以验证：
- 渠道在本地事务提交前抛错 => 整个事务回滚，账目不留半条；
- 客户端用同一个幂等键重试 => 只记账一次。

脚本是 "ok" / "raise:<kind>" 组成的列表，按调用次数依次消费，
耗尽后一律成功。kind 可取 timeout / declined / error。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque


class MockGatewayError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class MockGatewayTimeout(MockGatewayError):
    def __init__(self) -> None:
        super().__init__("timeout", "simulated gateway timeout (request outcome unknown)")


class MockGatewayDeclined(MockGatewayError):
    def __init__(self) -> None:
        super().__init__("declined", "simulated gateway decline")


class MockGatewayFault(MockGatewayError):
    def __init__(self) -> None:
        super().__init__("error", "simulated gateway internal error")


_RAISERS: dict[str, Callable[[], MockGatewayError]] = {
    "timeout": MockGatewayTimeout,
    "declined": MockGatewayDeclined,
    "error": MockGatewayFault,
}


@dataclass
class MockGateway:
    script: list[str] = field(default_factory=list)
    calls: int = 0
    _queue: Deque[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._queue = deque(self.script)

    def process(
        self,
        action: str,
        payment_id: str,
        amount: int,
        currency: str,
    ) -> dict:
        self.calls += 1
        if self._queue:
            directive = self._queue.popleft()
            if directive != "ok":
                kind = directive.split(":", 1)[1]
                raise _RAISERS[kind]()
        return {
            "channel": "mock-channel-v1",
            "channel_ref": f"mock-{action}-{payment_id}-{self.calls}",
            "status": "approved",
            "action": action,
            "amount": amount,
            "currency": currency,
        }
