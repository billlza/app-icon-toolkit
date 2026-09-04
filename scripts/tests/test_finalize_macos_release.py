"""Finalization CLI and cross-stage orchestration tests."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from finalization_test_support import FinalizationTestCase
import finalize_macos_release
import release_attempt
import release_draft
import release_finalization_core as core
import release_git_tag
import release_immutability
import release_public_acceptance
import release_targets


class FinalizerOrchestrationTests(FinalizationTestCase):
    def test_invalid_stop_phase_fails_before_any_local_or_github_io(self) -> None:
        options = replace(
            self.options(Path("/plugin"), Path("/attempt")),
            stop_after="invalid",
        )
        with mock.patch.object(
            finalize_macos_release.core,
            "validate_attempt_root_location",
        ) as validate_attempt, mock.patch.object(
            finalize_macos_release.source,
            "validate_checkout",
        ) as validate_checkout, mock.patch.object(
            finalize_macos_release.source,
            "validate_remote_tag",
        ) as validate_remote_tag, mock.patch.object(
            finalize_macos_release,
            "load_contract",
        ) as load_contract, mock.patch.object(
            finalize_macos_release.source,
            "read_workflow_run",
        ) as read_workflow, mock.patch.object(
            finalize_macos_release,
            "initialize_or_resume",
        ) as initialize, self.assertRaisesRegex(
            core.FinalizationError,
            "unsupported finalization stop phase",
        ):
            finalize_macos_release.finalize(options)

        validate_attempt.assert_not_called()
        validate_checkout.assert_not_called()
        validate_remote_tag.assert_not_called()
        load_contract.assert_not_called()
        read_workflow.assert_not_called()
        initialize.assert_not_called()

    def test_malformed_release_contract_is_wrapped_before_workflow_admission(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-invalid-contract-") as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            scripts = checkout / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "release-targets.json").write_text("{}\n", encoding="utf-8")
            options = self.options(checkout, root / "attempt")

            with mock.patch.object(
                finalize_macos_release.source,
                "validate_checkout",
                return_value=1_700_000_000,
            ), mock.patch.object(
                finalize_macos_release.source,
                "validate_remote_tag",
                return_value=object(),
            ), mock.patch.object(
                finalize_macos_release.source,
                "read_workflow_run",
            ) as read_workflow:
                with self.assertRaisesRegex(
                    core.FinalizationError,
                    "release target contract is invalid",
                ):
                    finalize_macos_release.finalize(options)
            read_workflow.assert_not_called()

    def test_immutability_gate_fails_before_attempt_initialization(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-immutability-order-") as temporary:
            root = Path(temporary)
            options = self.options(root, root / "attempt")
            run = release_draft.WorkflowRun(
                workflow_id=options.binding.workflow_database_id,
                run_id=options.binding.run_id,
                attempt=options.binding.run_attempt,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
            with mock.patch.object(
                finalize_macos_release.core,
                "validate_attempt_root_location",
                return_value=options.attempt_root,
            ), mock.patch.object(
                finalize_macos_release.source,
                "validate_checkout",
                return_value=1_700_000_000,
            ), mock.patch.object(
                finalize_macos_release.source,
                "validate_remote_tag",
                return_value=object(),
            ), mock.patch.object(
                finalize_macos_release,
                "load_contract",
                return_value=object(),
            ), mock.patch.object(
                finalize_macos_release.source,
                "read_workflow_run",
                return_value=run,
            ), mock.patch.object(
                finalize_macos_release.source,
                "read_release_immutability_policy",
                side_effect=core.FinalizationError(
                    "repository immutable releases must be enabled before finalization"
                ),
            ), mock.patch.object(
                finalize_macos_release,
                "initialize_or_resume",
            ) as initialize:
                with self.assertRaisesRegex(
                    core.FinalizationError,
                    "immutable releases must be enabled",
                ):
                    finalize_macos_release.finalize(options)

            initialize.assert_not_called()

    def test_finalize_publish_returns_only_after_public_acceptance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-public-order-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            plugin = root / "plugin"
            plugin.mkdir(mode=0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            hosted = core.HostedValidationInput(700, 701, 1, 702)
            options = replace(
                self.options(plugin, attempt),
                stop_after="publish",
                hosted_validation=hosted,
            )
            run = release_draft.WorkflowRun(
                workflow_id=options.binding.workflow_database_id,
                run_id=options.binding.run_id,
                attempt=options.binding.run_attempt,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
            remote_tag = release_git_tag.RemoteTagBinding(
                tag=run.tag,
                tag_object_sha="1" * 40,
                commit_sha=run.head_sha,
            )
            immutability = release_immutability.ReleaseImmutabilityPolicy(
                enabled=True,
                enforced_by_owner=False,
            )
            asset_path = attempt / "candidate.zip"
            asset_path.write_bytes(b"published candidate")
            all_assets = release_draft.snapshot_local_assets(
                {asset_path.name: asset_path},
                expected_names=(asset_path.name,),
            )
            publication = release_draft.PublicationReceipt(
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
            notes = "release notes\n"
            public_receipt = self.public_acceptance_receipt(
                options,
                publication,
                notes,
            )
            events: list[str] = []

            def publish(*_args: object) -> release_draft.PublicationReceipt:
                events.append("publish")
                return publication

            def accept(*_args: object) -> release_public_acceptance.PublicAcceptanceReceipt:
                events.append("public-acceptance")
                return public_receipt

            with mock.patch.object(
                finalize_macos_release.core,
                "validate_attempt_root_location",
                return_value=attempt,
            ), mock.patch.object(
                finalize_macos_release.source,
                "validate_checkout",
                return_value=1_700_000_000,
            ), mock.patch.object(
                finalize_macos_release.source,
                "validate_remote_tag",
                return_value=remote_tag,
            ), mock.patch.object(
                finalize_macos_release,
                "load_contract",
                return_value=release_targets.load_contract(),
            ), mock.patch.object(
                finalize_macos_release.source,
                "read_workflow_run",
                side_effect=(run, run),
            ), mock.patch.object(
                finalize_macos_release.source,
                "read_release_immutability_policy",
                return_value=immutability,
            ), mock.patch.object(
                finalize_macos_release,
                "initialize_or_resume",
                return_value=attempt,
            ), mock.patch.object(
                finalize_macos_release,
                "exclusive_attempt",
                return_value=nullcontext(),
            ), mock.patch.object(
                finalize_macos_release.source,
                "download_candidates",
                return_value={"candidate": asset_path},
            ), mock.patch.object(
                finalize_macos_release.candidate,
                "prepare_assets",
                return_value=({}, all_assets),
            ), mock.patch.object(
                finalize_macos_release.notarization,
                "notarize_assets",
            ), mock.patch.object(
                finalize_macos_release,
                "read_release_notes",
                return_value=notes,
            ), mock.patch.object(
                finalize_macos_release.publication,
                "publish_assets",
                side_effect=publish,
            ), mock.patch.object(
                finalize_macos_release.acceptance,
                "accept_public_release",
                side_effect=accept,
            ):
                result = finalize_macos_release.finalize(options)

            self.assertEqual(events, ["publish", "public-acceptance"])
            self.assertEqual(result["phase"], "public-verified")
            self.assertEqual(result["publication"], {
                "repository": publication.repository,
                "release_id": publication.release_id,
                "release_database_id": publication.release_database_id,
                "tag": publication.tag,
                "head_sha": publication.head_sha,
                "workflow_id": publication.workflow_id,
                "run_id": publication.run_id,
                "run_attempt": publication.run_attempt,
                "reconciled_after_unknown_mutation": False,
            })
            self.assertEqual(result["public_acceptance"], public_receipt.to_json_value())

    def test_publish_mode_requires_complete_hosted_binding_before_any_work(self) -> None:
        options = replace(
            self.options(Path("/plugin"), Path("/attempt")),
            stop_after="publish",
            hosted_validation=None,
        )
        with mock.patch.object(
            finalize_macos_release.core,
            "validate_attempt_root_location",
        ) as validate_attempt, self.assertRaisesRegex(
            core.FinalizationError,
            "hosted validation binding",
        ):
            finalize_macos_release.finalize(options)
        validate_attempt.assert_not_called()


class FinalizerPlatformBoundaryTests(unittest.TestCase):
    def test_non_darwin_host_fails_before_checkout_or_github_reads(self) -> None:
        with mock.patch.object(
            release_attempt.sys,
            "platform",
            "linux",
        ), mock.patch.object(
            finalize_macos_release.source,
            "validate_checkout",
        ) as validate_checkout, mock.patch.object(
            finalize_macos_release.source,
            "validate_remote_tag",
        ) as validate_remote_tag, self.assertRaisesRegex(
            release_attempt.ReleaseAttemptError,
            "requires a macOS host",
        ):
            finalize_macos_release.finalize(mock.sentinel.options)

        validate_checkout.assert_not_called()
        validate_remote_tag.assert_not_called()
