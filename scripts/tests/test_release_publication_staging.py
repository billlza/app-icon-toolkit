"""Draft staging and hosted-validation binding tests."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from finalization_test_support import FinalizationTestCase
import release_artifacts
import release_attempt
import release_draft
import release_draft_staging as staging
import release_finalization_core as core
import release_github_publication as publication
import release_hosted_validation
import release_publication_preflight as preflight
import release_targets


class ReleasePublicationStagingTests(FinalizationTestCase):
    def test_publish_phase_requires_notarization_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-publish-gate-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            options = self.options(root, root / "attempt")
            with self.assertRaisesRegex(
                core.FinalizationError,
                "notarization phase receipt is missing",
            ):
                publication.publish_assets(
                    options,
                    release_targets.load_contract(),
                    root,
                    release_draft.WorkflowRun(
                        workflow_id=456,
                        run_id=123,
                        attempt=1,
                        tag="v1.2.3",
                        head_sha=options.binding.head_sha,
                    ),
                    (),
                    "release notes",
                )

    def test_stage_uploads_exact_assets_without_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-stage-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            release_attempt.write_receipt_no_replace(
                attempt,
                "notarized.json",
                {"accepted": True},
            )
            options = self.options(root, attempt)
            run = release_draft.WorkflowRun(
                workflow_id=456,
                run_id=123,
                attempt=1,
                tag="v1.2.3",
                head_sha=options.binding.head_sha,
            )
            notes = "release notes\n"
            asset_path = attempt / "candidate.zip"
            asset_path.write_bytes(b"signed candidate")
            assets = release_draft.snapshot_local_assets(
                {asset_path.name: asset_path},
                expected_names=(asset_path.name,),
            )

            def release_json(*, with_asset: bool) -> str:
                remote_assets = []
                if with_asset:
                    remote_assets.append(
                        {
                            "name": assets[0].name,
                            "size": assets[0].size,
                            "digest": f"sha256:{assets[0].sha256}",
                            "state": "uploaded",
                        }
                    )
                return json.dumps(
                    {
                        "id": "R_stage",
                        "databaseId": 600,
                        "tagName": run.tag,
                        "name": f"App Icon Toolkit {run.tag}",
                        "body": notes,
                        "isDraft": True,
                        "isPrerelease": False,
                        "assets": remote_assets,
                    }
                )

            with mock.patch.object(
                staging,
                "read_release_json",
                side_effect=(release_json(with_asset=False), release_json(with_asset=True)),
            ), mock.patch.object(
                staging.release_draft,
                "run_uploads",
            ) as uploads, mock.patch.object(
                staging.release_draft,
                "publish_verified_draft",
            ) as publish:
                verified = staging.stage_assets(
                    options,
                    attempt,
                    run,
                    assets,
                    notes,
                )

            uploads.assert_called_once()
            publish.assert_not_called()
            self.assertEqual(verified.release_database_id, 600)
            self.assertTrue((attempt / "github-assets-verified.json").is_file())
            self.assertFalse((attempt / "github-publication-intent.json").exists())
            self.assertFalse((attempt / "published.json").exists())

    def test_upload_intent_precedes_post_and_crash_requires_reconciliation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-upload-intent-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            release_attempt.write_receipt_no_replace(
                attempt,
                "notarized.json",
                {"accepted": True},
            )
            options = self.options(root, attempt)
            run = release_draft.WorkflowRun(
                workflow_id=456,
                run_id=123,
                attempt=1,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
            notes = "release notes\n"
            asset_path = attempt / "candidate.zip"
            asset_path.write_bytes(b"signed candidate")
            assets = release_draft.snapshot_local_assets(
                {asset_path.name: asset_path},
                expected_names=(asset_path.name,),
            )

            def release_json(*, uploaded: bool) -> str:
                remote_assets = []
                if uploaded:
                    remote_assets.append(
                        {
                            "name": assets[0].name,
                            "size": assets[0].size,
                            "digest": f"sha256:{assets[0].sha256}",
                            "state": "uploaded",
                        }
                    )
                return json.dumps(
                    {
                        "id": "R_upload_intent",
                        "databaseId": 601,
                        "tagName": run.tag,
                        "name": f"App Icon Toolkit {run.tag}",
                        "body": notes,
                        "isDraft": True,
                        "isPrerelease": False,
                        "assets": remote_assets,
                    }
                )

            intent_path = attempt / "github-upload-intent-000.json"

            class InterruptingRunner:
                def __call__(self, command: tuple[str, ...]):
                    if not intent_path.is_file():
                        raise AssertionError("asset POST ran before its durable intent")
                    raise KeyboardInterrupt("simulated process interruption")

            with mock.patch.object(
                staging,
                "read_release_json",
                return_value=release_json(uploaded=False),
            ), mock.patch.object(
                staging,
                "GitHubRunner",
                return_value=InterruptingRunner(),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    staging.stage_assets(
                        options,
                        attempt,
                        run,
                        assets,
                        notes,
                    )

            intent = release_attempt.read_receipt(intent_path)
            self.assertEqual(intent["release_database_id"], 601)
            self.assertEqual(intent["asset"]["name"], assets[0].name)
            self.assertEqual(intent["asset"]["sha256"], assets[0].sha256)
            self.assertFalse((attempt / "github-upload-unknown.json").exists())

            with mock.patch.object(
                staging,
                "read_release_json",
                return_value=release_json(uploaded=False),
            ), mock.patch.object(staging, "GitHubRunner") as runner_factory:
                with self.assertRaisesRegex(
                    core.ExternalMutationOutcomeUnknown,
                    "--reconcile-github-upload",
                ):
                    staging.stage_assets(
                        options,
                        attempt,
                        run,
                        assets,
                        notes,
                    )
            runner_factory.assert_not_called()

            class SuccessfulRunner:
                def __call__(
                    self,
                    command: tuple[str, ...],
                ) -> subprocess.CompletedProcess[str]:
                    if not intent_path.is_file():
                        raise AssertionError("reconciled POST lost its durable intent")
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="",
                        stderr="",
                    )

            with mock.patch.object(
                staging,
                "read_release_json",
                side_effect=(
                    release_json(uploaded=False),
                    release_json(uploaded=True),
                ),
            ), mock.patch.object(
                staging,
                "GitHubRunner",
                return_value=SuccessfulRunner(),
            ):
                verified = staging.stage_assets(
                    replace(options, reconcile_github_upload=True),
                    attempt,
                    run,
                    assets,
                    notes,
                )

            self.assertEqual(verified.release_database_id, 601)
            self.assertTrue((attempt / "github-assets-verified.json").is_file())

    def test_hosted_receipt_download_is_numeric_digest_bound_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-hosted-receipt-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            binding = core.HostedValidationInput(
                workflow_id=700,
                run_id=701,
                run_attempt=2,
                receipt_artifact_id=702,
            )
            options = replace(
                self.options(root, attempt),
                hosted_validation=binding,
            )
            source_run = release_draft.WorkflowRun(
                workflow_id=456,
                run_id=123,
                attempt=1,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
            hosted_run = release_draft.WorkflowRun(
                workflow_id=binding.workflow_id,
                run_id=binding.run_id,
                attempt=binding.run_attempt,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
            asset_path = attempt / "candidate.zip"
            asset_path.write_bytes(b"signed candidate")
            assets = release_draft.snapshot_local_assets(
                {asset_path.name: asset_path},
                expected_names=(asset_path.name,),
            )
            verified = release_draft.VerifiedDraft(
                repository=options.repository,
                run=source_run,
                release_id="R_hosted",
                release_database_id=800,
                expected_body="release notes\n",
                assets=assets,
            )
            record = release_artifacts.ArtifactRecord(
                artifact_id=binding.receipt_artifact_id,
                name=(
                    f"hosted-validation-receipt-run-{binding.run_id}"
                    f"-attempt-{binding.run_attempt}"
                ),
                size_in_bytes=900,
                archive_sha256="a" * 64,
                created_at="2026-09-04T00:00:00Z",
                updated_at="2026-09-04T00:01:00Z",
                run_id=hosted_run.run_id,
                head_sha=hosted_run.head_sha,
                head_branch=hosted_run.tag,
                repository_id=99,
                head_repository_id=99,
            )
            workflow = release_hosted_validation.HostedWorkflowIdentity(
                workflow_id=binding.workflow_id,
                name=release_hosted_validation.EXPECTED_WORKFLOW_NAME,
                path=release_hosted_validation.EXPECTED_WORKFLOW_PATH,
                state="active",
            )
            receipt = mock.sentinel.hosted_receipt
            receipt_payload = {
                "schema_version": 1,
                "assets": [
                    {
                        "asset_id": 901,
                        "name": assets[0].name,
                        "size": assets[0].size,
                        "sha256": assets[0].sha256,
                    }
                ],
            }
            downloader = mock.sentinel.downloader

            with mock.patch.object(
                preflight.source,
                "read_hosted_validation_workflow",
                return_value=workflow,
            ), mock.patch.object(
                preflight.source,
                "read_hosted_validation_run",
                return_value=hosted_run,
            ), mock.patch.object(
                preflight.source,
                "read_hosted_receipt_artifact",
                return_value=record,
            ), mock.patch.object(
                preflight.release_artifact_download,
                "GitHubArtifactZipDownloader",
                return_value=downloader,
            ), mock.patch.object(
                preflight.release_artifact_download,
                "download_public_archive",
                return_value=attempt / "hosted-receipt" / "hosted-validation-receipt.json",
            ) as download, mock.patch.object(
                preflight,
                "read_receipt",
                return_value={"schema_version": 1},
            ), mock.patch.object(
                preflight.release_hosted_validation,
                "parse_receipt",
                return_value=receipt,
            ), mock.patch.object(
                preflight.release_hosted_validation,
                "bind_receipt",
            ) as bind, mock.patch.object(
                preflight.release_hosted_validation,
                "receipt_payload",
                return_value=receipt_payload,
            ), mock.patch.object(
                preflight,
                "sha256_file",
                return_value="b" * 64,
            ):
                observed = preflight.verify_hosted_validation(
                    options,
                    release_targets.load_contract(),
                    attempt,
                    source_run,
                    verified,
                )

            self.assertIs(observed, receipt)
            download.assert_called_once_with(
                options.repository,
                record,
                preflight.HOSTED_RECEIPT_FILENAME,
                attempt / "hosted-receipt-cache",
                attempt / "hosted-receipt",
                downloader,
            )
            bind.assert_called_once()
            persisted = release_attempt.read_receipt(
                attempt / "hosted-validation-verified.json"
            )
            self.assertEqual(persisted["receipt_artifact_id"], record.artifact_id)
            self.assertEqual(
                persisted["receipt_artifact_sha256"],
                record.archive_sha256,
            )
            self.assertEqual(persisted["workflow_path"], workflow.path)
            self.assertEqual(persisted["receipt"], receipt_payload)
