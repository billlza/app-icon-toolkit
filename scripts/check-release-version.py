#!/usr/bin/env python3
"""Verify that a release tag agrees with every versioned project surface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tomllib


SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--plugin-root", type=Path, default=Path("."))
    arguments = parser.parse_args()

    if not arguments.tag.startswith("v"):
        raise SystemExit("release tag must start with `v`")
    version = arguments.tag[1:]
    if SEMVER.fullmatch(version) is None:
        raise SystemExit(f"release tag is not a stable semantic version: {arguments.tag}")

    root = arguments.plugin_root.resolve(strict=True)
    cargo = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    cargo_version = cargo["workspace"]["package"]["version"]
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    plugin_version = manifest["version"].split("+", maxsplit=1)[0]
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")

    mismatches: list[str] = []
    if cargo_version != version:
        mismatches.append(f"Cargo workspace version is {cargo_version}")
    if plugin_version != version:
        mismatches.append(f"plugin manifest version is {plugin_version}")
    if f"## {version} - " not in changelog:
        mismatches.append("changelog has no dated release heading")
    if mismatches:
        details = "; ".join(mismatches)
        raise SystemExit(f"tag {arguments.tag} does not match release metadata: {details}")


if __name__ == "__main__":
    main()
