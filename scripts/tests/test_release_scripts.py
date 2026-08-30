from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
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


class _CompletedProcessFixture:
    def __init__(self) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def wait(self, timeout: float) -> int:
        return 0

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        raise AssertionError("completed process must not be killed")


class _TimedOutProcessFixture(_CompletedProcessFixture):
    def __init__(self, stdin: io.StringIO | None = None) -> None:
        super().__init__()
        if stdin is not None:
            self.stdin = stdin
        self.killed = False
        self.wait_calls = 0

    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired(["fixture-server"], timeout)
        return -9

    def poll(self) -> int | None:
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True


class _BrokenPipeStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.close_attempts = 0

    def close(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise BrokenPipeError("fixture broken pipe")
        super().close()


class _StoppedThreadFixture:
    def join(self, timeout: float) -> None:
        return None

    def is_alive(self) -> bool:
        return False


class _AliveThreadFixture(_StoppedThreadFixture):
    def is_alive(self) -> bool:
        return True


class McpProcessLifecycleTests(unittest.TestCase):
    def make_process(
        self,
        stderr: str = "",
        process_fixture: _CompletedProcessFixture | None = None,
    ):
        process = object.__new__(smoke_installed_plugin.McpProcess)
        process._process = process_fixture or _CompletedProcessFixture()
        process._reader = _StoppedThreadFixture()
        process._stderr_reader = _StoppedThreadFixture()
        process._stderr_capture = stderr
        process._stderr_truncated = False
        process._stderr_overlap = ""
        process._stderr_has_ansi = False
        process._stderr_has_disallowed_diagnostic = bool(
            smoke_installed_plugin.DISALLOWED_DIAGNOSTIC.search(stderr)
        )
        process._stderr_error = None
        process._seen = [{"jsonrpc": "2.0", "id": 1}]
        process._closed = False
        return process

    def test_close_releases_every_process_stream(self) -> None:
        process = self.make_process("INFO server stopped cleanly")

        process.close()

        self.assertTrue(process._process.stdin.closed)
        self.assertTrue(process._process.stdout.closed)
        self.assertTrue(process._process.stderr.closed)
        process.close()

    def test_close_rejects_warning_and_still_releases_streams(self) -> None:
        diagnostics = (
            "WARN obsolete API",
            "warning: obsolete API",
            "DeprecationWarning: obsolete API",
            "[DEP0005] deprecated API",
            "ERROR release smoke failed",
        )
        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                process = self.make_process(diagnostic)

                with self.assertRaisesRegex(
                    smoke_installed_plugin.McpProcessFailure,
                    "error or warning",
                ):
                    process.close()

                self.assertTrue(process._process.stdin.closed)
                self.assertTrue(process._process.stdout.closed)
                self.assertTrue(process._process.stderr.closed)

    def test_timeout_kills_reaps_and_closes_every_stream(self) -> None:
        process_fixture = _TimedOutProcessFixture()
        process = self.make_process(process_fixture=process_fixture)

        with self.assertRaisesRegex(
            smoke_installed_plugin.McpProcessFailure,
            "wait for graceful shutdown",
        ):
            process.close()

        self.assertTrue(process_fixture.killed)
        self.assertEqual(process_fixture.wait_calls, 2)
        self.assertTrue(process_fixture.stdin.closed)
        self.assertTrue(process_fixture.stdout.closed)
        self.assertTrue(process_fixture.stderr.closed)

    def test_broken_stdin_still_kills_reaps_and_closes_every_stream(self) -> None:
        stdin = _BrokenPipeStream()
        process_fixture = _TimedOutProcessFixture(stdin=stdin)
        process = self.make_process(process_fixture=process_fixture)

        with self.assertRaisesRegex(
            smoke_installed_plugin.McpProcessFailure,
            "fixture broken pipe",
        ):
            process.close()

        self.assertTrue(process_fixture.killed)
        self.assertEqual(process_fixture.wait_calls, 2)
        self.assertTrue(process_fixture.stdin.closed)
        self.assertTrue(process_fixture.stdout.closed)
        self.assertTrue(process_fixture.stderr.closed)

    def test_reader_failures_are_reported_after_stream_cleanup(self) -> None:
        process = self.make_process()
        process._stderr_reader = _AliveThreadFixture()
        process._stderr_error = OSError("fixture stderr read failure")

        with self.assertRaisesRegex(
            smoke_installed_plugin.McpProcessFailure,
            "reader did not stop.*fixture stderr read failure",
        ):
            process.close()

        self.assertTrue(process._process.stdin.closed)
        self.assertTrue(process._process.stdout.closed)
        self.assertTrue(process._process.stderr.closed)

    def test_primary_and_shutdown_failures_are_both_preserved(self) -> None:
        process = self.make_process("WARNING shutdown diagnostic")
        primary = RuntimeError("primary protocol failure")

        with self.assertRaises(ExceptionGroup) as captured:
            with process:
                raise primary

        self.assertIs(captured.exception.exceptions[0], primary)
        self.assertIsInstance(
            captured.exception.exceptions[1],
            smoke_installed_plugin.McpProcessFailure,
        )

    def test_large_stderr_is_drained_without_unbounded_capture(self) -> None:
        process = object.__new__(smoke_installed_plugin.McpProcess)
        process._process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('INFO ' + 'x' * 1048576)",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        process._start_readers()
        process._seen.append({"jsonrpc": "2.0", "id": 1})

        process.close()

        self.assertTrue(process._stderr_truncated)
        self.assertEqual(
            len(process._stderr_capture),
            smoke_installed_plugin.STDERR_CAPTURE_LIMIT,
        )

if __name__ == "__main__":
    unittest.main()
