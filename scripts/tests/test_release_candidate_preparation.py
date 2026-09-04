"""Candidate extraction, signing, and packaging tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
from unittest import mock

from finalization_test_support import FinalizationTestCase
import release_attempt
import release_candidate_preparation as candidate
import release_files
import release_finalization_core as core
import release_targets


macos_signing = candidate.macos_signing


class ReleaseCandidatePreparationTests(FinalizationTestCase):
    def test_prepare_assets_uses_the_shared_archive_mapping(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-prepare-assets-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            downloads = root / "downloads"
            downloads.mkdir(mode=0o700)
            options = self.options(root, attempt)
            full_contract = release_targets.load_contract()
            target = next(
                item
                for item in full_contract.targets
                if item.family not in core.MACOS_FAMILIES
            )
            contract = release_targets.ReleaseContract(
                release_toolchain=full_contract.release_toolchain,
                macos_signing=full_contract.macos_signing,
                targets=(target,),
            )
            archive_name = target.release_filename(options.binding.tag)
            (downloads / archive_name).write_bytes(b"CI archive fixture")

            with mock.patch.object(
                candidate,
                "verify_release_assets",
            ), mock.patch.object(
                candidate,
                "archive_paths",
                wraps=candidate.archive_paths,
            ) as archive_mapping:
                assets, all_assets = candidate.prepare_assets(
                    options,
                    contract,
                    attempt,
                    downloads,
                    1_700_000_000,
                )

            archive_mapping.assert_called_once_with(
                assets,
                contract,
                options.binding.tag,
            )
            self.assertEqual(
                tuple(asset.name for asset in all_assets),
                ("SHA256SUMS", archive_name),
            )

    def test_signed_candidate_resumes_without_treating_signed_bytes_as_input(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-sign-resume-") as temporary:
            root = Path(temporary)
            plugin, contract, target, archive, target_root, binary = self.fixture(root)
            options = self.options(plugin, root / "attempt")
            original_sha256 = candidate.candidate_binary_sha256(
                options,
                target,
                archive,
            )

            def sign(*_args, **_kwargs):
                binary.write_bytes(b"Developer ID signed Mach-O fixture")
                binary.chmod(0o755)
                verified = self.verification(binary)
                return macos_signing.SigningReceipt(
                    input_sha256=original_sha256,
                    signed_sha256=verified.signed_sha256,
                    identity_sha1=verified.identity_sha1,
                    identifier=verified.identifier,
                    team_id=verified.team_id,
                    architectures=verified.architectures,
                    slices=verified.slices,
                )

            with mock.patch.object(
                candidate,
                "check_release_binary",
            ), mock.patch.object(
                macos_signing,
                "architectures",
                return_value=("arm64",),
            ), mock.patch.object(
                macos_signing,
                "inspect_pre_signatures",
                return_value=(
                    macos_signing.PreSignSlice(
                        "arm64", macos_signing.PreSignKind.UNSIGNED
                    ),
                ),
            ), mock.patch.object(
                macos_signing,
                "sign_and_verify",
                side_effect=sign,
            ) as sign_call, mock.patch.object(
                macos_signing,
                "verify_signed",
                side_effect=lambda *_args, **_kwargs: self.verification(binary),
            ) as verify_call:
                first = candidate.sign_candidate(
                    options,
                    contract,
                    target,
                    archive,
                    target_root,
                    binary,
                    object(),
                )
                second = candidate.sign_candidate(
                    options,
                    contract,
                    target,
                    archive,
                    target_root,
                    binary,
                    object(),
                )

            self.assertEqual(first, second)
            sign_call.assert_called_once()
            verify_call.assert_called_once()
            receipt = release_attempt.read_receipt(target_root / "signing.json")
            self.assertEqual(receipt["input_sha256"], original_sha256)
            self.assertNotEqual(receipt["input_sha256"], receipt["signed"]["signed_sha256"])

    def test_local_packager_argv_requires_static_only_verification(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-packager-mode-") as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            plugin.mkdir()
            binary = root / "app-icon-toolkit-mcp"
            binary.write_bytes(b"signed binary")
            assets = root / "assets"
            assets.mkdir()
            target = release_targets.load_contract().target("aarch64-apple-darwin")
            options = self.options(plugin, root / "attempt")

            with mock.patch.object(
                candidate,
                "required_command",
            ) as required_command:
                candidate._run_packager(
                    options,
                    target,
                    binary,
                    assets,
                    1_700_000_000,
                )

            command = required_command.call_args.args[0]
            self.assertEqual(
                command[command.index("--verification-mode") + 1],
                "static-only",
            )
            self.assertNotIn("smoke-installed-plugin.py", " ".join(command))

    def test_signed_archive_validation_never_executes_candidate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-static-validation-") as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            plugin.mkdir()
            package = root / "package"
            binary = package / "bin" / "app-icon-toolkit-mcp"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"signed binary")
            archive = root / "signed.zip"
            archive.write_bytes(b"archive fixture")
            contract = release_targets.load_contract()
            target = contract.target("aarch64-apple-darwin")
            options = self.options(plugin, root / "attempt")
            digest = release_files.sha256_file(binary)

            with mock.patch.object(
                candidate,
                "safe_extract_archive",
                return_value=package,
            ), mock.patch.object(
                candidate,
                "_validate_extracted_candidate",
                return_value=binary,
            ), mock.patch.object(
                candidate,
                "check_release_binary",
            ), mock.patch.object(
                macos_signing,
                "verify_signed",
                return_value=self.verification(binary),
            ), mock.patch.object(
                candidate,
                "required_command",
            ) as required_command:
                candidate.validate_signed_archive(
                    options,
                    contract,
                    target,
                    archive,
                    digest,
                    object(),
                )

            required_command.assert_not_called()

    def test_corrupt_candidate_archive_has_a_stable_finalizer_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-corrupt-archive-") as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            plugin.mkdir()
            contract = release_targets.load_contract()
            target = contract.target("aarch64-apple-darwin")
            archive = root / target.release_filename("v1.2.3")
            archive.write_bytes(b"not a zip archive")
            target_root = root / "target-work"
            target_root.mkdir(mode=0o700)
            options = self.options(plugin, root / "attempt")

            with self.assertRaisesRegex(
                core.FinalizationError,
                f"cannot extract downloaded candidate for {target.id}",
            ):
                candidate.extract_candidate(
                    options,
                    target,
                    archive,
                    target_root,
                )

            with self.assertRaisesRegex(
                core.FinalizationError,
                f"cannot extract downloaded candidate for {target.id}",
            ):
                candidate.extract_candidate(
                    options,
                    target,
                    archive,
                    target_root,
                )

            self.assertFalse((target_root / "candidate").exists())
            self.assertEqual(list((target_root / "candidate.partial").iterdir()), [])

            with self.assertRaisesRegex(
                core.FinalizationError,
                f"cannot inspect original candidate archive for {target.id}",
            ):
                candidate.candidate_binary_sha256(
                    options,
                    target,
                    archive,
                )

    def test_safe_partial_candidate_is_rebuilt_and_promoted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-partial-resume-") as temporary:
            root = Path(temporary)
            plugin, _contract, target, archive, target_root, _binary = self.fixture(root)
            candidate_root = target_root / "candidate"
            package = candidate_root / "app-icon-toolkit"
            for path in sorted(package.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                else:
                    path.rmdir()
            package.rmdir()
            candidate_root.rmdir()
            partial = target_root / "candidate.partial"
            partial_file = partial / "app-icon-toolkit" / "README.md"
            partial_file.parent.mkdir(parents=True)
            partial_file.write_bytes(b"interrupted")
            partial.chmod(0o700)
            options = self.options(plugin, root / "attempt")

            resumed_package, resumed_binary = candidate.extract_candidate(
                options,
                target,
                archive,
                target_root,
            )

            self.assertEqual(resumed_package, target_root / "candidate" / "app-icon-toolkit")
            self.assertTrue(resumed_binary.is_file())
            self.assertFalse(partial.exists())

    def test_signing_intent_preserves_an_invalid_candidate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-signed-state-preserve-") as temporary:
            root = Path(temporary)
            plugin, _contract, target, archive, target_root, binary = self.fixture(root)
            binary.write_bytes(b"interrupted signed candidate")
            release_attempt.write_receipt_no_replace(
                target_root,
                "signing-intent.json",
                {"target": target.id},
            )
            options = self.options(plugin, root / "attempt")

            with self.assertRaises(core.FinalizationError):
                with mock.patch.object(
                    candidate,
                    "_validate_extracted_candidate",
                    side_effect=core.FinalizationError("invalid signed candidate"),
                ):
                    candidate.extract_candidate(
                        options,
                        target,
                        archive,
                        target_root,
                    )

            self.assertEqual(binary.read_bytes(), b"interrupted signed candidate")
            self.assertTrue((target_root / "signing-intent.json").is_file())

    def test_crash_after_codesign_before_receipt_recovers_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-sign-crash-") as temporary:
            root = Path(temporary)
            plugin, contract, target, archive, target_root, binary = self.fixture(root)
            options = self.options(plugin, root / "attempt")
            original_sha256 = candidate.candidate_binary_sha256(
                options,
                target,
                archive,
            )
            real_ensure = candidate.ensure_receipt

            def sign(*_args, **_kwargs):
                binary.write_bytes(b"Developer ID signed after crash")
                binary.chmod(0o755)
                verified = self.verification(binary)
                return macos_signing.SigningReceipt(
                    input_sha256=original_sha256,
                    signed_sha256=verified.signed_sha256,
                    identity_sha1=verified.identity_sha1,
                    identifier=verified.identifier,
                    team_id=verified.team_id,
                    architectures=verified.architectures,
                    slices=verified.slices,
                )

            def fail_signing_receipt(root_path, name, payload):
                if name == "signing.json":
                    raise core.FinalizationError(
                        "injected crash before signing receipt"
                    )
                return real_ensure(root_path, name, payload)

            common = (
                mock.patch.object(candidate, "check_release_binary"),
                mock.patch.object(
                    macos_signing, "architectures", return_value=("arm64",)
                ),
                mock.patch.object(
                    macos_signing,
                    "inspect_pre_signatures",
                    return_value=(
                        macos_signing.PreSignSlice(
                            "arm64", macos_signing.PreSignKind.UNSIGNED
                        ),
                    ),
                ),
            )
            with common[0], common[1], common[2], mock.patch.object(
                macos_signing,
                "sign_and_verify",
                side_effect=sign,
            ) as sign_call, mock.patch.object(
                candidate,
                "ensure_receipt",
                side_effect=fail_signing_receipt,
            ):
                with self.assertRaisesRegex(
                    core.FinalizationError,
                    "injected crash",
                ):
                    candidate.sign_candidate(
                        options,
                        contract,
                        target,
                        archive,
                        target_root,
                        binary,
                        object(),
                    )

            self.assertTrue((target_root / "signing-intent.json").is_file())
            self.assertFalse((target_root / "signing.json").exists())
            with mock.patch.object(
                candidate,
                "check_release_binary",
            ), mock.patch.object(
                macos_signing,
                "verify_signed",
                return_value=self.verification(binary),
            ) as verify_call:
                recovered = candidate.sign_candidate(
                    options,
                    contract,
                    target,
                    archive,
                    target_root,
                    binary,
                    object(),
                )

            sign_call.assert_called_once()
            verify_call.assert_called_once()
            self.assertEqual(recovered.signed_sha256, self.verification(binary).signed_sha256)
            receipt = release_attempt.read_receipt(target_root / "signing.json")
            self.assertEqual(receipt["input_sha256"], original_sha256)
