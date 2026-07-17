"""Build the Claude Desktop extension bundle (.mcpb).

A .mcpb is a zip containing manifest.json, server/main.py and server/lib/ (the
moneybird package plus all dependencies, pip-installed with --target). Because
the dependency tree contains compiled wheels (pydantic-core), the bundle is
specific to the platform and Python minor version it was built on — build on
Windows for Windows users, on macOS for macOS users.

Usage (from the repo root):

    python scripts/build_mcpb.py

Output: dist/moneybird-mcp-<version>-<platform>-py<X.Y>.mcpb
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def main() -> None:
    manifest_path = ROOT / "mcpb" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest["version"]

    staging = DIST / "mcpb-staging"
    if staging.exists():
        shutil.rmtree(staging)
    lib = staging / "server" / "lib"
    lib.mkdir(parents=True)

    print("Installing moneybird-mcp and dependencies into the bundle ...")
    subprocess.run(
        # Include the [pdf] extra so the Desktop bundle can read attachment text layers.
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(lib), f"{ROOT}[pdf]"],
        check=True,
    )

    shutil.copy(manifest_path, staging / "manifest.json")
    shutil.copy(ROOT / "mcpb" / "main.py", staging / "server" / "main.py")
    license_path = ROOT / "LICENSE"
    if license_path.exists():
        shutil.copy(license_path, staging / "LICENSE")

    tag = f"{sys.platform}-py{sys.version_info.major}.{sys.version_info.minor}"
    out = DIST / f"moneybird-mcp-{version}-{tag}.mcpb"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(staging.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                bundle.write(path, path.relative_to(staging).as_posix())

    shutil.rmtree(staging)
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"Built {out} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
