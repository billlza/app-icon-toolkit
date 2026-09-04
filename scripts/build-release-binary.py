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
from release_files import FilePublicationIndeterminate, publish_sibling_no_replace
import macos_signing


MACOS_UNSIGNED_RUSTFLAGS = "\x1f".join(
    ("-C", "link-arg=-Wl,-no_adhoc_codesign")
)


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


def release_environment(target: ReleaseTarget) -> dict[str, str]:
    """Return a controlled environment for one release target build."""

    environment = os.environ.copy()
    for name in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        if environment.get(name):
            raise RuntimeError(f"release builds reject ambient {name}")
        environment.pop(name, None)
    if target.family in {"macos", "macos_universal2"}:
        if target.macos_minimum is None:
            raise RuntimeError(f"macOS release target {target.id} omitted macos_minimum")
        environment["MACOSX_DEPLOYMENT_TARGET"] = target.macos_minimum
        environment["CARGO_ENCODED_RUSTFLAGS"] = MACOS_UNSIGNED_RUSTFLAGS
    return environment


def verify_unsigned_macos_binary(
    binary: Path,
    expected_architectures: tuple[str, ...],
    runner: macos_signing.CommandRunner,
) -> None:
    """Require exact Mach-O slices and a uniform unsigned pre-sign state."""

    actual = macos_signing.architectures(binary, runner)
    if len(actual) != len(expected_architectures) or set(actual) != set(
        expected_architectures
    ):
        raise RuntimeError(
            f"Mach-O architectures are {actual!r}; expected {expected_architectures!r}"
        )
    states = macos_signing.inspect_pre_signatures(
        binary,
        expected_architectures,
        runner,
    )
    if any(state.kind is not macos_signing.PreSignKind.UNSIGNED for state in states):
        raise RuntimeError(
            f"macOS release candidate must be uniformly unsigned: {states!r}"
        )


def build_candidate(
    root: Path, contract: ReleaseContract, target: ReleaseTarget, destination: Path
) -> None:
    """Build and copy or merge the approved target without replacing a candidate."""

    if destination.exists():
        raise RuntimeError(f"refusing to replace existing release candidate: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    environment = release_environment(target)
    signing_runner = macos_signing.SubprocessRunner(timeout_seconds=60)

    for rust_target in target.rust_targets:
        run_build(root, contract, rust_target, environment)
        if target.family in {"macos", "macos_universal2"}:
            architecture = "arm64" if rust_target.startswith("aarch64-") else "x86_64"
            verify_unsigned_macos_binary(
                cargo_binary(root, contract, rust_target, target.binary_name),
                (architecture,),
                signing_runner,
            )

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    preserve_temporary = False
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
        if target.family in {"macos", "macos_universal2"}:
            verify_unsigned_macos_binary(
                temporary,
                target.macos_architectures(),
                signing_runner,
            )
        publish_candidate_no_replace(temporary, destination)
    except FilePublicationIndeterminate:
        preserve_temporary = True
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not preserve_temporary and temporary.exists():
            temporary.unlink()


def publish_candidate_no_replace(temporary: Path, destination: Path) -> None:
    """Atomically link a complete sibling candidate without replacing a path."""

    publish_sibling_no_replace(
        temporary,
        destination,
        label="release candidate",
    )


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
