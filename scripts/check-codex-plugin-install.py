#!/usr/bin/env python3
"""Validate local-marketplace installation on a clean Codex host."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import contextmanager
import filecmp
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterator

from release_package import STATIC_PATHS


COMMAND_TIMEOUT_SECONDS = 180
MAX_DIAGNOSTIC_CHARS = 4_096
PLUGIN_NAME = "app-icon-toolkit"
MARKETPLACE_NAME = "app-icon-toolkit"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
CREDENTIAL_ENVIRONMENT_NAMES = (
    "CODEX_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
)


def execute_command(
    command: list[str],
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
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
            env=environment,
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


def run_json_command(
    command: list[str],
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Any:
    completed = execute_command(command, cwd, environment)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{' '.join(command)} returned invalid JSON: {error}"
        ) from error


def run_command(
    command: list[str],
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> None:
    execute_command(command, cwd, environment)


def expect_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} must be a JSON object")
    return value


def expect_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{context} must be a non-empty string")
    return value


def isolated_host_environment(codex_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in CREDENTIAL_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    environment["CODEX_HOME"] = str(codex_home)
    return environment


def _validate_host_directory(
    path: Path,
    *,
    label: str,
    require_private: bool,
) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise RuntimeError(f"cannot inspect {label} {path}: {error}") from error
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or (reparse_attribute and file_attributes & reparse_attribute)
    ):
        raise RuntimeError(f"{label} must be an ordinary directory: {path}")
    if hasattr(os, "getuid"):
        if metadata.st_uid != os.getuid():
            raise RuntimeError(f"{label} must be owned by the current user: {path}")
        forbidden_permissions = 0o077 if require_private else 0o022
        if stat.S_IMODE(metadata.st_mode) & forbidden_permissions:
            requirement = (
                "must be private"
                if require_private
                else "must not be writable by group or other users"
            )
            raise RuntimeError(f"{label} {requirement}: {path}")


def _host_workspace_parent() -> Path:
    try:
        user_home = Path.home().resolve(strict=True)
        system_temporary = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"cannot resolve isolated host workspace parent: {error}") from error
    _validate_host_directory(
        user_home,
        label="user home",
        require_private=False,
    )
    if user_home == system_temporary or user_home.is_relative_to(system_temporary):
        raise RuntimeError(
            f"user home must not be inside the system temporary directory: {user_home}"
        )
    return user_home


@contextmanager
def isolated_host_workspace(
    plugin_root: Path,
) -> Iterator[tuple[Path, Path]]:
    """Create one private Codex host state root outside system temporary paths."""

    parent = _host_workspace_parent()
    with tempfile.TemporaryDirectory(
        prefix=".app-icon-toolkit-codex-host-",
        dir=parent,
    ) as temporary:
        workspace = Path(temporary).resolve(strict=True)
        _validate_host_directory(
            workspace,
            label="isolated host workspace",
            require_private=True,
        )
        if workspace == plugin_root or workspace.is_relative_to(plugin_root):
            raise RuntimeError(
                f"isolated host workspace must be outside plugin source: {workspace}"
            )
        codex_home = workspace / "codex-home"
        codex_home.mkdir(mode=0o700)
        _validate_host_directory(
            codex_home,
            label="isolated CODEX_HOME",
            require_private=True,
        )
        yield workspace, codex_home


def resolve_codex_executable() -> str:
    """Resolve the host command before subprocesses change working directory."""

    candidate = shutil.which("codex")
    if candidate is None:
        raise RuntimeError("Codex executable is unavailable")
    try:
        return str(Path(candidate).resolve(strict=True))
    except OSError as error:
        raise RuntimeError(
            f"Codex executable cannot be resolved: {candidate}: {error}"
        ) from error


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


def validate_isolated_cache_path(installed_path: Path, codex_home: Path) -> None:
    installed_path = installed_path.resolve(strict=True)
    codex_home = codex_home.resolve(strict=True)
    try:
        relative = installed_path.relative_to(codex_home)
    except ValueError as error:
        raise RuntimeError(
            f"installed plugin cache {installed_path} escapes isolated CODEX_HOME {codex_home}"
        ) from error
    if not relative.parts:
        raise RuntimeError("installed plugin cache cannot be the CODEX_HOME root")


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


def validate_empty_mcp_listing(value: Any) -> None:
    if value != []:
        raise RuntimeError(
            "clean Codex host exposed MCP servers before plugin installation"
        )


def validate_mcp_server(value: Any, installed_path: Path) -> None:
    server = expect_object(value, "MCP server configuration")
    expected_server_fields = {
        "name": PLUGIN_NAME,
        "enabled": True,
        "disabled_reason": None,
        "startup_timeout_sec": None,
        "tool_timeout_sec": None,
    }
    for field, expected_value in expected_server_fields.items():
        if server.get(field) != expected_value:
            raise RuntimeError(
                f"MCP server configuration {field} is {server.get(field)!r}, "
                f"expected {expected_value!r}"
            )

    transport = expect_object(server.get("transport"), "MCP server transport")
    expected_transport_fields = {
        "type": "stdio",
        "command": "./bin/app-icon-toolkit-mcp",
        "args": [],
        "env": None,
        "env_vars": [],
    }
    for field, expected_value in expected_transport_fields.items():
        if transport.get(field) != expected_value:
            raise RuntimeError(
                f"MCP server transport {field} is {transport.get(field)!r}, "
                f"expected {expected_value!r}"
            )

    configured_cwd = Path(
        expect_string(transport.get("cwd"), "MCP server transport cwd")
    ).resolve(strict=True)
    installed_path = installed_path.resolve(strict=True)
    if configured_cwd != installed_path:
        raise RuntimeError(
            f"MCP server transport cwd is {configured_cwd}, expected {installed_path}"
        )

    configured_command = Path(expected_transport_fields["command"])
    unresolved_command = (configured_cwd / configured_command).resolve(strict=False)
    packaged_binary = (
        installed_path / packaged_binary_relative_path(installed_path)
    ).resolve(strict=True)
    if packaged_binary.suffix.lower() == ".exe" and not unresolved_command.suffix:
        unresolved_command = unresolved_command.with_name(
            f"{unresolved_command.name}.exe"
        )
    if unresolved_command != packaged_binary:
        raise RuntimeError(
            "MCP server command does not resolve to the installed platform binary: "
            f"{unresolved_command} != {packaged_binary}"
        )


def validate_mcp_listing(value: Any, installed_path: Path) -> None:
    if not isinstance(value, list):
        raise RuntimeError("MCP listing must be a JSON array")
    if len(value) != 1:
        raise RuntimeError(
            "clean Codex host must expose exactly one MCP server after installation; "
            f"found {len(value)}"
        )
    validate_mcp_server(value[0], installed_path)
    server = expect_object(value[0], "MCP listing entry")
    if server.get("auth_status") != "unsupported":
        raise RuntimeError(
            "local stdio MCP server must report unsupported authentication status"
        )


def validate_mcp_get(value: Any, installed_path: Path) -> None:
    validate_mcp_server(value, installed_path)
    server = expect_object(value, "MCP get response")
    for field in ("enabled_tools", "disabled_tools"):
        if server.get(field) is not None:
            raise RuntimeError(
                f"MCP get response {field} is {server.get(field)!r}, expected None"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin_root", type=Path)
    arguments = parser.parse_args()

    plugin_root = arguments.plugin_root.resolve(strict=True)
    expected_version = load_plugin_version(plugin_root)
    expected_host_version = load_codex_host_version(plugin_root)
    try:
        codex = resolve_codex_executable()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    for relative in (
        Path(".agents/plugins/marketplace.json"),
        Path(".codex-plugin/plugin.json"),
        Path(".mcp.json"),
    ):
        path = plugin_root / relative
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"installed plugin input is unavailable: {path}")

    with isolated_host_workspace(plugin_root) as (
        host_working_directory,
        codex_home,
    ):
        host_environment = isolated_host_environment(codex_home)
        host_version = execute_command(
            [codex, "--version"], host_working_directory, host_environment
        ).stdout.strip()
        if host_version != f"codex-cli {expected_host_version}":
            raise SystemExit(
                f"Codex host version is {host_version!r}, "
                f"expected 'codex-cli {expected_host_version}'"
            )
        marketplace_receipt = run_json_command(
            [codex, "plugin", "marketplace", "add", str(plugin_root), "--json"],
            host_working_directory,
            host_environment,
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
            host_environment,
        )
        validate_preinstall_listing(preinstall_listing, expected_version)
        validate_empty_mcp_listing(
            run_json_command(
                [codex, "mcp", "list", "--json"],
                host_working_directory,
                host_environment,
            )
        )

        install_receipt = run_json_command(
            [codex, "plugin", "add", PLUGIN_ID, "--json"],
            host_working_directory,
            host_environment,
        )
        installed_path = validate_install_receipt(install_receipt, expected_version)
        validate_isolated_cache_path(installed_path, codex_home)
        validate_installed_copy(plugin_root, installed_path)

        validate_mcp_listing(
            run_json_command(
                [codex, "mcp", "list", "--json"],
                host_working_directory,
                host_environment,
            ),
            installed_path,
        )
        validate_mcp_get(
            run_json_command(
                [codex, "mcp", "get", PLUGIN_NAME, "--json"],
                host_working_directory,
                host_environment,
            ),
            installed_path,
        )

        run_command(
            [
                sys.executable,
                str(Path(__file__).with_name("smoke-installed-plugin.py")),
                str(installed_path),
            ],
            host_working_directory,
            host_environment,
        )

        listing = run_json_command(
            [
                codex,
                "plugin",
                "list",
                "--marketplace",
                MARKETPLACE_NAME,
                "--json",
            ],
            host_working_directory,
            host_environment,
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
