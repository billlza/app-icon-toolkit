from __future__ import annotations

import copy
from pathlib import Path
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
                    args=["codex"], returncode=0, stdout="{}", stderr="warning"
                ),
                "emitted stderr despite succeeding: warning",
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



if __name__ == "__main__":
    unittest.main()
