"""Regressions for the suite's own environment guards.

A pytest temp root left behind by a process running under a different security
context makes every ``tmp_path`` test fail during fixture setup, which reads as a
repository failure. ``tests/conftest.py`` detects that, redirects the run, and
reports it. These tests pin that detection so the guard cannot rot into silence.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path

import pytest
from conftest import default_pytest_temp_roots, temp_root_failure


def test_missing_directory_is_not_a_failure(tmp_path: Path) -> None:
    # pytest creates the root itself, so absence is normal.
    assert temp_root_failure(tmp_path / "does-not-exist") is None


def test_readable_directory_is_not_a_failure(tmp_path: Path) -> None:
    (tmp_path / "pytest-0").mkdir()
    assert temp_root_failure(tmp_path) is None


def test_unenumerable_directory_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows ACLs cannot be forged portably, so the denial is injected at the
    # exact call pytest itself makes.
    denied = PermissionError(5, "Access is denied")

    def fake_scandir(path):
        raise denied

    monkeypatch.setattr(os, "scandir", fake_scandir)
    assert temp_root_failure(tmp_path) is denied


def test_default_roots_mirror_pytests_own_naming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # pytest uses the raw username, so the guard must not sanitise it. An earlier
    # version replaced every non-alphanumeric character, which silently pointed
    # the guard at a directory pytest never touches.
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path))
    monkeypatch.setattr(getpass, "getuser", lambda: "jan-piet.de vries")
    roots = default_pytest_temp_roots()

    assert roots[0] == tmp_path / "pytest-of-jan-piet.de vries"
    assert roots[0].name == "pytest-of-jan-piet.de vries"
    assert roots[-1] == tmp_path / "pytest-of-unknown"


@pytest.mark.parametrize(
    "username",
    ["plain", "jan-piet", "jan.piet", "jan piet", "j@n", "CORP\\jan"],
)
def test_awkward_usernames_are_joined_exactly_as_pytest_joins_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, username: str
) -> None:
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path))
    monkeypatch.setattr(getpass, "getuser", lambda: username)

    # Compared as a whole path, because a separator in the name (a domain-style
    # account) has to land wherever pytest's identical joinpath lands it.
    assert default_pytest_temp_roots()[0] == tmp_path / f"pytest-of-{username}"


def test_temp_root_is_resolved_like_pytest_resolves_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # pytest calls .resolve() on the temp root. Windows hands back 8.3 short paths
    # (C:\\Users\\RUNNER~1 on GitHub runners) and macOS /tmp is a symlink, so an
    # unresolved root compares unequal to the directory pytest actually uses.
    nested = tmp_path / "sub"
    nested.mkdir()
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(nested / ".."))
    monkeypatch.setattr(getpass, "getuser", lambda: "someone")

    assert default_pytest_temp_roots()[0] == tmp_path.resolve() / "pytest-of-someone"


def test_unresolvable_username_falls_back_like_pytest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path))

    def boom():
        raise KeyError("no such user")

    monkeypatch.setattr(getpass, "getuser", boom)
    roots = default_pytest_temp_roots()

    assert roots == [tmp_path / "pytest-of-unknown"]


def test_guard_matches_the_directory_pytest_actually_uses(
    request: pytest.FixtureRequest,
) -> None:
    # The strongest available check: compare against pytest's own basetemp, whose
    # parent is the root the guard is meant to inspect.
    basetemp = request.config._tmp_path_factory.getbasetemp()
    if request.config.option.basetemp:
        pytest.skip("run was redirected by the guard itself")
    assert basetemp.parent in default_pytest_temp_roots()
