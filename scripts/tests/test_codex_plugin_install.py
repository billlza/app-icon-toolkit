from __future__ import annotations

import copy
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from release_test_support import check_codex_plugin_install


class CodexPluginInstallTests(unittest.TestCase):
    def test_json_command_reports_process_and_protocol_failures(self) -> None:
        cases = [
            (
                subprocess.CompletedProcess(
                    args=["codex"], returncode=7, stdout="", stderr="install failed"
                ),
                "exited with 7: install failed",
            ),
            (
                subprocess.CompletedProcess(
                    args=["codex"], returncode=0, stdout="not-json", stderr=""
                ),
                "returned invalid JSON",
            ),
            (
                subprocess.CompletedProcess(
                    args=["codex"],
                    returncode=0,
                    stdout="{}",
                    stderr=(
                        "WARNING: proceeding, even though we could not create PATH "
                        "aliases: Refusing to create helper binaries under temporary "
                        'dir "/tmp"'
                    ),
                ),
                "emitted stderr despite succeeding: WARNING: proceeding",
            ),
        ]
        for completed, message in cases:
            with self.subTest(message=message):
                with mock.patch.object(
                    check_codex_plugin_install.subprocess,
                    "run",
                    return_value=completed,
                ):
                    with self.assertRaisesRegex(RuntimeError, message):
                        check_codex_plugin_install.run_json_command(
                            ["codex", "plugin", "list", "--json"]
                        )

        with mock.patch.object(
            check_codex_plugin_install.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["codex"], 180),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed to run codex"):
                check_codex_plugin_install.run_json_command(["codex"])

    def test_json_command_and_install_receipts(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout='{"marketplaceName":"app-icon-toolkit"}',
            stderr="",
        )
        with mock.patch.object(
            check_codex_plugin_install.subprocess,
            "run",
            return_value=completed,
        ) as run:
            value = check_codex_plugin_install.run_json_command(
                ["codex", "plugin", "list", "--json"]
            )

        self.assertEqual(value["marketplaceName"], "app-icon-toolkit")
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            check_codex_plugin_install.COMMAND_TIMEOUT_SECONDS,
        )
        self.assertIsNone(run.call_args.kwargs["env"])

        with tempfile.TemporaryDirectory(prefix="codex-install-receipt-test-") as temporary:
            root = Path(temporary)
            check_codex_plugin_install.validate_marketplace_receipt(
                {
                    "marketplaceName": "app-icon-toolkit",
                    "installedRoot": str(root),
                    "alreadyAdded": False,
                },
                root.resolve(),
            )
            with self.assertRaisesRegex(RuntimeError, "already installed"):
                check_codex_plugin_install.validate_marketplace_receipt(
                    {
                        "marketplaceName": "app-icon-toolkit",
                        "installedRoot": str(root),
                        "alreadyAdded": True,
                    },
                    root.resolve(),
                )

            installed = root / "installed"
            installed.mkdir()
            receipt = {
                "pluginId": "app-icon-toolkit@app-icon-toolkit",
                "name": "app-icon-toolkit",
                "marketplaceName": "app-icon-toolkit",
                "version": "0.2.2",
                "installedPath": str(installed),
            }
            self.assertEqual(
                check_codex_plugin_install.validate_install_receipt(receipt, "0.2.2"),
                installed.resolve(),
            )
            receipt["version"] = "0.2.1"
            with self.assertRaisesRegex(RuntimeError, "version.*0.2.1.*0.2.2"):
                check_codex_plugin_install.validate_install_receipt(receipt, "0.2.2")

    def test_clean_host_environment_isolated_cache_and_credentials(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-home-isolation-test-") as temporary:
            codex_home = Path(temporary) / "codex-home"
            installed = codex_home / "plugins" / "cache" / "plugin"
            installed.mkdir(parents=True)
            with mock.patch.dict(
                check_codex_plugin_install.os.environ,
                {
                    "CODEX_HOME": "/ambient/codex-home",
                    "OPENAI_API_KEY": "secret",
                    "GH_TOKEN": "secret",
                },
            ):
                environment = check_codex_plugin_install.isolated_host_environment(
                    codex_home
                )

            self.assertEqual(environment["CODEX_HOME"], str(codex_home))
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("GH_TOKEN", environment)
            check_codex_plugin_install.validate_isolated_cache_path(
                installed, codex_home
            )

            outside = Path(temporary) / "outside"
            outside.mkdir()
            with self.assertRaisesRegex(RuntimeError, "escapes isolated CODEX_HOME"):
                check_codex_plugin_install.validate_isolated_cache_path(
                    outside, codex_home
                )

    def test_isolated_host_workspace_uses_private_user_home_and_cleans_up(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="codex-workspace-parent-test-",
            dir=Path.cwd(),
        ) as temporary:
            root = Path(temporary)
            fake_home = root / "home"
            plugin_root = root / "plugin"
            fake_home.mkdir(mode=0o700)
            plugin_root.mkdir()

            with mock.patch.object(Path, "home", return_value=fake_home):
                with check_codex_plugin_install.isolated_host_workspace(
                    plugin_root
                ) as (first_workspace, codex_home):
                    self.assertEqual(first_workspace.parent, fake_home.resolve())
                    self.assertEqual(codex_home.parent, first_workspace)
                    self.assertEqual(
                        check_codex_plugin_install.isolated_host_environment(
                            codex_home
                        )["CODEX_HOME"],
                        str(codex_home),
                    )
                    if hasattr(os, "getuid"):
                        self.assertEqual(
                            stat.S_IMODE(os.lstat(first_workspace).st_mode),
                            0o700,
                        )
                        self.assertEqual(
                            stat.S_IMODE(os.lstat(codex_home).st_mode),
                            0o700,
                        )
                    with check_codex_plugin_install.isolated_host_workspace(
                        plugin_root
                    ) as (second_workspace, _second_codex_home):
                        self.assertNotEqual(second_workspace, first_workspace)
                    self.assertFalse(second_workspace.exists())
                    retained_workspace = first_workspace

            self.assertFalse(retained_workspace.exists())

    def test_isolated_host_workspace_rejects_temporary_or_missing_home(self) -> None:
        plugin_root = Path.cwd().resolve(strict=True)
        with tempfile.TemporaryDirectory(
            prefix="codex-temporary-home-test-"
        ) as temporary:
            temporary_home = Path(temporary)
            with mock.patch.object(Path, "home", return_value=temporary_home):
                with self.assertRaisesRegex(RuntimeError, "system temporary"):
                    with check_codex_plugin_install.isolated_host_workspace(
                        plugin_root
                    ):
                        self.fail("system temporary homes must be rejected")

            missing_home = temporary_home / "missing"
            with mock.patch.object(Path, "home", return_value=missing_home):
                with self.assertRaisesRegex(RuntimeError, "cannot resolve"):
                    with check_codex_plugin_install.isolated_host_workspace(
                        plugin_root
                    ):
                        self.fail("missing homes must be rejected")

    def test_isolated_host_workspace_rejects_plugin_source_containment(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="codex-workspace-containment-test-",
            dir=Path.cwd(),
        ) as temporary:
            fake_home = Path(temporary) / "home"
            fake_home.mkdir(mode=0o700)
            with mock.patch.object(Path, "home", return_value=fake_home):
                with self.assertRaisesRegex(RuntimeError, "outside plugin source"):
                    with check_codex_plugin_install.isolated_host_workspace(
                        fake_home.resolve()
                    ):
                        self.fail("host workspace cannot be inside plugin source")
            self.assertEqual(list(fake_home.iterdir()), [])

    def test_codex_executable_is_resolved_before_working_directory_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="codex-executable-test-",
            dir=Path.cwd(),
        ) as temporary:
            executable = Path(temporary) / "bin" / "codex"
            executable.parent.mkdir()
            executable.write_bytes(b"executable")
            relative = Path(os.path.relpath(executable, Path.cwd()))

            with mock.patch.object(
                check_codex_plugin_install.shutil,
                "which",
                return_value=str(relative),
            ):
                resolved = check_codex_plugin_install.resolve_codex_executable()

            self.assertEqual(resolved, str(executable.resolve()))

        with mock.patch.object(
            check_codex_plugin_install.shutil,
            "which",
            return_value=None,
        ):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                check_codex_plugin_install.resolve_codex_executable()

    def test_plugin_listing_requires_one_enabled_expected_version(self) -> None:
        valid = {
            "installed": [
                {
                    "pluginId": "app-icon-toolkit@app-icon-toolkit",
                    "name": "app-icon-toolkit",
                    "marketplaceName": "app-icon-toolkit",
                    "version": "0.2.2",
                    "installed": True,
                    "enabled": True,
                }
            ],
            "available": [],
        }
        check_codex_plugin_install.validate_plugin_listing(valid, "0.2.2")

        disabled = copy.deepcopy(valid)
        disabled["installed"][0]["enabled"] = False
        with self.assertRaisesRegex(RuntimeError, "enabled.*False.*True"):
            check_codex_plugin_install.validate_plugin_listing(disabled, "0.2.2")

    def test_preinstall_listing_requires_available_but_not_installed_plugin(self) -> None:
        available = {
            "installed": [],
            "available": [
                {
                    "pluginId": "app-icon-toolkit@app-icon-toolkit",
                    "version": "0.2.2",
                    "installed": False,
                }
            ],
        }
        check_codex_plugin_install.validate_preinstall_listing(available, "0.2.2")

        already_installed = copy.deepcopy(available)
        already_installed["installed"] = copy.deepcopy(already_installed["available"])
        already_installed["available"] = []
        with self.assertRaisesRegex(RuntimeError, "already contains installed"):
            check_codex_plugin_install.validate_preinstall_listing(
                already_installed, "0.2.2"
            )

    def test_installed_cache_copy_is_distinct_and_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="installed-copy-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            installed = root / "installed"
            for relative in check_codex_plugin_install.STATIC_PATHS:
                for package_root in (source, installed):
                    path = package_root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(relative.as_posix().encode("utf-8"))
            for package_root in (source, installed):
                binary = package_root / "bin" / "app-icon-toolkit-mcp"
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(b"binary")

            check_codex_plugin_install.validate_installed_copy(source, installed)

            (installed / "bin" / "app-icon-toolkit-mcp").write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "differs from the archive"):
                check_codex_plugin_install.validate_installed_copy(source, installed)

            with self.assertRaisesRegex(RuntimeError, "independent cache copy"):
                check_codex_plugin_install.validate_installed_copy(source, source)

    def test_mcp_listing_and_get_resolve_the_independent_cache_binary(self) -> None:
        for binary_name in ("app-icon-toolkit-mcp", "app-icon-toolkit-mcp.exe"):
            with self.subTest(binary_name=binary_name):
                with tempfile.TemporaryDirectory(
                    prefix="codex-mcp-resolution-test-"
                ) as temporary:
                    installed = Path(temporary) / "installed"
                    binary = installed / "bin" / binary_name
                    binary.parent.mkdir(parents=True)
                    binary.write_bytes(b"binary")
                    server = {
                        "name": "app-icon-toolkit",
                        "enabled": True,
                        "disabled_reason": None,
                        "transport": {
                            "type": "stdio",
                            "command": "./bin/app-icon-toolkit-mcp",
                            "args": [],
                            "env": None,
                            "env_vars": [],
                            "cwd": str(installed),
                        },
                        "startup_timeout_sec": None,
                        "tool_timeout_sec": None,
                    }

                    listing_entry = {**server, "auth_status": "unsupported"}
                    check_codex_plugin_install.validate_mcp_listing(
                        [listing_entry], installed
                    )
                    get_response = {
                        **server,
                        "enabled_tools": None,
                        "disabled_tools": None,
                    }
                    check_codex_plugin_install.validate_mcp_get(
                        get_response, installed
                    )

    def test_mcp_host_configuration_fails_closed(self) -> None:
        check_codex_plugin_install.validate_empty_mcp_listing([])
        with self.assertRaisesRegex(RuntimeError, "before plugin installation"):
            check_codex_plugin_install.validate_empty_mcp_listing([{}])

        with tempfile.TemporaryDirectory(
            prefix="codex-mcp-invalid-config-test-"
        ) as temporary:
            installed = Path(temporary) / "installed"
            binary = installed / "bin" / "app-icon-toolkit-mcp"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"binary")
            invalid = {
                "name": "app-icon-toolkit",
                "enabled": True,
                "disabled_reason": None,
                "transport": {
                    "type": "stdio",
                    "command": "./bin/app-icon-toolkit-mcp",
                    "args": [],
                    "env": {"TOKEN": "unexpected"},
                    "env_vars": [],
                    "cwd": str(installed),
                },
                "startup_timeout_sec": None,
                "tool_timeout_sec": None,
                "enabled_tools": None,
                "disabled_tools": None,
            }
            with self.assertRaisesRegex(RuntimeError, "transport env"):
                check_codex_plugin_install.validate_mcp_get(invalid, installed)


if __name__ == "__main__":
    unittest.main()
