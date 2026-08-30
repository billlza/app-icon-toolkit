#!/usr/bin/env python3
"""Install the pinned Rust toolchain components for one release target."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from release_targets import ReleaseContract, ReleaseTarget, load_contract


def install(contract: ReleaseContract, target: ReleaseTarget) -> None:
    """Install exactly the toolchain, targets, and inspectors in the contract."""

    subprocess.run(
        [
            "rustup",
            "toolchain",
            "install",
            contract.release_toolchain,
            "--profile",
            "minimal",
        ],
        check=True,
    )
    for rust_target in target.rust_targets:
        subprocess.run(
            [
                "rustup",
                "target",
                "add",
                "--toolchain",
                contract.release_toolchain,
                rust_target,
            ],
            check=True,
        )
    if target.family == "windows_msvc":
        subprocess.run(
            [
                "rustup",
                "component",
                "add",
                "--toolchain",
                contract.release_toolchain,
                "llvm-tools-preview",
            ],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, default=Path("."))
    parser.add_argument("--target", required=True)
    arguments = parser.parse_args()

    root = arguments.plugin_root.resolve(strict=True)
    contract = load_contract(root / "scripts" / "release-targets.json")
    install(contract, contract.target(arguments.target))


if __name__ == "__main__":
    main()
