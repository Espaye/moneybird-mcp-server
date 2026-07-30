"""Build wheel/sdist twice with the same inputs and require identical hashes."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def _source_date_epoch(root: Path, explicit: str) -> str:
    if explicit:
        return explicit
    result = subprocess.run(
        ["git", "log", "-1", "--format=%ct"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-date-epoch", default="")
    parser.add_argument(
        "--output-dir",
        default="",
        help=(
            "After the comparison passes, copy the exact first compared wheel "
            "and sdist here. The directory must be empty."
        ),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    epoch = _source_date_epoch(root, args.source_date_epoch)
    environment = {
        **os.environ,
        "SOURCE_DATE_EPOCH": epoch,
        "PYTHONHASHSEED": "0",
    }
    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    if output_dir is not None and output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(
            f"Refusing to mix reproducible artifacts with existing files in {output_dir}"
        )
    with tempfile.TemporaryDirectory(prefix="moneybird_repro_") as temp:
        base = Path(temp)
        build_directories: list[Path] = []
        outputs: list[dict[str, str]] = []
        for index in (1, 2):
            destination = base / f"build-{index}"
            destination.mkdir()
            build_directories.append(destination)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--outdir",
                    str(destination),
                ],
                cwd=root,
                env=environment,
                check=True,
            )
            outputs.append(_hashes(destination))
        if outputs[0] != outputs[1]:
            names = sorted(set(outputs[0]) | set(outputs[1]))
            differences = {
                name: {
                    "first": outputs[0].get(name),
                    "second": outputs[1].get(name),
                }
                for name in names
                if outputs[0].get(name) != outputs[1].get(name)
            }
            raise SystemExit(f"Build is not reproducible: {differences}")
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            for artifact in sorted(build_directories[0].iterdir()):
                if artifact.is_file():
                    shutil.copy2(artifact, output_dir / artifact.name)
    for name, digest in outputs[0].items():
        print(f"{digest}  {name}")
    print(f"Reproducible build passed with SOURCE_DATE_EPOCH={epoch}")
    if output_dir is not None:
        print(f"Published compared artifacts to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
