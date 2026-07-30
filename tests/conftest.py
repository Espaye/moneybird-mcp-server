"""Suite-wide policy defaults.

Production defaults to read-only. Most historical tests intentionally exercise
write preparation/execution, so the test process opts into writes explicitly;
policy-specific tests override/clear this value.
"""
from __future__ import annotations

import pytest

from moneybird.capabilities import CAPABILITY_MODE_ENV


@pytest.fixture(autouse=True)
def _explicit_test_write_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CAPABILITY_MODE_ENV, "write_enabled")
