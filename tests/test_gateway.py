import logging

import pytest

from app.errors import DomainError, ErrorCode
from app.gateway import MockGateway

logger = logging.getLogger(__name__)


def test_retry_same_op_id_returns_same_ref_and_applies_once():
    gw = MockGateway()
    ref1 = gw.authorize("op-1", "pay-1", 1000, "USD")
    ref2 = gw.authorize("op-1", "pay-1", 1000, "USD")
    logger.info("authorize op-1 twice -> %s, %s (must be equal)", ref1, ref2)
    assert ref1 == ref2
    assert len(gw._processed) == 1


def test_fail_once_then_succeeds():
    gw = MockGateway()
    gw.fail_once.add("op-2")
    with pytest.raises(DomainError) as exc:
        gw.capture("op-2", "pay-1", 500, "USD")
    assert exc.value.code is ErrorCode.GATEWAY_FAILURE
    logger.info("first capture(op-2) -> %s (expected failure)", exc.value.code.value)
    ref = gw.capture("op-2", "pay-1", 500, "USD")
    logger.info("retry capture(op-2) -> %s (success)", ref)
    assert ref.startswith("GW-")
    # Another retry is still a replay, not a new charge
    assert gw.capture("op-2", "pay-1", 500, "USD") == ref
