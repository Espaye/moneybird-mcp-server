"""Suite-wide policy defaults and test-environment guards.

Production defaults to read-only. Most historical tests intentionally exercise
write preparation/execution, so the test process opts into writes explicitly;
policy-specific tests override/clear this value.
"""
from __future__ import annotations

import getpass
import os
import tempfile
from pathlib import Path

import pytest

from moneybird.capabilities import CAPABILITY_MODE_ENV


@pytest.fixture(autouse=True)
def _explicit_test_write_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CAPABILITY_MODE_ENV, "write_enabled")


# --- pytest temp-root guard ---------------------------------------------------
#
# pytest keeps its ``tmp_path`` scratch space in ``<tempdir>/pytest-of-<user>``
# and garbage-collects old runs by scanning that directory. If the directory
# survives from a process that ran under a different security context, the scan
# raises PermissionError during *fixture setup*, so every test using ``tmp_path``
# reports an opaque collection error that looks like a repository failure.
#
# The directory is pytest's own scratch space, so relocating this run is safe and
# loses nothing. The condition is reported loudly rather than silently patched:
# it says something real about the machine, just nothing about this code.


def default_pytest_temp_roots() -> list[Path]:
    """Reproduce pytest's own temp-root candidates, in the order pytest tries them.

    pytest uses the *raw* ``getpass.getuser()`` -- it does not sanitise it -- and
    falls back to ``pytest-of-unknown`` when creating that directory raises. An
    earlier version of this guard invented a sanitising regex, which happens to
    agree only for usernames that are already alphanumeric; a Windows account name
    containing a dot, hyphen or domain separator would have made the guard inspect
    a directory pytest never touches, leaving the real failure in place.

    The temp root is resolved for the same reason pytest resolves it. On Windows
    ``tempfile.gettempdir()` can hand back an 8.3 short path (``C:\\Users\\RUNNER~1``
    on GitHub runners), and on macOS ``/tmp`` is a symlink; comparing an unresolved
    path against pytest's resolved one silently misses the directory again.
    """

    root = Path(
        os.environ.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir()
    ).resolve()
    candidates = []
    try:
        candidates.append(root / f"pytest-of-{getpass.getuser()}")
    except (ImportError, OSError, KeyError):
        pass
    candidates.append(root / "pytest-of-unknown")
    return candidates


def temp_root_failure(path: Path) -> OSError | None:
    """Return the error that makes ``path`` unusable as a temp root, if any.

    A missing directory is fine -- pytest creates it. Only an existing directory
    that cannot be enumerated is a problem, because that is what breaks the
    numbered-directory cleanup.
    """

    if not path.exists():
        return None
    try:
        with os.scandir(path) as entries:
            for _entry in entries:
                break
    except OSError as exc:
        return exc
    return None


_TEMP_ROOT_NOTICE: list[str] = []


def pytest_configure(config: pytest.Config) -> None:
    if config.option.basetemp:
        return
    broken = [
        (root, failure)
        for root, failure in (
            (root, temp_root_failure(root)) for root in default_pytest_temp_roots()
        )
        if failure is not None
    ]
    if not broken:
        return
    replacement = Path(tempfile.mkdtemp(prefix="moneybird-pytest-basetemp-"))
    config.option.basetemp = str(replacement)
    for root, failure in broken:
        _TEMP_ROOT_NOTICE.append(
            f"UNUSABLE PYTEST TEMP ROOT: {root} exists but cannot be enumerated "
            f"({type(failure).__name__}: {failure}). This run was redirected to "
            f"{replacement}. This is a machine-local condition, not a repository "
            "failure: the directory was created by a process running under a "
            "different security context. Remove or rename it (an elevated shell "
            "may be required), or set PYTEST_DEBUG_TEMPROOT to a writable "
            "directory."
        )


def pytest_report_header() -> list[str]:
    return list(_TEMP_ROOT_NOTICE)


def pytest_terminal_summary(terminalreporter) -> None:
    # Repeat at the end so the notice survives a long, scrolling run.
    for notice in _TEMP_ROOT_NOTICE:
        terminalreporter.write_line(notice, yellow=True, bold=True)
