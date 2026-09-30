import logging

import pytest

from app.money import format_money, from_minor, to_minor, validate_currency

logger = logging.getLogger(__name__)


def test_to_minor_roundtrip_decimal_safety():
    cases = [
        (0.1, "USD", 10),
        (0.2, "USD", 20),
        (19.99, "USD", 1999),
        (100, "JPY", 100),       # zero-decimal currency
        ("1.005", "USD", 101),   # ROUND_HALF_UP
        (-5.5, "EUR", -550),
    ]
    for amount, ccy, expected in cases:
        got = to_minor(amount, ccy)
        logger.info("to_minor(%r, %s) -> %s (expected %s)", amount, ccy, got, expected)
        assert got == expected


def test_float_summation_does_not_corrupt():
    total = to_minor(0.1, "USD") + to_minor(0.2, "USD")
    logger.info("0.1 + 0.2 in cents -> %s", total)
    assert total == 30


def test_format_and_from_minor():
    assert from_minor(1999, "USD").__str__() == "19.99"
    assert format_money(1999, "USD") == "19.99"
    assert format_money(100, "JPY") == "100"
    assert format_money(-550, "EUR") == "-5.50"
    logger.info("format roundtrip USD 1999 -> %s", format_money(1999, "USD"))


def test_unsupported_currency_rejected():
    with pytest.raises(ValueError):
        validate_currency("XYZ")
    with pytest.raises(ValueError):
        to_minor(1, "XYZ")
    logger.info("unsupported currency XYZ -> ValueError (as expected)")
