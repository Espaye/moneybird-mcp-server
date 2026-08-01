"""Subprocess regressions for the operator-controlled configuration boundary."""
from __future__ import annotations

import json
import os
import site
import subprocess
import sys
from pathlib import Path

import pytest

from moneybird.config import MoneybirdError, load_env_file

ROOT = Path(__file__).resolve().parent.parent
SECURITY_ENV_KEYS = {
    "MONEYBIRD_ACCESS_TOKEN",
    "MONEYBIRD_ADMINISTRATION_ID",
    "MONEYBIRD_CAPABILITY_MODE",
    "MONEYBIRD_CREDENTIAL_MODE",
    "MONEYBIRD_MCP_DATA_DIR",
    "MONEYBIRD_OAUTH_CLIENT_ID",
    "MONEYBIRD_OAUTH_CLIENT_SECRET",
    "MCP_AUTH_TOKEN",
    "MCP_HOST",
    "MCP_PORT",
    "MCP_TRANSPORT",
    "MCP_TOOL_DISCOVERY",
    "MCP_TRUSTED_TLS_PROXY",
}
HOSTILE_ENV_TEXT = "\n".join(
    [
        "MONEYBIRD_CAPABILITY_MODE=write_enabled",
        "MONEYBIRD_ADMINISTRATION_ID=999",
        "MCP_TRANSPORT=http",
        "MCP_HOST=0.0.0.0",
        "MCP_PORT=9999",
        "MCP_AUTH_TOKEN=attacker-secret",
        "MCP_TRUSTED_TLS_PROXY=true",
        "MONEYBIRD_CREDENTIAL_MODE=hosted_request_only",
        "MONEYBIRD_ACCESS_TOKEN=attacker-token",
        "MONEYBIRD_MCP_DATA_DIR=attacker-directory",
        "MCP_TOOL_DISCOVERY=full",
    ]
)


def _import_paths() -> str:
    """Resolve the subprocess import path from this interpreter, not the environment.

    The allowlist below strips the ambient environment on purpose -- that is the
    boundary under test -- and the home-directory tests deliberately point HOME
    and USERPROFILE at an isolated directory. On Windows, Python derives the
    per-user site-packages location from %APPDATA%, which the allowlist does not
    carry, so the subprocess could not import third-party dependencies and failed
    before reaching the assertion. Resolving the site directories here keeps the
    environment strict while making the import path deterministic on every
    platform.
    """

    paths = [str(ROOT)]
    try:
        paths.extend(site.getsitepackages())
    except AttributeError:  # pragma: no cover - not present in some embeddings
        pass
    if site.ENABLE_USER_SITE:
        user_site = site.getusersitepackages()
        if isinstance(user_site, str):
            paths.append(user_site)
        else:
            paths.extend(user_site)
    seen: set[str] = set()
    unique = [path for path in paths if path and not (path in seen or seen.add(path))]
    return os.pathsep.join(unique)


def _clean_subprocess_env(**overrides: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "COMSPEC",
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "WINDIR",
        }
    }
    env["PYTHONPATH"] = _import_paths()
    env.update(overrides)
    return env


def _run_probe(cwd: Path, *, args: list[str] | None = None, **env: str) -> dict:
    code = """
import json
import os
from moneybird.capabilities import capability_mode
from moneybird.server import build_config

config = build_config(%r)
keys = %r
print(json.dumps({
    "capability": capability_mode().value,
    "configuration": {key: os.environ.get(key) for key in keys},
    "server": {
        "transport": config.transport,
        "host": config.host,
        "port": config.port,
        "auth_token": config.auth_token,
        "credential_mode": config.credential_mode,
    },
}))
""" % (args or [], sorted(SECURITY_ENV_KEYS))
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=_clean_subprocess_env(**env),
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


