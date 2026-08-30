from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]


def load_script(module_name: str, filename: str):
    specification = importlib.util.spec_from_file_location(
        module_name, SCRIPTS_ROOT / filename
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {filename}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


release_targets = load_script("release_targets", "release_targets.py")
prepare_release_package = load_script(
    "prepare_release_package", "prepare-release-package.py"
)
check_release_binary = load_script(
    "check_release_binary", "check-release-binary.py"
)
build_release_binary = load_script(
    "build_release_binary", "build-release-binary.py"
)
install_release_toolchain = load_script(
    "install_release_toolchain", "install-release-toolchain.py"
)
smoke_installed_plugin = load_script(
    "smoke_installed_plugin", "smoke-installed-plugin.py"
)


class ReleaseTargetContractTests(unittest.TestCase):
    def test_contract_is_the_complete_release_inventory(self) -> None:
        contract = release_targets.load_contract()

        self.assertEqual(contract.release_toolchain, "1.97.1")
        self.assertEqual(
            [entry["id"] for entry in (target.matrix_entry() for target in contract.targets)],
            [target.id for target in contract.targets],
        )
        self.assertEqual(len({target.id for target in contract.targets}), len(contract.targets))
        universal = contract.target("universal2-apple-darwin")
        self.assertEqual(
            set(universal.rust_targets),
            {"aarch64-apple-darwin", "x86_64-apple-darwin"},
        )
        self.assertIsNone(universal.test_target)
        self.assertEqual(universal.native_verify_runner, "macos-26-intel")

    def test_rejects_unknown_fields_and_duplicate_ids(self) -> None:
        source = Path(release_targets.CONTRACT_PATH).read_text(encoding="utf-8")
        contract_value = json.loads(source)
        cases = []

        unknown = copy.deepcopy(contract_value)
        unknown["targets"][0]["unreviewed"] = True
        cases.append((unknown, "unknown fields"))

        duplicate = copy.deepcopy(contract_value)
        duplicate["targets"].append(copy.deepcopy(duplicate["targets"][0]))
        cases.append((duplicate, "duplicate target ids"))

        traversal = copy.deepcopy(contract_value)
        traversal["targets"][0]["binary_name"] = "../app-icon-toolkit-mcp"
        cases.append((traversal, "binary_name"))

        mismatched_id = copy.deepcopy(contract_value)
        mismatched_id["targets"][0]["id"] = "custom-linux-release"
        cases.append((mismatched_id, "id must equal"))

        mismatched_runner = copy.deepcopy(contract_value)
        mismatched_runner["targets"][2]["runner"] = "ubuntu-24.04"
        cases.append((mismatched_runner, "runner architecture"))

        missing_minimum = copy.deepcopy(contract_value)
        missing_minimum["targets"][3].pop("macos_minimum")
        cases.append((missing_minimum, "macos_minimum"))

        wrong_python = copy.deepcopy(contract_value)
        wrong_python["targets"][-1]["python"] = "python3"
        cases.append((wrong_python, "python"))

        for value, message in cases:
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory(
                    prefix="release-contract-test-"
                ) as temporary:
                    path = Path(temporary) / "contract.json"
                    path.write_text(
                        json.dumps(value), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(RuntimeError, message):
                        release_targets.load_contract(path)

    def test_release_asset_verification_is_exact(self) -> None:
        contract = release_targets.load_contract()
        tag = "v9.8.7"
        with tempfile.TemporaryDirectory(prefix="release-assets-test-") as temporary:
            directory = Path(temporary)
            for target in contract.targets:
                (directory / target.release_filename(tag)).write_bytes(b"archive")

            release_targets.verify_release_assets(contract, directory, tag)
            extra = directory / "unexpected.tar.gz"
            extra.write_bytes(b"unexpected")
            with self.assertRaisesRegex(RuntimeError, "extra=.*unexpected"):
                release_targets.verify_release_assets(contract, directory, tag)

            extra.unlink()
            first = directory / contract.targets[0].release_filename(tag)
            first.write_bytes(b"")
            with self.assertRaisesRegex(RuntimeError, "non-empty"):
                release_targets.verify_release_assets(contract, directory, tag)

    def test_about_targets_must_match_every_rust_triple(self) -> None:
        contract = release_targets.load_contract()
        with tempfile.TemporaryDirectory(prefix="about-targets-test-") as temporary:
            path = Path(temporary) / "about.toml"
            values = ", ".join(
                f'"{target}"' for target in contract.rust_targets()
            )
            path.write_text(f"targets = [{values}]\n", encoding="utf-8")
            release_targets.verify_about_targets(contract, path)

            path.write_text('targets = ["x86_64-unknown-linux-gnu"]\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "expected"):
                release_targets.verify_about_targets(contract, path)

    def test_contract_cli_is_independent_of_the_process_working_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-cli-cwd-") as temporary:
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS_ROOT / "release_targets.py"), "matrix"],
                cwd=temporary,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        matrix = json.loads(completed.stdout)
        self.assertEqual(
            [entry["id"] for entry in matrix["include"]],
            [target.id for target in release_targets.load_contract().targets],
        )

    def test_target_details_cli_uses_the_validated_contract(self) -> None:
        target_id = "aarch64-pc-windows-msvc"
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "release_targets.py"),
                "target-details",
                "--target",
                target_id,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        details = json.loads(completed.stdout)
        self.assertEqual(details["id"], target_id)
        self.assertEqual(details["family"], "windows_msvc")
        self.assertEqual(details["test_target"], target_id)

        rejected = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "release_targets.py"),
                "target-details",
                "--target",
                "unapproved-pc-windows-msvc",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unsupported release target", rejected.stderr)


