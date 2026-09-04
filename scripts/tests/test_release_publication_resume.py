"""Publication intent durability and explicit-retry tests."""

from __future__ import annotations

from dataclasses import replace
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


class ReleasePublicationResumeTests(FinalizationTestCase):
    def test_successful_publication_receipt_is_stable_on_read_only_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-publish-resume-") as temporary:
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
            remote_asset = {
                "name": assets[0].name,
                "size": assets[0].size,
                "digest": f"sha256:{assets[0].sha256}",
                "state": "uploaded",
            }

            def release_json(*, draft: bool) -> str:
                return json.dumps(
                    {
                        "id": "R_kgDORelease",
                        "databaseId": 67890,
                        "tagName": run.tag,
                        "name": f"App Icon Toolkit {run.tag}",
                        "body": notes,
                        "isDraft": draft,
                        "isPrerelease": False,
                        "assets": [remote_asset],
                    }
                )

            class Runner:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, ...]] = []

                def __call__(
                    self, command: tuple[str, ...]
                ) -> subprocess.CompletedProcess[str]:
                    self.assert_publication_intent_is_durable(attempt)
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

                @staticmethod
                def assert_publication_intent_is_durable(attempt_root: Path) -> None:
                    if not (attempt_root / "github-publication-intent.json").is_file():
                        raise AssertionError("publication mutation ran before durable intent")

            runner = Runner()

            def hosted_gate(*_args, **_kwargs):
                self.assertFalse(
                    (attempt / "github-publication-intent.json").exists(),
                    "publication intent was written before hosted validation",
                )

            with mock.patch.object(
                publication,
                "GitHubRunner",
                return_value=runner,
            ), mock.patch.object(
                staging,
                "read_release_json",
                side_effect=(
                    release_json(draft=True),
                    release_json(draft=True),
                    release_json(draft=True),
                    release_json(draft=False),
                    release_json(draft=False),
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
                side_effect=hosted_gate,
            ) as hosted_validation, mock.patch.object(
                preflight,
                "require_persisted_hosted_validation",
            ):
                first = publication.publish_assets(
                    options,
                    release_targets.load_contract(),
                    attempt,
                    run,
                    assets,
                    notes,
                )
                resumed = publication.publish_assets(
                    options,
                    release_targets.load_contract(),
                    attempt,
                    run,
                    assets,
                    notes,
                )

            self.assertFalse(first.reconciled_after_unknown_mutation)
            hosted_validation.assert_called_once()
            self.assertEqual(immutability.call_count, 1)
            self.assertEqual(prepublication.call_count, 1)
            self.assertEqual(resumed, first)
            self.assertEqual(
                runner.calls,
                [release_draft.publish_command(
                    release_draft.VerifiedDraft(
                        repository=options.repository,
                        run=run,
                        release_id=first.release_id,
                        release_database_id=first.release_database_id,
                        expected_body=notes,
                        assets=assets,
                    )
                )],
            )
            self.assertEqual(
                release_attempt.read_receipt(attempt / "published.json"),
                {
                    "repository": first.repository,
                    "release_id": first.release_id,
                    "release_database_id": first.release_database_id,
                    "tag": first.tag,
                    "head_sha": first.head_sha,
                    "workflow_id": first.workflow_id,
                    "run_id": first.run_id,
                    "run_attempt": first.run_attempt,
                    "reconciled_after_unknown_mutation": False,
                },
            )
            self.assertEqual(
                release_attempt.read_receipt(
                    attempt / "github-publication-intent.json"
                ),
                publication._publication_intent_payload(
                    release_draft.VerifiedDraft(
                        repository=options.repository,
                        run=run,
                        release_id=first.release_id,
                        release_database_id=first.release_database_id,
                        expected_body=notes,
                        assets=assets,
                    )
                ),
            )

    def test_unknown_publication_requires_explicit_single_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-publish-unknown-") as temporary:
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
                    self.returncodes = [1, 0]
                    self.calls: list[tuple[str, ...]] = []

                def __call__(
                    self, command: tuple[str, ...]
                ) -> subprocess.CompletedProcess[str]:
                    if not (attempt / "github-publication-intent.json").is_file():
                        raise AssertionError("publication mutation ran before durable intent")
                    if immutability.call_count != len(self.calls) + 1:
                        raise AssertionError(
                            "immutable-release policy was not rechecked before PATCH"
                        )
                    self.calls.append(command)
                    returncode = self.returncodes.pop(0)
                    return subprocess.CompletedProcess(
                        command,
                        returncode,
                        stdout="",
                        stderr="injected unknown mutation" if returncode else "",
                    )

            runner = Runner()
            with mock.patch.object(
                publication,
                "GitHubRunner",
                return_value=runner,
            ), mock.patch.object(
                staging,
                "read_release_json",
                side_effect=(*([draft] * 7), public),
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
                self.assertEqual(len(runner.calls), 1)
                self.assertTrue(
                    (attempt / "github-publication-intent.json").is_file()
                )
                self.assertFalse((attempt / "published.json").exists())

                with self.assertRaisesRegex(
                    core.ExternalMutationOutcomeUnknown,
                    "--reconcile-github-publish",
                ):
                    publication.publish_assets(
                        options,
                        release_targets.load_contract(),
                        attempt,
                        run,
                        assets,
                        notes,
                    )
                self.assertEqual(len(runner.calls), 1)

                receipt = publication.publish_assets(
                    replace(options, reconcile_github_publish=True),
                    release_targets.load_contract(),
                    attempt,
                    run,
                    assets,
                    notes,
                )

            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(immutability.call_count, 2)
            self.assertEqual(prepublication.call_count, 2)
            self.assertEqual(runner.returncodes, [])
            self.assertTrue(receipt.reconciled_after_unknown_mutation)
            self.assertEqual(runner.calls[0], runner.calls[1])
            self.assertEqual(
                runner.calls[0],
                release_draft.publish_command(
                    release_draft.VerifiedDraft(
                        repository=options.repository,
                        run=run,
                        release_id="R_kgDORelease",
                        release_database_id=67890,
                        expected_body=notes,
                        assets=assets,
                    )
                ),
            )
