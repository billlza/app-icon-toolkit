from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from release_test_support import (
    SCRIPTS_ROOT,
    build_release_binary,
    check_release_binary,
    check_release_version,
    install_release_toolchain,
    prepare_release_package,
    release_targets,
)


class ReleaseVersionTests(unittest.TestCase):
    @staticmethod
    def cargo_metadata(
        versions: tuple[str, ...] = ("0.2.1", "0.2.1"),
    ) -> subprocess.CompletedProcess[str]:
        packages = [
            {"id": f"package-{index}", "version": version}
            for index, version in enumerate(versions)
        ]
        metadata = {
            "workspace_members": [package["id"] for package in packages],
            "packages": packages,
        }
        return subprocess.CompletedProcess(
            args=["cargo", "metadata"],
            returncode=0,
            stdout=json.dumps(metadata),
            stderr="",
        )

    def test_workspace_version_comes_from_all_cargo_members(self) -> None:
        with mock.patch.object(
            check_release_version.subprocess,
            "run",
            return_value=self.cargo_metadata(),
        ) as run:
            version = check_release_version.load_workspace_version(Path("/workspace"))

        self.assertEqual(version, "0.2.1")
        command = run.call_args.args[0]
        self.assertEqual(command[0:2], ["cargo", "metadata"])
        self.assertIn("--locked", command)
        self.assertIn("--no-deps", command)
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            check_release_version.CARGO_METADATA_TIMEOUT_SECONDS,
        )

    def test_workspace_version_rejects_drift_and_cargo_failure(self) -> None:
        cases = [
            (self.cargo_metadata(("0.2.0", "0.2.1")), "versions disagree"),
            (
                subprocess.CompletedProcess(
                    args=["cargo", "metadata"],
                    returncode=101,
                    stdout="",
                    stderr="manifest is invalid",
                ),
                "Cargo metadata failed.*manifest is invalid",
            ),
            (self.cargo_metadata(()), "no workspace members"),
        ]
        for completed, message in cases:
            with self.subTest(message=message):
                with mock.patch.object(
                    check_release_version.subprocess,
                    "run",
                    return_value=completed,
                ):
                    with self.assertRaisesRegex(RuntimeError, message):
                        check_release_version.load_workspace_version(Path("/workspace"))

        with mock.patch.object(
            check_release_version.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["cargo", "metadata"], 60),
        ):
            with self.assertRaisesRegex(RuntimeError, "exceeded 60 seconds"):
                check_release_version.load_workspace_version(Path("/workspace"))

    def test_workspace_version_rejects_malformed_cargo_metadata(self) -> None:
        cases = [
            (
                subprocess.CompletedProcess(
                    args=["cargo", "metadata"],
                    returncode=0,
                    stdout="{",
                    stderr="",
                ),
                "invalid JSON",
            ),
            (
                subprocess.CompletedProcess(
                    args=["cargo", "metadata"],
                    returncode=0,
                    stdout=json.dumps(
                        {"workspace_members": ["missing"], "packages": []}
                    ),
                    stderr="",
                ),
                "omitted workspace members.*missing",
            ),
            (
                subprocess.CompletedProcess(
                    args=["cargo", "metadata"],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "workspace_members": ["duplicate"],
                            "packages": [
                                {"id": "duplicate", "version": "0.2.1"},
                                {"id": "duplicate", "version": "0.2.1"},
                            ],
                        }
                    ),
                    stderr="",
                ),
                "repeated package id.*duplicate",
            ),
        ]
        for completed, message in cases:
            with self.subTest(message=message):
                with mock.patch.object(
                    check_release_version.subprocess,
                    "run",
                    return_value=completed,
                ):
                    with self.assertRaisesRegex(RuntimeError, message):
                        check_release_version.load_workspace_version(Path("/workspace"))

        with mock.patch.object(
            check_release_version.subprocess,
            "run",
            side_effect=OSError("cargo executable is unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed to run Cargo metadata"):
                check_release_version.load_workspace_version(Path("/workspace"))

    def test_release_version_checks_plugin_and_changelog_surfaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-version-test-") as temporary:
            root = Path(temporary)
            (root / ".codex-plugin").mkdir()
            (root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"version": "0.2.1+installation-test"}),
                encoding="utf-8",
            )
            (root / "CHANGELOG.md").write_text(
                "## 0.2.1 - 2026-08-30\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                check_release_version,
                "load_workspace_version",
                return_value="0.2.1",
            ):
                check_release_version.verify_release_version("v0.2.1", root)

                (root / ".codex-plugin" / "plugin.json").write_text(
                    json.dumps({"version": "0.2.0"}),
                    encoding="utf-8",
                )
                (root / "CHANGELOG.md").write_text(
                    "## 0.2.0 - 2026-08-30\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    SystemExit,
                    "Cargo workspace version is 0.2.1.*"
                    "plugin manifest version is 0.2.0.*"
                    "changelog must contain exactly one dated release heading",
                ):
                    check_release_version.verify_release_version("v0.3.0", root)

                (root / ".codex-plugin" / "plugin.json").write_text(
                    json.dumps({"version": "0.2.1"}),
                    encoding="utf-8",
                )
                (root / "CHANGELOG.md").write_text(
                    "## 0.2.1 - 2026-08-30\n## 0.2.1 - 2026-08-31\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(SystemExit, "exactly one"):
                    check_release_version.verify_release_version("v0.2.1", root)

                (root / "CHANGELOG.md").write_text(
                    "## 0.2.1 - 2026-99-99\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(SystemExit, "invalid date"):
                    check_release_version.verify_release_version("v0.2.1", root)


class ReleaseTargetContractTests(unittest.TestCase):
    def test_contract_is_the_complete_release_inventory(self) -> None:
        contract = release_targets.load_contract()
        expected_ids = [
            "x86_64-unknown-linux-gnu",
            "x86_64-unknown-linux-musl",
            "aarch64-unknown-linux-musl",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
            "universal2-apple-darwin",
            "x86_64-pc-windows-msvc",
            "aarch64-pc-windows-msvc",
        ]

        self.assertEqual(contract.release_toolchain, "1.97.1")
        self.assertEqual([target.id for target in contract.targets], expected_ids)
        self.assertEqual(
            [entry["id"] for entry in (target.matrix_entry() for target in contract.targets)],
            expected_ids,
        )
        self.assertEqual(
            [target.id for target in contract.targets if target.codex_install_verify],
            [
                "x86_64-unknown-linux-gnu",
                "aarch64-apple-darwin",
                "x86_64-pc-windows-msvc",
            ],
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

        invalid_install_gate = copy.deepcopy(contract_value)
        invalid_install_gate["targets"][0]["codex_install_verify"] = "yes"
        cases.append((invalid_install_gate, "codex_install_verify.*boolean"))

        missing_install_host = copy.deepcopy(contract_value)
        missing_install_host["targets"][3]["codex_install_verify"] = False
        cases.append((missing_install_host, "exactly one target each"))

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

    def test_rust_targets_cli_matches_the_validated_contract(self) -> None:
        contract = release_targets.load_contract()
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS_ROOT / "release_targets.py"), "rust-targets"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.stdout.splitlines(), list(contract.rust_targets()))

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

    def test_codex_install_matrix_comes_from_the_target_contract(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "release_targets.py"),
                "codex-install-matrix",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        matrix = json.loads(completed.stdout)
        self.assertEqual(
            [entry["id"] for entry in matrix["include"]],
            [
                "x86_64-unknown-linux-gnu",
                "aarch64-apple-darwin",
                "x86_64-pc-windows-msvc",
            ],
        )
        self.assertTrue(
            all(entry["codex_install_verify"] for entry in matrix["include"])
        )


class HostCompatibilityContractTests(unittest.TestCase):
    def test_codex_host_test_version_is_exact_and_packaged(self) -> None:
        version_path = SCRIPTS_ROOT.parent / "CODEX_HOST_TEST_VERSION"
        version = version_path.read_text(encoding="utf-8").strip()

        self.assertIsNotNone(release_targets.TOOLCHAIN.fullmatch(version))
        self.assertIn(
            Path("CODEX_HOST_TEST_VERSION"), prepare_release_package.STATIC_PATHS
        )


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
        contract["schema_version"] = 3
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


if __name__ == "__main__":
    unittest.main()
