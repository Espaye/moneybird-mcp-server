"""Generate a reproducible CycloneDX SBOM for the built wheel.

The wheel is installed into a disposable environment so the SBOM describes the
actual resolved runtime graph, not the release job's test/build tooling.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
import zipfile


def _venv_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise SystemExit(
                f"Expected one METADATA file in {wheel.name}, found {metadata_files}"
            )
        for line in archive.read(metadata_files[0]).decode("utf-8").splitlines():
            if line.startswith("Version: "):
                return line.removeprefix("Version: ").strip()
    raise SystemExit(f"{wheel.name} does not declare a package version")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", default="dist")
    parser.add_argument("--output-dir", default="sbom")
    parser.add_argument("--expected-version", default="")
    args = parser.parse_args()

    dist_dir = Path(args.dist_dir).resolve()
    wheels = sorted(dist_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(
            f"Expected one wheel in {dist_dir}, found {[path.name for path in wheels]}"
        )
    wheel = wheels[0]
    version = _wheel_version(wheel)
    if args.expected_version and version != args.expected_version:
        raise SystemExit(
            f"Wheel version {version!r} does not match {args.expected_version!r}"
        )

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"moneybird-mcp-{version}.cdx.json"
    with tempfile.TemporaryDirectory(prefix="moneybird_sbom_") as temp:
        environment = Path(temp) / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _venv_python(environment)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel),
            ],
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "cyclonedx_py",
                "environment",
                "--spec-version",
                "1.6",
                "--output-format",
                "JSON",
                "--output-reproducible",
                "--output-file",
                str(output),
                str(python),
            ],
            check=True,
        )

    payload = json.loads(output.read_text(encoding="utf-8"))
    component_names = {
        str(component.get("name") or "")
        for component in payload.get("components", [])
        if isinstance(component, dict)
    }
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.6":
        raise SystemExit("Generated output is not a CycloneDX 1.6 SBOM")
    if "moneybird-mcp" not in component_names:
        raise SystemExit("Generated SBOM does not inventory moneybird-mcp")
    print(f"Generated {output} with {len(component_names)} components")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