def test_stripped_environment_still_resolves_third_party_imports() -> None:
    """The allowlist must cost the subprocess nothing but the ambient configuration.

    Two Windows-specific ways this broke before: the per-user site-packages
    directory is derived from %APPDATA%, which the allowlist deliberately drops,
    and an inherited invalid stdin handle makes subprocess.run raise WinError 6
    before the probe runs at all. Both turned a boundary assertion into an
    unrelated infrastructure error.
    """

    completed = subprocess.run(
        [sys.executable, "-c", "import httpx, moneybird; print('ok')"],
        env=_clean_subprocess_env(),
        check=False,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_hostile_working_directory_env_is_never_discovered(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        HOSTILE_ENV_TEXT,
        encoding="utf-8",
    )

    result = _run_probe(
        tmp_path,
        MONEYBIRD_ACCESS_TOKEN="parent-fake-token",
    )

    assert result["capability"] == "read_only"
    assert result["configuration"]["MONEYBIRD_ACCESS_TOKEN"] == "parent-fake-token"
    assert all(
        value is None
        for key, value in result["configuration"].items()
        if key != "MONEYBIRD_ACCESS_TOKEN"
    )
    assert result["server"] == {
        "transport": "stdio",
        "host": "127.0.0.1",
        "port": 8000,
        "auth_token": "",
        "credential_mode": "local",
    }


def test_same_hostile_file_loads_only_when_its_exact_path_is_selected(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        HOSTILE_ENV_TEXT,
        encoding="utf-8",
    )

    result = _run_probe(tmp_path, args=["--env-file", str(env_file)])

    assert result["capability"] == "write_enabled"
    assert result["configuration"]["MONEYBIRD_ADMINISTRATION_ID"] == "999"
    assert result["configuration"]["MONEYBIRD_ACCESS_TOKEN"] == "attacker-token"
    assert result["configuration"]["MONEYBIRD_MCP_DATA_DIR"] == "attacker-directory"
    assert result["configuration"]["MCP_TOOL_DISCOVERY"] == "full"
    assert result["server"] == {
        "transport": "http",
        "host": "0.0.0.0",
        "port": 9999,
        "auth_token": "attacker-secret",
        "credential_mode": "hosted_request_only",
    }


def test_explicit_env_file_is_loaded_before_configuration(tmp_path: Path) -> None:
    env_file = tmp_path / "operator.env"
    env_file.write_text(
        "\n".join(
            [
                "MONEYBIRD_ADMINISTRATION_ID=123",
                "MONEYBIRD_CAPABILITY_MODE=read_only",
                "MONEYBIRD_CREDENTIAL_MODE=network_single_user",
                "MONEYBIRD_MCP_DATA_DIR=operator-state",
                "MCP_AUTH_TOKEN=operator-edge-secret",
                "MCP_HOST=127.0.0.1",
                "MCP_PORT=8123",
                "MCP_TRANSPORT=http",
                "MCP_TRUSTED_TLS_PROXY=false",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_probe(tmp_path, args=["--env-file", str(env_file)])

    assert result["capability"] == "read_only"
    assert result["configuration"]["MONEYBIRD_ADMINISTRATION_ID"] == "123"
    assert result["configuration"]["MONEYBIRD_MCP_DATA_DIR"] == "operator-state"
    assert result["server"] == {
        "transport": "http",
        "host": "127.0.0.1",
        "port": 8123,
        "auth_token": "operator-edge-secret",
        "credential_mode": "network_single_user",
    }


def test_parent_process_values_win_over_explicit_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "operator.env"
    env_file.write_text(
        "\n".join(
            [
                "MONEYBIRD_ACCESS_TOKEN=file-moneybird-token",
                "MONEYBIRD_ADMINISTRATION_ID=999",
                "MONEYBIRD_CAPABILITY_MODE=write_enabled",
                "MONEYBIRD_CREDENTIAL_MODE=network_single_user",
                "MONEYBIRD_MCP_DATA_DIR=file-state",
                "MCP_HOST=0.0.0.0",
                "MCP_PORT=9443",
                "MCP_TRANSPORT=http",
                "MCP_AUTH_TOKEN=env-file-secret",
                "MCP_TRUSTED_TLS_PROXY=true",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_probe(
        tmp_path,
        args=["--env-file", str(env_file)],
        MONEYBIRD_ACCESS_TOKEN="parent-moneybird-token",
        MONEYBIRD_ADMINISTRATION_ID="42",
        MONEYBIRD_CAPABILITY_MODE="read_only",
        MONEYBIRD_CREDENTIAL_MODE="local",
        MONEYBIRD_MCP_DATA_DIR="parent-state",
        MCP_HOST="localhost",
        MCP_PORT="9000",
        MCP_TRANSPORT="stdio",
        MCP_AUTH_TOKEN="parent-secret",
        MCP_TRUSTED_TLS_PROXY="false",
    )

    assert result["capability"] == "read_only"
    assert result["configuration"]["MONEYBIRD_ACCESS_TOKEN"] == "parent-moneybird-token"
    assert result["configuration"]["MONEYBIRD_ADMINISTRATION_ID"] == "42"
    assert result["configuration"]["MONEYBIRD_CREDENTIAL_MODE"] == "local"
    assert result["configuration"]["MONEYBIRD_MCP_DATA_DIR"] == "parent-state"
    assert result["configuration"]["MCP_HOST"] == "localhost"
    assert result["configuration"]["MCP_PORT"] == "9000"
    assert result["configuration"]["MCP_AUTH_TOKEN"] == "parent-secret"
    assert result["configuration"]["MCP_TRUSTED_TLS_PROXY"] == "false"
    assert result["server"]["transport"] == "stdio"
    assert result["server"]["host"] == "localhost"
    assert result["server"]["port"] == 9000
    assert result["server"]["auth_token"] == "parent-secret"


def test_invalid_variable_name_does_not_partially_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "invalid.env"
    env_file.write_text(
        "MONEYBIRD_CAPABILITY_MODE=write_enabled\nBAD-NAME=value\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MONEYBIRD_CAPABILITY_MODE", raising=False)

    with pytest.raises(MoneybirdError, match="invalid variable name"):
        load_env_file(env_file)

    assert "MONEYBIRD_CAPABILITY_MODE" not in os.environ


def test_stdio_main_uses_home_state_not_hostile_working_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "MONEYBIRD_MCP_DATA_DIR=attacker-state\n",
        encoding="utf-8",
    )
    isolated_home = tmp_path / "operator-home"
    isolated_home.mkdir()
    code = """
import json
import os
import sys
import types

fake_mcp = types.SimpleNamespace(run=lambda **kwargs: None)
fake_tools = types.ModuleType("moneybird.tools")
fake_tools.mcp = fake_mcp
sys.modules["moneybird.tools"] = fake_tools

from moneybird.server import main
main([])
print(json.dumps({"data_dir": os.environ["MONEYBIRD_MCP_DATA_DIR"]}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_clean_subprocess_env(
            HOME=str(isolated_home),
            USERPROFILE=str(isolated_home),
        ),
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=20,
    )

    assert json.loads(completed.stdout)["data_dir"] == str(
        isolated_home / ".moneybird-mcp"
    )


def test_oauth_login_uses_same_home_state_default_as_stdio(tmp_path: Path) -> None:
    isolated_home = tmp_path / "operator-home"
    isolated_home.mkdir()
    code = """
import json
import os
import sys
from unittest import mock

from scripts import oauth_login

with (
    mock.patch.object(
        oauth_login.oauth,
        "build_authorize_url",
        return_value="https://example.invalid/authorize",
    ),
    mock.patch.object(oauth_login.webbrowser, "open", return_value=False),
    mock.patch("builtins.input", return_value=""),
    mock.patch.object(sys, "argv", ["oauth_login.py"]),
):
    result = oauth_login.main()
print(json.dumps({
    "data_dir": os.environ["MONEYBIRD_MCP_DATA_DIR"],
    "result": result,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_clean_subprocess_env(
            HOME=str(isolated_home),
            USERPROFILE=str(isolated_home),
        ),
        check=False,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])

    assert result == {
        "data_dir": str(isolated_home / ".moneybird-mcp"),
        "result": 1,
    }
