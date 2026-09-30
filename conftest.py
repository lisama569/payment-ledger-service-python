"""Pytest logging setup so each test's input/verdict lines are visible."""
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
)
