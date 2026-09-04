"""Anonymous public-release acceptance transaction tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
from unittest import mock

from finalization_test_support import FinalizationTestCase
import release_attempt
import release_draft
import release_git_tag
import release_public_acceptance
import release_public_acceptance_transaction as acceptance
import release_targets


class ReleasePublicAcceptanceTransactionTests(FinalizationTestCase):
    def test_public_acceptance_is_anonymous_and_persisted_after_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-public-acceptance-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            options = self.options(root, attempt)
            run = release_draft.WorkflowRun(
                workflow_id=options.binding.workflow_database_id,
                run_id=options.binding.run_id,
                attempt=options.binding.run_attempt,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
            publication_receipt = release_draft.PublicationReceipt(
                repository=options.repository,
                release_id="R_public",
                release_database_id=789,
                tag=run.tag,
                head_sha=run.head_sha,
                workflow_id=run.workflow_id,
                run_id=run.run_id,
                run_attempt=run.attempt,
                reconciled_after_unknown_mutation=False,
            )
            release_attempt.write_receipt_no_replace(
                attempt,
                "published.json",
                {
                    "repository": publication_receipt.repository,
                    "release_id": publication_receipt.release_id,
                    "release_database_id": publication_receipt.release_database_id,
                    "tag": publication_receipt.tag,
                    "head_sha": publication_receipt.head_sha,
                    "workflow_id": publication_receipt.workflow_id,
                    "run_id": publication_receipt.run_id,
                    "run_attempt": publication_receipt.run_attempt,
                    "reconciled_after_unknown_mutation": False,
                },
            )
            hosted_receipt = {"verified": True}
            release_attempt.write_receipt_no_replace(
                attempt,
                "hosted-validation-verified.json",
                hosted_receipt,
            )
            remote_tag = release_git_tag.RemoteTagBinding(
                tag=publication_receipt.tag,
                tag_object_sha="1" * 40,
                commit_sha=publication_receipt.head_sha,
            )
            asset_path = attempt / "candidate.zip"
            asset_path.write_bytes(b"published candidate")
            assets = release_draft.snapshot_local_assets(
                {asset_path.name: asset_path},
                expected_names=(asset_path.name,),
            )
            notes = "release notes\n"
            public_receipt = self.public_acceptance_receipt(
                options,
                publication_receipt,
                notes,
            )

            with mock.patch.object(
                release_public_acceptance,
                "verify_public_release",
                return_value=public_receipt,
            ) as verify:
                observed = acceptance.accept_public_release(
                    options,
                    release_targets.load_contract(),
                    attempt,
                    remote_tag,
                    publication_receipt,
                    assets,
                    notes,
                )

            self.assertEqual(observed, public_receipt)
            request = verify.call_args.args[0]
            self.assertEqual(request.repository, options.repository)
            self.assertEqual(request.release_id, publication_receipt.release_database_id)
            self.assertEqual(request.tag, publication_receipt.tag)
            self.assertEqual(
                request.expected_tag_object_sha,
                remote_tag.tag_object_sha,
            )
            self.assertEqual(request.expected_head_sha, publication_receipt.head_sha)
            self.assertEqual(request.body, notes)
            self.assertEqual(request.local_assets, assets)
            self.assertEqual(request.identity_sha1, options.identity_sha1)
            self.assertEqual(
                release_attempt.read_receipt(
                    attempt
                    / acceptance.PUBLIC_VERIFICATION_RECEIPT_FILENAME
                ),
                {
                    "schema_version": 1,
                    "attempt_binding": {
                        "repository": options.binding.repository,
                        "tag": options.binding.tag,
                        "head_sha": options.binding.head_sha,
                        "run_id": options.binding.run_id,
                        "run_attempt": options.binding.run_attempt,
                        "workflow_database_id": options.binding.workflow_database_id,
                    },
                    "publication": {
                        "repository": publication_receipt.repository,
                        "release_id": publication_receipt.release_id,
                        "release_database_id": publication_receipt.release_database_id,
                        "tag": publication_receipt.tag,
                        "head_sha": publication_receipt.head_sha,
                        "workflow_id": publication_receipt.workflow_id,
                        "run_id": publication_receipt.run_id,
                        "run_attempt": publication_receipt.run_attempt,
                        "reconciled_after_unknown_mutation": False,
                    },
                    "hosted_validation": hosted_receipt,
                    "public_acceptance": public_receipt.to_json_value(),
                },
            )

            receipt_path = (
                attempt
                / acceptance.PUBLIC_VERIFICATION_RECEIPT_FILENAME
            )
            with mock.patch.object(
                release_public_acceptance,
                "verify_public_release",
                return_value=public_receipt,
            ):
                resumed = acceptance.accept_public_release(
                    options,
                    release_targets.load_contract(),
                    attempt,
                    remote_tag,
                    publication_receipt,
                    assets,
                    notes,
                )
            self.assertEqual(resumed, public_receipt)

            receipt_path.write_text('{"tampered":true}\n', encoding="utf-8")
            receipt_path.chmod(0o600)
            with mock.patch.object(
                release_public_acceptance,
                "verify_public_release",
                return_value=public_receipt,
            ), self.assertRaisesRegex(
                release_public_acceptance.PublicButUnverifiedError,
                "existing receipt differs",
            ):
                acceptance.accept_public_release(
                    options,
                    release_targets.load_contract(),
                    attempt,
                    remote_tag,
                    publication_receipt,
                    assets,
                    notes,
                )

    def test_public_acceptance_failure_stays_explicit_and_does_not_write_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-public-unverified-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            options = self.options(root, attempt)
            publication_receipt = release_draft.PublicationReceipt(
                repository=options.repository,
                release_id="R_public",
                release_database_id=789,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
                workflow_id=options.binding.workflow_database_id,
                run_id=options.binding.run_id,
                run_attempt=options.binding.run_attempt,
                reconciled_after_unknown_mutation=False,
            )
            release_attempt.write_receipt_no_replace(
                attempt,
                "published.json",
                {"release_database_id": publication_receipt.release_database_id},
            )
            remote_tag = release_git_tag.RemoteTagBinding(
                tag=publication_receipt.tag,
                tag_object_sha="1" * 40,
                commit_sha=publication_receipt.head_sha,
            )

            with mock.patch.object(
                release_public_acceptance,
                "verify_public_release",
                side_effect=release_public_acceptance.PublicButUnverifiedError(
                    "injected public GET failure"
                ),
            ), self.assertRaisesRegex(
                release_public_acceptance.PublicButUnverifiedError,
                "^PUBLIC_BUT_UNVERIFIED: injected public GET failure$",
            ):
                acceptance.accept_public_release(
                    options,
                    release_targets.load_contract(),
                    attempt,
                    remote_tag,
                    publication_receipt,
                    (),
                    "release notes\n",
                )

            self.assertFalse(
                (attempt / acceptance.PUBLIC_VERIFICATION_RECEIPT_FILENAME).exists()
            )
