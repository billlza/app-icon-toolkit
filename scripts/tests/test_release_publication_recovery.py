"""Release lookup and recovery-after-unknown-mutation tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from finalization_test_support import FinalizationTestCase
import release_attempt
import release_draft
import release_draft_staging as staging
import release_finalization_core as core
import release_github_publication as publication
import release_publication_preflight as preflight
import release_targets


class ReleasePublicationRecoveryTests(FinalizationTestCase):
    def test_release_view_uses_host_qualified_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-release-view-") as temporary:
            root = Path(temporary)
            options = self.options(root, root / "attempt")
            with mock.patch.object(
                staging,
                "required_command",
                return_value="{}",
            ) as required:
                self.assertEqual(staging.read_release_json(options), "{}")

            required.assert_called_once_with(
                (
                    "gh",
                    "release",
                    "view",
                    options.binding.tag,
                    "--repo",
                    f"github.com/{options.repository}",
                    "--json",
                    release_draft.RELEASE_VIEW_FIELDS,
                )
            )

    def test_numeric_draft_read_uses_exact_hostname_bound_release_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-numeric-draft-") as temporary:
            root = Path(temporary)
            attempt, options, run, notes, assets = self.publication_fixture(root)
            payload = self.publication_rest_release_json(run, notes, assets)
            with mock.patch.object(
                staging,
                "required_command",
                return_value=payload,
            ) as required:
                release = staging.read_numeric_draft_release(
                    options,
                    67890,
                )

            self.assertEqual(release.release_database_id, 67890)
            required.assert_called_once_with(
                (
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "repos/example/app-icon-toolkit/releases/67890",
                )
            )

    def test_public_release_after_unknown_publish_is_recovered_without_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-publish-recovery-") as temporary:
            root = Path(temporary)
            attempt, options, run, notes, assets = self.publication_fixture(root)
            draft = self.publication_release_json(
                run,
                notes,
                assets,
                draft=True,
            )
            public = self.publication_release_json(
                run,
                notes,
                assets,
                draft=False,
            )

            class Runner:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, ...]] = []

                def __call__(
                    self, command: tuple[str, ...]
                ) -> subprocess.CompletedProcess[str]:
                    if immutability.call_count != len(self.calls) + 1:
                        raise AssertionError(
                            "immutable-release policy was not rechecked before PATCH"
                        )
                    self.calls.append(command)
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="",
                        stderr="",
                    )

            runner = Runner()
            with mock.patch.object(
                publication,
                "GitHubRunner",
                return_value=runner,
            ), mock.patch.object(
                staging,
                "read_release_json",
                side_effect=(
                    draft,
                    draft,
                    draft,
                    core.FinalizationError(
                        "injected post-PATCH read failure"
                    ),
                    public,
                ),
            ), mock.patch.object(
                preflight.source,
                "read_release_immutability_policy",
            ) as immutability, mock.patch.object(
                preflight,
                "require_prepublication_state",
                side_effect=lambda *_args: (
                    preflight.source.require_immutable_release_before_publication(
                        options
                    )
                ),
            ) as prepublication, mock.patch.object(
                preflight,
                "verify_hosted_validation",
            ), mock.patch.object(
                preflight,
                "require_persisted_hosted_validation",
            ):
                with self.assertRaises(
                    core.ExternalMutationOutcomeUnknown
                ):
                    publication.publish_assets(
                        options,
                        release_targets.load_contract(),
                        attempt,
                        run,
                        assets,
                        notes,
                    )
                recovered = publication.publish_assets(
                    options,
                    release_targets.load_contract(),
                    attempt,
                    run,
                    assets,
                    notes,
                )

            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(immutability.call_count, 1)
            self.assertEqual(prepublication.call_count, 1)
            self.assertTrue(recovered.reconciled_after_unknown_mutation)
            self.assertEqual(
                release_attempt.read_receipt(attempt / "published.json"),
                {
                    "repository": recovered.repository,
                    "release_id": recovered.release_id,
                    "release_database_id": recovered.release_database_id,
                    "tag": recovered.tag,
                    "head_sha": recovered.head_sha,
                    "workflow_id": recovered.workflow_id,
                    "run_id": recovered.run_id,
                    "run_attempt": recovered.run_attempt,
                    "reconciled_after_unknown_mutation": True,
                },
            )

    def test_public_recovery_rejects_same_tag_release_with_replaced_numeric_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-public-id-race-") as temporary:
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
                workflow_id=options.binding.workflow_database_id,
                run_id=options.binding.run_id,
                attempt=options.binding.run_attempt,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
            notes = "release notes\n"
            asset_path = attempt / "candidate.zip"
            asset_path.write_bytes(b"published candidate")
            assets = release_draft.snapshot_local_assets(
                {asset_path.name: asset_path},
                expected_names=(asset_path.name,),
            )
            release_attempt.write_receipt_no_replace(
                attempt,
                "github-assets-verified.json",
                {
                    "release_id": "R_original",
                    "release_database_id": 67890,
                    "assets": [
                        {
                            "name": assets[0].name,
                            "path": assets[0].path.name,
                            "size": assets[0].size,
                            "sha256": assets[0].sha256,
                        }
                    ],
                },
            )
            original = release_draft.VerifiedDraft(
                repository=options.repository,
                run=run,
                release_id="R_original",
                release_database_id=67890,
                expected_body=notes,
                assets=assets,
            )
            release_attempt.write_receipt_no_replace(
                attempt,
                "github-publication-intent.json",
                publication._publication_intent_payload(original),
            )
            replacement = json.dumps(
                {
                    "id": "R_replacement",
                    "databaseId": 67891,
                    "tagName": run.tag,
                    "name": f"App Icon Toolkit {run.tag}",
                    "body": notes,
                    "isDraft": False,
                    "isPrerelease": False,
                    "assets": [
                        {
                            "name": assets[0].name,
                            "size": assets[0].size,
                            "digest": f"sha256:{assets[0].sha256}",
                            "state": "uploaded",
                        }
                    ],
                }
            )

            with mock.patch.object(
                staging,
                "read_release_json",
                return_value=replacement,
            ), mock.patch.object(
                preflight,
                "require_persisted_hosted_validation",
            ), self.assertRaisesRegex(
                core.FinalizationError,
                "persisted publication intent",
            ):
                publication.publish_assets(
                    options,
                    release_targets.load_contract(),
                    attempt,
                    run,
                    assets,
                    notes,
                )

            self.assertFalse((attempt / "published.json").exists())
