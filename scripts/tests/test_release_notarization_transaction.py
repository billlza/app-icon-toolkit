"""Notarization submission and adoption transaction tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from unittest import mock

from finalization_test_support import FinalizationTestCase
import release_attempt
import release_files
import release_finalization_core as core
import release_notarization_transaction as notarization
import release_targets


macos_signing = notarization.macos_signing


class ReleaseNotarizationTransactionTests(FinalizationTestCase):
    def test_existing_notary_intent_without_job_id_never_blindly_resubmits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-notary-unknown-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            plugin = root / "plugin"
            plugin.mkdir(mode=0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            assets = attempt / "assets"
            assets.mkdir(mode=0o700)
            release_attempt.write_receipt_no_replace(attempt, "prepared.json", {"ok": True})
            contract = release_targets.load_contract()
            target = next(
                item
                for item in contract.targets
                if item.family in core.MACOS_FAMILIES
            )
            archive = assets / target.release_filename("v1.2.3")
            archive.write_bytes(b"signed archive awaiting notarization")
            work = release_attempt.private_subdirectory(attempt, "macos-work")
            target_root = release_attempt.private_subdirectory(work, target.id)
            release_attempt.write_receipt_no_replace(
                target_root,
                "notary-submission-intent.json",
                {
                    "target": target.id,
                    "archive_sha256": release_files.sha256_file(archive),
                },
            )
            options = self.options(plugin, attempt)

            with mock.patch.object(
                macos_signing,
                "submit_notarization",
            ) as submit:
                with self.assertRaisesRegex(
                    core.ExternalMutationOutcomeUnknown,
                    "reconcile Apple history",
                ):
                    notarization.notarize_assets(
                        options,
                        contract,
                        attempt,
                        assets,
                    )
            submit.assert_not_called()

    def test_adoption_without_existing_intent_never_submits(self) -> None:
        adopted_job_id = "11111111-1111-4111-8111-111111111111"
        with tempfile.TemporaryDirectory(prefix="finalizer-adopt-no-intent-") as temporary:
            root = Path(temporary)
            (
                attempt,
                assets,
                target_root,
                options,
                contract,
                target,
                _archive_sha256,
            ) = self.notary_adoption_fixture(root, create_intent=False)
            options = replace(
                options,
                adopted_submissions={target.id: adopted_job_id},
            )

            with mock.patch.object(
                macos_signing,
                "submit_notarization",
            ) as submit, self.assertRaisesRegex(
                core.FinalizationError,
                "no existing submission intent",
            ):
                notarization.notarize_assets(
                    options,
                    contract,
                    attempt,
                    assets,
                )

            submit.assert_not_called()
            self.assertFalse(
                (target_root / "notary-submission-intent.json").exists()
            )

    def test_adoption_must_match_existing_submission_receipt(self) -> None:
        existing_job_id = "11111111-1111-4111-8111-111111111111"
        different_job_id = "22222222-2222-4222-8222-222222222222"
        with tempfile.TemporaryDirectory(prefix="finalizer-adopt-mismatch-") as temporary:
            root = Path(temporary)
            (
                attempt,
                assets,
                _target_root,
                options,
                contract,
                target,
                _archive_sha256,
            ) = self.notary_adoption_fixture(
                root,
                create_intent=True,
                submission_job_id=existing_job_id,
            )
            options = replace(
                options,
                adopted_submissions={target.id: different_job_id},
            )

            with mock.patch.object(
                macos_signing,
                "submit_notarization",
            ) as submit, mock.patch.object(
                macos_signing,
                "verify_accepted_notarization",
            ) as verify, self.assertRaisesRegex(
                core.FinalizationError,
                "differs from the existing job ID",
            ):
                notarization.notarize_assets(
                    options,
                    contract,
                    attempt,
                    assets,
                )

            submit.assert_not_called()
            verify.assert_not_called()

    def test_adoption_is_exactly_consumed_without_new_submission(self) -> None:
        adopted_job_id = "11111111-1111-4111-8111-111111111111"
        for existing_submission in (None, adopted_job_id):
            with self.subTest(existing_submission=existing_submission):
                with tempfile.TemporaryDirectory(
                    prefix="finalizer-adopt-consumed-"
                ) as temporary:
                    root = Path(temporary)
                    (
                        attempt,
                        assets,
                        target_root,
                        options,
                        contract,
                        target,
                        archive_sha256,
                    ) = self.notary_adoption_fixture(
                        root,
                        create_intent=True,
                        submission_job_id=existing_submission,
                    )
                    options = replace(
                        options,
                        adopted_submissions={target.id: adopted_job_id},
                    )
                    accepted = macos_signing.NotarizationReceipt(
                        job_id=adopted_job_id,
                        archive_sha256=archive_sha256,
                        status="Accepted",
                    )

                    with mock.patch.object(
                        macos_signing,
                        "submit_notarization",
                    ) as submit, mock.patch.object(
                        macos_signing,
                        "verify_accepted_notarization",
                        return_value=accepted,
                    ) as verify, mock.patch.object(
                        macos_signing,
                        "check_notarization_ticket",
                    ):
                        notarization.notarize_assets(
                            options,
                            contract,
                            attempt,
                            assets,
                        )

                    submit.assert_not_called()
                    verify.assert_called_once()
                    self.assertEqual(
                        release_attempt.read_receipt(
                            target_root / "notary-submission.json"
                        )["job_id"],
                        adopted_job_id,
                    )
                    self.assertTrue((attempt / "notarized.json").is_file())
