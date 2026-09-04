#!/usr/bin/env python3
"""Validate local-marketplace installation on a clean Codex host."""

from __future__ import annotations

import argparse
import filecmp
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from release_package import STATIC_PATHS


COMMAND_TIMEOUT_SECONDS = 180
MAX_DIAGNOSTIC_CHARS = 4_096
PLUGIN_NAME = "app-icon-toolkit"
MARKETPLACE_NAME = "app-icon-toolkit"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"


def execute_command(
    command: list[str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=COMMAND_TIMEOUT_SECONDS,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"failed to run {' '.join(command)}: {error}") from error

    if completed.returncode != 0:
        detail = diagnostic_excerpt(completed.stderr or completed.stdout)
        raise RuntimeError(
            f"{' '.join(command)} exited with {completed.returncode}: {detail}"
        )
    if completed.stderr.strip():
        raise RuntimeError(
            f"{' '.join(command)} emitted stderr despite succeeding: "
            f"{diagnostic_excerpt(completed.stderr)}"
        )
    return completed


def diagnostic_excerpt(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return "no diagnostics"
    if len(normalized) <= MAX_DIAGNOSTIC_CHARS:
        return normalized
    return f"{normalized[:MAX_DIAGNOSTIC_CHARS]}... [truncated]"


def run_json_command(command: list[str], cwd: Path | None = None) -> Any:
    completed = execute_command(command, cwd)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{' '.join(command)} returned invalid JSON: {error}"
        ) from error


def run_command(command: list[str], cwd: Path | None = None) -> None:
    execute_command(command, cwd)


def expect_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} must be a JSON object")
    return value


def expect_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{context} must be a non-empty string")
    return value


def load_plugin_version(plugin_root: Path) -> str:
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        manifest = expect_object(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            "plugin manifest",
        )
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"failed to read plugin manifest {manifest_path}: {error}") from error
    return expect_string(manifest.get("version"), "plugin manifest version")


def load_codex_host_version(plugin_root: Path) -> str:
    version_path = plugin_root / "CODEX_HOST_TEST_VERSION"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"failed to read Codex host version {version_path}: {error}") from error
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise RuntimeError(f"Codex host test version is invalid: {version!r}")
    return version


def validate_marketplace_receipt(value: Any, plugin_root: Path) -> None:
    receipt = expect_object(value, "marketplace receipt")
    if receipt.get("marketplaceName") != MARKETPLACE_NAME:
        raise RuntimeError("marketplace receipt has the wrong marketplaceName")
    if receipt.get("alreadyAdded") is not False:
        raise RuntimeError("clean host reported that the marketplace was already installed")
    installed_root = Path(
        expect_string(receipt.get("installedRoot"), "marketplace installedRoot")
    ).resolve(strict=True)
    if installed_root != plugin_root:
        raise RuntimeError(
            f"marketplace installedRoot is {installed_root}, expected {plugin_root}"
        )


def validate_install_receipt(value: Any, expected_version: str) -> Path:
    receipt = expect_object(value, "plugin installation receipt")
    expected = {
        "pluginId": PLUGIN_ID,
        "name": PLUGIN_NAME,
        "marketplaceName": MARKETPLACE_NAME,
        "version": expected_version,
    }
    for field, expected_value in expected.items():
        if receipt.get(field) != expected_value:
            raise RuntimeError(
                f"plugin installation receipt {field} is {receipt.get(field)!r}, "
                f"expected {expected_value!r}"
            )
    installed_path = Path(
        expect_string(receipt.get("installedPath"), "plugin installedPath")
    ).resolve(strict=True)
    if not installed_path.is_dir():
        raise RuntimeError(f"plugin installedPath is not a directory: {installed_path}")
    return installed_path


def packaged_binary_relative_path(plugin_root: Path) -> Path:
    candidates = [
        Path("bin/app-icon-toolkit-mcp"),
        Path("bin/app-icon-toolkit-mcp.exe"),
    ]
    existing = [relative for relative in candidates if (plugin_root / relative).is_file()]
    if len(existing) != 1:
        raise RuntimeError(
            "plugin package must contain exactly one platform MCP executable; "
            f"found {[path.as_posix() for path in existing]}"
        )
    return existing[0]


def validate_installed_copy(plugin_root: Path, installed_path: Path) -> None:
    if installed_path == plugin_root or installed_path.is_relative_to(plugin_root):
        raise RuntimeError("Codex installedPath did not identify an independent cache copy")
    relative_paths = (*STATIC_PATHS, packaged_binary_relative_path(plugin_root))
    for relative in relative_paths:
        source = plugin_root / relative
        installed = installed_path / relative
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"source package entry is not a regular file: {source}")
        if installed.is_symlink() or not installed.is_file():
            raise RuntimeError(f"installed cache entry is not a regular file: {installed}")
        if source.samefile(installed):
            raise RuntimeError(f"installed cache entry aliases the source package: {relative}")
        if not filecmp.cmp(source, installed, shallow=False):
            raise RuntimeError(f"installed cache entry differs from the archive: {relative}")


