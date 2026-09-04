"""Cross-invocation finalization phase-resume tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from unittest import mock

from finalization_test_support import FinalizationTestCase
import finalize_macos_release
import release_draft
import release_files
import release_finalization_core as core
import release_git_tag
import release_immutability
import release_targets


class ReleaseFinalizationResumeTests(FinalizationTestCase):
    def test_prepare_notarize_and_stage_share_one_sealed_asset_set(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-phase-resume-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            plugin = root / "plugin"
            plugin.mkdir(mode=0o700)
            attempt = root / "attempt"
            downloads = root / "downloads"
            downloads.mkdir(mode=0o700)
            prepare_options = self.options(plugin, attempt)
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
            archive_name = target.release_filename(prepare_options.binding.tag)
            (downloads / archive_name).write_bytes(b"CI archive fixture")
            run = release_draft.WorkflowRun(
                workflow_id=prepare_options.binding.workflow_database_id,
                run_id=prepare_options.binding.run_id,
                attempt=prepare_options.binding.run_attempt,
                tag=prepare_options.binding.tag,
                head_sha=prepare_options.binding.head_sha,
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
            notes = "release notes\n"

            def stage_assets(_options, _attempt, fresh_run, assets, expected_notes):
                return release_draft.VerifiedDraft(
                    repository=prepare_options.repository,
                    run=fresh_run,
                    release_id="R_phase_resume",
                    release_database_id=789,
                    expected_body=expected_notes,
                    assets=assets,
                )

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
                return_value=contract,
            ), mock.patch.object(
                finalize_macos_release.source,
                "read_workflow_run",
                return_value=run,
            ), mock.patch.object(
                finalize_macos_release.source,
                "read_release_immutability_policy",
                return_value=immutability,
            ), mock.patch.object(
                finalize_macos_release.source,
                "download_candidates",
                return_value=downloads,
            ), mock.patch.object(
                finalize_macos_release.notarization,
                "notarize_assets",
            ) as notarize, mock.patch.object(
                finalize_macos_release,
                "read_release_notes",
                return_value=notes,
            ), mock.patch.object(
                finalize_macos_release.staging,
                "stage_assets",
                side_effect=stage_assets,
            ) as stage:
                prepared = finalize_macos_release.finalize(prepare_options)
                checksum = attempt / "assets" / release_draft.CHECKSUM_ASSET_NAME
                receipt = attempt / "prepared.json"
                checksum_before = (
                    checksum.read_bytes(),
                    release_files.FileSnapshot.from_stat(checksum.stat()),
                )
                receipt_before = (
                    receipt.read_bytes(),
                    release_files.FileSnapshot.from_stat(receipt.stat()),
                )
                notarized = finalize_macos_release.finalize(
                    replace(prepare_options, stop_after="notarize")
                )
                staged = finalize_macos_release.finalize(
                    replace(prepare_options, stop_after="stage")
                )

            self.assertEqual(
                (prepared["phase"], notarized["phase"], staged["phase"]),
                ("prepare", "notarize", "stage"),
            )
            self.assertEqual(notarize.call_count, 2)
            stage.assert_called_once()
            self.assertEqual(
                tuple(asset.name for asset in stage.call_args.args[3]),
                (release_draft.CHECKSUM_ASSET_NAME, archive_name),
            )
            self.assertEqual(
                staged["draft"],
                {"release_id": "R_phase_resume", "release_database_id": 789},
            )
            self.assertEqual(
                (
                    checksum.read_bytes(),
                    release_files.FileSnapshot.from_stat(checksum.stat()),
                ),
                checksum_before,
            )
            self.assertEqual(
                (
                    receipt.read_bytes(),
                    release_files.FileSnapshot.from_stat(receipt.stat()),
                ),
                receipt_before,
            )
