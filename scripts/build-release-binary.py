#!/usr/bin/env python3
"""Build one pinned-toolchain release binary into a uniform candidate path."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from release_targets import ReleaseContract, ReleaseTarget, load_contract


def release_target_directory(root: Path, contract: ReleaseContract) -> Path:
    """Return the isolated Cargo target directory for the pinned toolchain."""

    return root / "target" / "release-builds" / contract.release_toolchain


def cargo_binary(
    root: Path, contract: ReleaseContract, rust_target: str, binary_name: str
) -> Path:
    """Return Cargo's release output path for one Rust target."""

    return release_target_directory(root, contract) / rust_target / "release" / binary_name


def run_build(root: Path, contract: ReleaseContract, rust_target: str, env: dict[str, str]) -> None:
    env["CARGO_TARGET_DIR"] = str(release_target_directory(root, contract))
    subprocess.run(
        [
            "cargo",
            f"+{contract.release_toolchain}",
            "build",
            "--release",
            "--locked",
            "--package",
            "app-icon-mcp",
            "--target",
            rust_target,
        ],
        cwd=root,
        env=env,
        check=True,
    )


def build_candidate(
    root: Path, contract: ReleaseContract, target: ReleaseTarget, destination: Path
) -> None:
    """Build and copy or merge the approved target without replacing a candidate."""

    if destination.exists():
        raise RuntimeError(f"refusing to replace existing release candidate: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    if target.family in {"macos", "macos_universal2"}:
        if target.macos_minimum is None:
            raise RuntimeError(f"macOS release target {target.id} omitted macos_minimum")
        environment["MACOSX_DEPLOYMENT_TARGET"] = target.macos_minimum

    for rust_target in target.rust_targets:
        run_build(root, contract, rust_target, environment)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        if target.family == "macos_universal2":
            os.close(descriptor)
            descriptor = -1
            temporary.unlink()
            sources = [
                cargo_binary(root, contract, rust_target, target.binary_name)
                for rust_target in target.rust_targets
            ]
            subprocess.run(
                [
                    "xcrun",
                    "lipo",
                    "-create",
                    *map(str, sources),
                    "-output",
                    str(temporary),
                ],
                cwd=root,
                check=True,
            )
            with temporary.open("rb") as candidate:
                os.fsync(candidate.fileno())
        else:
            source = cargo_binary(root, contract, target.rust_targets[0], target.binary_name)
            with os.fdopen(descriptor, "wb") as candidate, source.open("rb") as built_binary:
                descriptor = -1
                shutil.copyfileobj(built_binary, candidate)
                candidate.flush()
                os.fsync(candidate.fileno())
        temporary.chmod(0o755)
        publish_candidate_no_replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def publish_candidate_no_replace(temporary: Path, destination: Path) -> None:
    """Atomically link a complete sibling candidate without replacing a path."""

    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError as error:
        raise RuntimeError(f"refusing to replace existing release candidate: {destination}") from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, default=Path("."))
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    root = arguments.plugin_root.resolve(strict=True)
    contract = load_contract(root / "scripts" / "release-targets.json")
    target = contract.target(arguments.target)
    destination = arguments.output
    if destination is None:
        destination = (
            root
            / "target"
            / "release-candidates"
            / target.id
            / target.binary_name
        )
    else:
        destination = destination.resolve()
    build_candidate(root, contract, target, destination)
    print(destination)


if __name__ == "__main__":
    main()
