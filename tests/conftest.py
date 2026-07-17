from __future__ import annotations

import os

import pytest

LIVE_TEST_ENV = "NEURO_CODE_RUN_LIVE_TESTS"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep live tests inert unless the caller supplies the explicit cost gate."""

    if os.environ.get(LIVE_TEST_ENV) == "1":
        return
    skip = pytest.mark.skip(
        reason=f"live tests require an explicit {LIVE_TEST_ENV}=1 cost/network opt-in"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
