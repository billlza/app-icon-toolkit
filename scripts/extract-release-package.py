#!/usr/bin/env python3
"""Safely extract one allowlisted App Icon Toolkit release archive."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from release_package import expected_archive_members, safe_extract_archive
from release_targets import load_contract


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, default=Path("."))
    parser.add_argument("--target", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    plugin_root = arguments.plugin_root.resolve(strict=True)
    target = load_contract(plugin_root / "scripts" / "release-targets.json").target(
        arguments.target
    )
    output = Path(os.path.abspath(arguments.output))
    if output.exists():
        raise SystemExit(f"refusing to use an existing extraction root: {output}")
    if not output.parent.is_dir():
        raise SystemExit(f"extraction parent does not exist: {output.parent}")
    output.mkdir(mode=0o700)

    package_root = safe_extract_archive(
        arguments.archive,
        target.archive_format,
        output,
        expected_archive_members(target.binary_name),
    )
    print(package_root)


if __name__ == "__main__":
    main()
