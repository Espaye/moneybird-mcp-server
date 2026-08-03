"""Install built wheel/sdist artifacts in isolated environments and smoke-test imports."""
from __future__ import annotations

import argparse
import email
import os
import subprocess
import tempfile
import venv
import zipfile
from pathlib import Path


def _single_artifact(dist_dir: Path, pattern: str, label: str) -> Path:
    matches = sorted(dist_dir.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(
            f"Expected exactly one {label} matching {pattern!r} in {dist_dir}, "
            f"found {[path.name for path in matches]}"
        )
    return matches[0].resolve()


def _venv_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _venv_script(environment: Path, name: str) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / f"{name}.exe"
    return environment / "bin" / name


def _install_and_smoke(
    artifact: Path,
    *,
    label: str,
    expected_version: str,
    with_pdf: bool = False,
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"moneybird-{label}-") as temp:
        root = Path(temp)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _venv_python(environment)
        requirement = f"{artifact}[pdf]" if with_pdf else str(artifact)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                requirement,
            ],
            check=True,
            cwd=root,
        )
        pdf_check = "import pypdf; " if with_pdf else ""
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                (
                    "from importlib.metadata import version; "
                    "import moneybird_mcp; import moneybird_mcp.server; "
                    "from moneybird_mcp.capabilities import capability_mode; "
                    "from moneybird_mcp.tools import mcp; "
                    f"{pdf_check}"
                    f"assert version('moneybird-mcp') == {expected_version!r}; "
                    "config = moneybird_mcp.server.build_config([]); "
                    "assert config.transport == 'stdio'; "
                    "assert config.credential_mode == 'local'; "
                    "assert capability_mode().value == 'read_only'; "
                    "assert mcp is not None"
                ),
            ],
            check=True,
            cwd=root,
        )
        subprocess.run(
            [str(_venv_script(environment, "moneybird-mcp")), "--help"],
            check=True,
            cwd=root,
            stdout=subprocess.DEVNULL,
        )


def _assert_pdf_extra(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise SystemExit(
                f"Expected one METADATA file in {wheel.name}, found {metadata_names}"
            )
        metadata = email.message_from_bytes(archive.read(metadata_names[0]))
    if "pdf" not in metadata.get_all("Provides-Extra", []):
        raise SystemExit("Built wheel does not declare the expected 'pdf' extra")
    pdf_requirements = [
        requirement
        for requirement in metadata.get_all("Requires-Dist", [])
        if requirement.lower().startswith("pypdf")
    ]
    if not pdf_requirements or not any(
        "; extra == " in item.lower() and "pdf" in item.split(";", 1)[1].lower()
        for item in pdf_requirements
    ):
        raise SystemExit(
            "Built wheel does not conditionally declare pypdf for the 'pdf' extra"
        )
    version = str(metadata.get("Version") or "").strip()
    if not version:
        raise SystemExit("Built wheel METADATA does not declare a version")
    return version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", default="dist")
    parser.add_argument("--expected-version", default="")
    parser.add_argument(
        "--wheel-only",
        action="store_true",
        help="Smoke only the wheel (used by the release artifact matrix).",
    )
    args = parser.parse_args()

    dist_dir = Path(args.dist_dir).resolve()
    wheel = _single_artifact(dist_dir, "*.whl", "wheel")
    metadata_version = _assert_pdf_extra(wheel)
    expected_version = args.expected_version or metadata_version
    if metadata_version != expected_version:
        raise SystemExit(
            f"Wheel version {metadata_version} does not match expected "
            f"{expected_version}"
        )
    _install_and_smoke(
        wheel,
        label="wheel",
        expected_version=expected_version,
        with_pdf=True,
    )
    if args.wheel_only:
        print(f"Clean installation smoke passed for {wheel.name}")
        return
    sdist = _single_artifact(dist_dir, "*.tar.gz", "sdist")
    _install_and_smoke(
        sdist,
        label="sdist",
        expected_version=expected_version,
    )
    print(f"Clean installation smoke passed for {wheel.name} and {sdist.name}")


if __name__ == "__main__":
    main()
