#!/usr/bin/env python3
"""Verify that a release tag agrees with every versioned project surface."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import subprocess


SEMVER_TEXT = (
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
)
SEMVER = re.compile(rf"^{SEMVER_TEXT}$")
RELEASE_HEADING = re.compile(
    rf"^## ({SEMVER_TEXT}) - ([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})$",
    flags=re.MULTILINE,
)
CARGO_METADATA_TIMEOUT_SECONDS = 60


def load_workspace_version(root: Path) -> str:
    """Read the workspace-member version through Cargo's authoritative parser."""

    command = [
        "cargo",
        "metadata",
        "--locked",
        "--no-deps",
        "--format-version",
        "1",
        "--manifest-path",
        str(root / "Cargo.toml"),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=CARGO_METADATA_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Cargo metadata exceeded {CARGO_METADATA_TIMEOUT_SECONDS} seconds"
        ) from error
    except OSError as error:
        raise RuntimeError(f"failed to run Cargo metadata: {error}") from error
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip() or "Cargo returned no diagnostic"
        raise RuntimeError(f"Cargo metadata failed: {diagnostic}")

    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Cargo metadata returned invalid JSON: {error}") from error
    if not isinstance(metadata, dict):
        raise RuntimeError("Cargo metadata root must be an object")
    workspace_members = metadata.get("workspace_members")
    packages = metadata.get("packages")
    if not isinstance(workspace_members, list) or not all(
        isinstance(member, str) for member in workspace_members
    ):
        raise RuntimeError("Cargo metadata workspace_members must be an array of strings")
    if not workspace_members:
        raise RuntimeError("Cargo metadata reported no workspace members")
    if not isinstance(packages, list) or not all(
        isinstance(package, dict) for package in packages
    ):
        raise RuntimeError("Cargo metadata packages must be an array of objects")

    versions_by_id: dict[str, str] = {}
    for package in packages:
        package_id = package.get("id")
        version = package.get("version")
        if not isinstance(package_id, str) or not isinstance(version, str):
            raise RuntimeError("Cargo metadata package id and version must be strings")
        if package_id in versions_by_id:
            raise RuntimeError(f"Cargo metadata repeated package id: {package_id}")
        versions_by_id[package_id] = version

    missing_members = sorted(set(workspace_members).difference(versions_by_id))
    if missing_members:
        raise RuntimeError(
            "Cargo metadata omitted workspace members: " + ", ".join(missing_members)
        )
    workspace_versions = {versions_by_id[member] for member in workspace_members}
    if len(workspace_versions) != 1:
        raise RuntimeError(
            "workspace member versions disagree: "
            + ", ".join(sorted(workspace_versions))
        )
    return next(iter(workspace_versions))


def verify_release_version(tag: str, plugin_root: Path) -> None:
    """Verify one stable tag against every versioned project surface."""

    if not tag.startswith("v"):
        raise SystemExit("release tag must start with `v`")
    version = tag[1:]
    if SEMVER.fullmatch(version) is None:
        raise SystemExit(f"release tag is not a stable semantic version: {tag}")

    root = plugin_root.resolve(strict=True)
    cargo_version = load_workspace_version(root)
    manifest = json.loads(
        (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    if not isinstance(manifest, dict) or not isinstance(manifest.get("version"), str):
        raise RuntimeError("plugin manifest version must be a string")
    plugin_version = manifest["version"].split("+", maxsplit=1)[0]
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")

    mismatches: list[str] = []
    if cargo_version != version:
        mismatches.append(f"Cargo workspace version is {cargo_version}")
    if plugin_version != version:
        mismatches.append(f"plugin manifest version is {plugin_version}")
    release_dates = [
        release_date
        for release_version, release_date in RELEASE_HEADING.findall(changelog)
        if release_version == version
    ]
    if len(release_dates) != 1:
        mismatches.append("changelog must contain exactly one dated release heading")
    else:
        try:
            date.fromisoformat(release_dates[0])
        except ValueError:
            mismatches.append("changelog release heading has an invalid date")
    if mismatches:
        details = "; ".join(mismatches)
        raise SystemExit(f"tag {tag} does not match release metadata: {details}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--plugin-root", type=Path, default=Path("."))
    arguments = parser.parse_args()
    verify_release_version(arguments.tag, arguments.plugin_root)


if __name__ == "__main__":
    main()