def validate_plugin_listing(value: Any, expected_version: str) -> None:
    listing = expect_object(value, "plugin listing")
    installed = listing.get("installed")
    if not isinstance(installed, list):
        raise RuntimeError("plugin listing installed field must be an array")
    matches = [
        expect_object(entry, "installed plugin entry")
        for entry in installed
        if isinstance(entry, dict) and entry.get("pluginId") == PLUGIN_ID
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"plugin listing must contain exactly one {PLUGIN_ID}; found {len(matches)}"
        )
    plugin = matches[0]
    expected = {
        "name": PLUGIN_NAME,
        "marketplaceName": MARKETPLACE_NAME,
        "version": expected_version,
        "installed": True,
        "enabled": True,
    }
    for field, expected_value in expected.items():
        if plugin.get(field) != expected_value:
            raise RuntimeError(
                f"installed plugin listing {field} is {plugin.get(field)!r}, "
                f"expected {expected_value!r}"
            )


def validate_preinstall_listing(value: Any, expected_version: str) -> None:
    listing = expect_object(value, "pre-install plugin listing")
    installed = listing.get("installed")
    available = listing.get("available")
    if not isinstance(installed, list) or not isinstance(available, list):
        raise RuntimeError("pre-install plugin listing must contain installed and available arrays")
    if any(
        isinstance(entry, dict) and entry.get("pluginId") == PLUGIN_ID
        for entry in installed
    ):
        raise RuntimeError(f"clean host already contains installed plugin {PLUGIN_ID}")
    matches = [
        entry
        for entry in available
        if isinstance(entry, dict) and entry.get("pluginId") == PLUGIN_ID
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"pre-install listing must expose exactly one available {PLUGIN_ID}; "
            f"found {len(matches)}"
        )
    plugin = matches[0]
    if plugin.get("version") != expected_version or plugin.get("installed") is not False:
        raise RuntimeError("pre-install listing has the wrong version or installation state")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin_root", type=Path)
    arguments = parser.parse_args()

    plugin_root = arguments.plugin_root.resolve(strict=True)
    expected_version = load_plugin_version(plugin_root)
    expected_host_version = load_codex_host_version(plugin_root)
    codex = shutil.which("codex")
    if codex is None:
        raise SystemExit("Codex executable is unavailable")
    for relative in (
        Path(".agents/plugins/marketplace.json"),
        Path(".codex-plugin/plugin.json"),
        Path(".mcp.json"),
    ):
        path = plugin_root / relative
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"installed plugin input is unavailable: {path}")

    with tempfile.TemporaryDirectory(prefix="app-icon-toolkit-codex-host-") as temporary:
        host_working_directory = Path(temporary)
        host_version = execute_command(
            [codex, "--version"], host_working_directory
        ).stdout.strip()
        if host_version != f"codex-cli {expected_host_version}":
            raise SystemExit(
                f"Codex host version is {host_version!r}, "
                f"expected 'codex-cli {expected_host_version}'"
            )
        marketplace_receipt = run_json_command(
            [codex, "plugin", "marketplace", "add", str(plugin_root), "--json"],
            host_working_directory,
        )
        validate_marketplace_receipt(marketplace_receipt, plugin_root)

        preinstall_listing = run_json_command(
            [
                codex,
                "plugin",
                "list",
                "--marketplace",
                MARKETPLACE_NAME,
                "--available",
                "--json",
            ],
            host_working_directory,
        )
        validate_preinstall_listing(preinstall_listing, expected_version)

        install_receipt = run_json_command(
            [codex, "plugin", "add", PLUGIN_ID, "--json"],
            host_working_directory,
        )
        installed_path = validate_install_receipt(install_receipt, expected_version)
        validate_installed_copy(plugin_root, installed_path)

        run_command(
            [
                sys.executable,
                str(Path(__file__).with_name("smoke-installed-plugin.py")),
                str(installed_path),
            ],
            host_working_directory,
        )

        listing = run_json_command(
            [codex, "plugin", "list", "--marketplace", MARKETPLACE_NAME, "--json"],
            host_working_directory,
        )
        validate_plugin_listing(listing, expected_version)

    print(
        json.dumps(
            {
                "marketplace": MARKETPLACE_NAME,
                "plugin": PLUGIN_NAME,
                "status": "installed_and_listed",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
