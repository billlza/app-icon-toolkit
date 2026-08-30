from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]


def load_script(module_name: str, filename: str):
    specification = importlib.util.spec_from_file_location(
        module_name, SCRIPTS_ROOT / filename
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {filename}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


prepare_release_package = load_script(
    "prepare_release_package", "prepare-release-package.py"
)
smoke_installed_plugin = load_script(
    "smoke_installed_plugin", "smoke-installed-plugin.py"
)


class InstalledBinaryNameTests(unittest.TestCase):
    def test_maps_every_release_target(self) -> None:
        self.assertEqual(
            prepare_release_package.installed_binary_name(
                "x86_64-pc-windows-msvc"
            ),
            "app-icon-toolkit-mcp.exe",
        )
        for target in (
            "x86_64-unknown-linux-gnu",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
        ):
            with self.subTest(target=target):
                self.assertEqual(
                    prepare_release_package.installed_binary_name(target),
                    "app-icon-toolkit-mcp",
                )

    def test_rejects_an_unapproved_release_target(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported release target"):
            prepare_release_package.installed_binary_name("i686-pc-windows-msvc")


class PackagedCommandResolutionTests(unittest.TestCase):
    def test_resolves_from_plugin_cwd_instead_of_parent_process_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="app-icon-command-test-") as temporary:
            temporary_root = Path(temporary)
            plugin_root = temporary_root / "package" / "app-icon-toolkit"
            bin_directory = plugin_root / "bin"
            bin_directory.mkdir(parents=True)
            binary_name = (
                "app-icon-toolkit-mcp.exe"
                if os.name == "nt"
                else "app-icon-toolkit-mcp"
            )
            binary = bin_directory / binary_name
            binary.write_bytes(b"test executable fixture")
            binary.chmod(0o755)

            unrelated_cwd = temporary_root / "unrelated"
            unrelated_cwd.mkdir()
            shadow_binary = unrelated_cwd / binary_name
            shadow_binary.write_bytes(b"ambient executable fixture")
            shadow_binary.chmod(0o755)
            original_cwd = Path.cwd()
            try:
                os.chdir(unrelated_cwd)
                command, working_directory = (
                    smoke_installed_plugin.resolve_packaged_server_process(
                        plugin_root,
                        {
                            "command": "./bin/app-icon-toolkit-mcp",
                            "args": ["--probe"],
                            "cwd": ".",
                        },
                    )
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(Path(command[0]), binary.resolve())
            self.assertEqual(command[1:], ["--probe"])
            self.assertEqual(working_directory, plugin_root.resolve())

    def test_rejects_command_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="app-icon-command-test-") as temporary:
            plugin_root = Path(temporary) / "plugin"
            plugin_root.mkdir()
            with self.assertRaisesRegex(RuntimeError, "escapes the packaged plugin root"):
                smoke_installed_plugin.resolve_packaged_server_process(
                    plugin_root,
                    {"command": "../outside", "args": [], "cwd": "."},
                )

    def test_rejects_cwd_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="app-icon-command-test-") as temporary:
            plugin_root = Path(temporary) / "plugin"
            plugin_root.mkdir()
            with self.assertRaisesRegex(RuntimeError, "escapes the packaged plugin root"):
                smoke_installed_plugin.resolve_packaged_server_process(
                    plugin_root,
                    {"command": "./bin/server", "args": [], "cwd": ".."},
                )

if __name__ == "__main__":
    unittest.main()