class InstalledBinaryNameTests(unittest.TestCase):
    def test_maps_every_release_target(self) -> None:
        contract = release_targets.load_contract()
        for target in contract.targets:
            with self.subTest(target=target.id):
                self.assertEqual(
                    prepare_release_package.installed_binary_name(target.id),
                    target.binary_name,
                )

    def test_rejects_an_unapproved_release_target(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported release target"):
            prepare_release_package.installed_binary_name("i686-pc-windows-msvc")

    def test_uses_the_selected_plugin_roots_contract(self) -> None:
        contract = json.loads(Path(release_targets.CONTRACT_PATH).read_text(encoding="utf-8"))
        contract["schema_version"] = 2
        with tempfile.TemporaryDirectory(prefix="alternate-plugin-root-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "release-targets.json").write_text(
                json.dumps(contract), encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "schema_version"):
                prepare_release_package.installed_binary_name(
                    "x86_64-unknown-linux-gnu", root
                )

    def test_archive_format_must_match_the_contract(self) -> None:
        target = release_targets.load_contract().target("x86_64-pc-windows-msvc")
        prepare_release_package.validate_archive_format(target, "zip")
        with self.assertRaisesRegex(RuntimeError, "requires zip"):
            prepare_release_package.validate_archive_format(target, "tar.gz")


class ReleaseBinaryInspectionTests(unittest.TestCase):
    def test_windows_imports_reject_dynamic_crt_case_insensitively(self) -> None:
        allowed = "Import {\n  Name: KERNEL32.dll\n}\nImport {\n  Name: bcrypt.dll\n}"
        self.assertEqual(
            check_release_binary.parse_windows_imports(allowed),
            {"KERNEL32.dll", "bcrypt.dll"},
        )

        for name in (
            "VCRUNTIME140.dll",
            "msvcp140.DLL",
            "ucrtbase.dll",
            "api-ms-win-crt-runtime-l1-1-0.dll",
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(RuntimeError, "dynamically imports"):
                    check_release_binary.parse_windows_imports(f"Name: {name}\n")

    def test_machine_fields_must_match_the_requested_architecture(self) -> None:
        check_release_binary.assert_windows_machine(
            "Machine: IMAGE_FILE_MACHINE_ARM64 (0xAA64)\n", "aarch64"
        )
        check_release_binary.assert_elf_machine("Machine: AArch64\n", "aarch64")
        with self.assertRaisesRegex(RuntimeError, "expected IMAGE_FILE_MACHINE_ARM64"):
            check_release_binary.assert_windows_machine(
                "Machine: IMAGE_FILE_MACHINE_AMD64 (0x8664)\n", "aarch64"
            )
        with self.assertRaisesRegex(RuntimeError, "expected AArch64"):
            check_release_binary.assert_elf_machine(
                "Machine: Advanced Micro Devices X86-64\n", "aarch64"
            )

    def test_glibc_versions_use_numeric_order_and_reject_private_symbols(self) -> None:
        versions = check_release_binary.parse_glibc_versions(
            "Name: GLIBC_2.9\nName: GLIBC_2.34\n", "2.34"
        )
        self.assertEqual(versions, {(2, 9), (2, 34)})

        with self.assertRaisesRegex(RuntimeError, "above 2.34"):
            check_release_binary.parse_glibc_versions("GLIBC_2.35", "2.34")
        with self.assertRaisesRegex(RuntimeError, "GLIBC_PRIVATE"):
            check_release_binary.parse_glibc_versions("GLIBC_PRIVATE GLIBC_2.34", "2.34")

    def test_musl_requires_no_interpreter_or_needed_entries(self) -> None:
        check_release_binary.assert_static_musl(
            "Program Headers:\n  LOAD 0x000000\n",
            "There is no dynamic section in this file.\n",
        )
        with self.assertRaisesRegex(RuntimeError, "INTERP"):
            check_release_binary.assert_static_musl(
                "  INTERP 0x000000\n", "There is no dynamic section\n"
            )
        with self.assertRaisesRegex(RuntimeError, "NEEDED"):
            check_release_binary.assert_static_musl(
                "  LOAD 0x000000\n", "0x0000000000000001 (NEEDED) Shared library\n"
            )

    def test_macos_architecture_set_is_exact(self) -> None:
        check_release_binary.assert_macos_architectures(
            "x86_64 arm64\n", {"arm64", "x86_64"}
        )
        with self.assertRaisesRegex(RuntimeError, "expected"):
            check_release_binary.assert_macos_architectures("arm64\n", {"arm64", "x86_64"})

    def test_macos_minimum_is_checked_per_slice(self) -> None:
        check_release_binary.assert_macos_minimum(
            "Load command 10\n      minos 13.0\n", "13.0", "arm64"
        )
        with self.assertRaisesRegex(RuntimeError, "minimum is 14.0"):
            check_release_binary.assert_macos_minimum(
                "      minos 14.0\n", "13.0", "x86_64"
            )

    def test_binary_symlink_is_rejected_before_native_inspection(self) -> None:
        contract = release_targets.load_contract()
        target = contract.target("x86_64-unknown-linux-gnu")
        with tempfile.TemporaryDirectory(prefix="binary-symlink-test-") as temporary:
            root = Path(temporary)
            binary = root / "binary"
            binary.write_bytes(b"ELF fixture")
            link = root / "link"
            try:
                link.symlink_to(binary.name)
            except OSError as error:
                self.skipTest(f"symlink creation is unavailable: {error}")
            with self.assertRaisesRegex(RuntimeError, "non-symlink"):
                check_release_binary.check_binary(contract, target, link)


class ReleaseCandidatePublicationTests(unittest.TestCase):
    def test_concurrent_publish_never_replaces_the_winner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="candidate-publish-test-") as temporary:
            root = Path(temporary)
            destination = root / "candidate"
            sources = []
            for index in range(8):
                source = root / f"source-{index}"
                source.write_text(str(index), encoding="utf-8")
                sources.append(source)
            barrier = threading.Barrier(len(sources) + 1)
            outcomes: list[str] = []
            outcome_lock = threading.Lock()

            def publish(source: Path) -> None:
                barrier.wait()
                try:
                    build_release_binary.publish_candidate_no_replace(
                        source, destination
                    )
                    outcome = "published"
                except RuntimeError:
                    outcome = "collision"
                with outcome_lock:
                    outcomes.append(outcome)

            workers = [threading.Thread(target=publish, args=(source,)) for source in sources]
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join()

            self.assertEqual(outcomes.count("published"), 1)
            self.assertEqual(outcomes.count("collision"), len(sources) - 1)
            self.assertIn(destination.read_text(encoding="utf-8"), {str(i) for i in range(8)})

    def test_thin_macos_build_uses_pinned_target_and_contract_minimum(self) -> None:
        contract = release_targets.load_contract()
        target = contract.target("aarch64-apple-darwin")
        with tempfile.TemporaryDirectory(prefix="thin-candidate-build-") as temporary:
            root = Path(temporary)
            destination = root / "candidate" / target.binary_name
            calls: list[tuple[str, str | None]] = []

            def fake_build(
                build_root: Path,
                _contract,
                rust_target: str,
                environment: dict[str, str],
            ) -> None:
                calls.append((rust_target, environment.get("MACOSX_DEPLOYMENT_TARGET")))
                binary = build_release_binary.cargo_binary(
                    build_root, _contract, rust_target, target.binary_name
                )
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(b"thin release binary")

            with mock.patch.object(build_release_binary, "run_build", side_effect=fake_build):
                build_release_binary.build_candidate(root, contract, target, destination)

            self.assertEqual(calls, [("aarch64-apple-darwin", "13.0")])
            self.assertEqual(destination.read_bytes(), b"thin release binary")

    def test_macos_build_uses_contract_minimum_and_cleans_lipo_failure(self) -> None:
        contract = release_targets.load_contract()
        target = contract.target("universal2-apple-darwin")
        with tempfile.TemporaryDirectory(prefix="candidate-build-test-") as temporary:
            root = Path(temporary)
            destination = root / "candidate" / target.binary_name

            def fake_build(
                build_root: Path,
                _contract,
                rust_target: str,
                environment: dict[str, str],
            ) -> None:
                self.assertEqual(environment.get("MACOSX_DEPLOYMENT_TARGET"), "13.0")
                binary = build_release_binary.cargo_binary(
                    build_root, _contract, rust_target, target.binary_name
                )
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(rust_target.encode("utf-8"))

            with mock.patch.object(build_release_binary, "run_build", side_effect=fake_build), mock.patch.object(
                build_release_binary.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, ["xcrun", "lipo"]),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    build_release_binary.build_candidate(root, contract, target, destination)

            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.glob("*.tmp")), [])


class ReleaseToolchainInstallTests(unittest.TestCase):
    def test_installs_every_slice_and_only_windows_llvm_tools(self) -> None:
        contract = release_targets.load_contract()
        for target_id, expected_component_calls in (
            ("universal2-apple-darwin", 0),
            ("aarch64-pc-windows-msvc", 1),
        ):
            target = contract.target(target_id)
            with self.subTest(target=target_id), mock.patch.object(
                install_release_toolchain.subprocess, "run"
            ) as run:
                install_release_toolchain.install(contract, target)

                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(
                    commands[0],
                    [
                        "rustup",
                        "toolchain",
                        "install",
                        contract.release_toolchain,
                        "--profile",
                        "minimal",
                    ],
                )
                self.assertEqual(
                    [command[-1] for command in commands[1 : 1 + len(target.rust_targets)]],
                    list(target.rust_targets),
                )
                component_calls = [
                    command for command in commands if command[1:3] == ["component", "add"]
                ]
                self.assertEqual(len(component_calls), expected_component_calls)
                for call in run.call_args_list:
                    self.assertTrue(call.kwargs.get("check"))


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
