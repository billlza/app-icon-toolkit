"""Shared fixtures for finalization stage tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import macos_signing
import release_candidate_preparation as candidate
import release_draft
import release_files
import release_finalization_core as core
import release_public_acceptance
import release_targets
import release_attempt
from release_test_support import prepare_release_package


IDENTITY = "2DA7764ED42B213AE04925B6261238B24C758FE1"


@unittest.skipUnless(
    sys.platform == "darwin",
    "trusted macOS finalization tests require a Darwin host",
)
class FinalizationTestCase(unittest.TestCase):
    @staticmethod
    def options(plugin_root: Path, attempt_root: Path) -> core.FinalizationOptions:
        binding = release_attempt.ReleaseBinding(
            repository="example/app-icon-toolkit",
            tag="v1.2.3",
            head_sha="0123456789abcdef0123456789abcdef01234567",
            run_id=123,
            run_attempt=1,
            workflow_database_id=456,
        )
        return core.FinalizationOptions(
            plugin_root=plugin_root,
            repository=binding.repository,
            binding=binding,
            identity_sha1=IDENTITY,
            notary_profile="test-notary-profile",
            attempt_root=attempt_root,
            stop_after="prepare",
            notary_timeout="60m",
            adopted_submissions={},
            reconcile_github_upload=False,
            reconcile_github_publish=False,
        )

    @staticmethod
    def fixture(
        root: Path,
    ) -> tuple[
        Path,
        release_targets.ReleaseContract,
        release_targets.ReleaseTarget,
        Path,
        Path,
        Path,
    ]:
        plugin = root / "plugin"
        package = root / "package"
        for relative in prepare_release_package.STATIC_PATHS:
            contents = f"fixture:{relative.as_posix()}\n".encode()
            plugin_source = plugin / relative
            plugin_source.parent.mkdir(parents=True, exist_ok=True)
            plugin_source.write_bytes(contents)
            package_source = package / relative
            package_source.parent.mkdir(parents=True, exist_ok=True)
            package_source.write_bytes(contents)
        contract = release_targets.load_contract()
        target = contract.target("aarch64-apple-darwin")
        original_binary = package / "bin" / target.binary_name
        original_binary.parent.mkdir(parents=True)
        original_binary.write_bytes(b"unsigned Mach-O fixture")
        original_binary.chmod(0o755)
        archive = root / target.release_filename("v1.2.3")
        prepare_release_package.write_zip(package, archive, 1_700_000_000)

        target_root = root / "target-work"
        target_root.mkdir(mode=0o700)
        options = FinalizationTestCase.options(plugin, root / "attempt")
        _package, working_binary = candidate.extract_candidate(
            options,
            target,
            archive,
            target_root,
        )
        return plugin, contract, target, archive, target_root, working_binary

    @staticmethod
    def verification(binary: Path) -> macos_signing.SignatureVerificationReceipt:
        return macos_signing.SignatureVerificationReceipt(
            signed_sha256=release_files.sha256_file(
                binary,
                label="signed test binary",
            ),
            identity_sha1=IDENTITY,
            identifier="io.github.billlza.app-icon-toolkit.mcp",
            team_id="YKUPL7Z869",
            architectures=("arm64",),
            slices=(
                macos_signing.SignedSlice(
                    architecture="arm64",
                    leaf_authority="Developer ID Application: Test (YKUPL7Z869)",
                    cdhash="a" * 40,
                    timestamp="Sep 4, 2026",
                    designated_requirement="designated => test",
                ),
            ),
        )

    @staticmethod
    def publication_fixture(
        root: Path,
    ) -> tuple[
        Path,
        core.FinalizationOptions,
        release_draft.WorkflowRun,
        str,
        tuple[release_draft.LocalAsset, ...],
    ]:
        root.chmod(0o700)
        attempt = root / "attempt"
        attempt.mkdir(mode=0o700)
        release_attempt.write_receipt_no_replace(
            attempt,
            "notarized.json",
            {"accepted": True},
        )
        options = FinalizationTestCase.options(root, attempt)
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
        return attempt, options, run, notes, assets

    @staticmethod
    def publication_release_json(
        run: release_draft.WorkflowRun,
        notes: str,
        assets: tuple[release_draft.LocalAsset, ...],
        *,
        draft: bool,
        release_id: str = "R_kgDORelease",
        release_database_id: int = 67890,
    ) -> str:
        return json.dumps(
            {
                "id": release_id,
                "databaseId": release_database_id,
                "tagName": run.tag,
                "name": f"App Icon Toolkit {run.tag}",
                "body": notes,
                "isDraft": draft,
                "isPrerelease": False,
                "assets": [
                    {
                        "name": asset.name,
                        "size": asset.size,
                        "digest": f"sha256:{asset.sha256}",
                        "state": "uploaded",
                    }
                    for asset in assets
                ],
            }
        )

    @staticmethod
    def publication_rest_release_json(
        run: release_draft.WorkflowRun,
        notes: str,
        assets: tuple[release_draft.LocalAsset, ...],
        *,
        release_id: str = "R_kgDORelease",
        release_database_id: int = 67890,
        asset_id_offset: int = 900,
    ) -> str:
        return json.dumps(
            {
                "id": release_database_id,
                "node_id": release_id,
                "tag_name": run.tag,
                "name": f"App Icon Toolkit {run.tag}",
                "body": notes,
                "draft": True,
                "prerelease": False,
                "assets": [
                    {
                        "id": asset_id_offset + index,
                        "name": asset.name,
                        "size": asset.size,
                        "digest": f"sha256:{asset.sha256}",
                        "state": "uploaded",
                    }
                    for index, asset in enumerate(assets)
                ],
            }
        )

    @staticmethod
    def public_acceptance_receipt(
        options: core.FinalizationOptions,
        publication: release_draft.PublicationReceipt,
        notes: str,
    ) -> release_public_acceptance.PublicAcceptanceReceipt:
        return release_public_acceptance.PublicAcceptanceReceipt(
            schema_version=1,
            status=release_public_acceptance.PUBLIC_VERIFIED,
            repository=options.repository,
            release_id=publication.release_database_id,
            tag=publication.tag,
            tag_object_sha="1" * 40,
            head_sha=publication.head_sha,
            identity_sha1=options.identity_sha1,
            name=f"App Icon Toolkit {publication.tag}",
            body_sha256=hashlib.sha256(notes.encode("utf-8")).hexdigest(),
            immutable=True,
            tag_binding_verified=True,
            snapshot_sha256="a" * 64,
            snapshots_match=True,
            github_mutation_performed=False,
            candidate_execution_performed=False,
            assets=(),
            static_files=(),
            archives=(),
        )

    @staticmethod
    def notary_adoption_fixture(
        root: Path,
        *,
        create_intent: bool,
        submission_job_id: str | None = None,
    ) -> tuple[
        Path,
        Path,
        Path,
        core.FinalizationOptions,
        release_targets.ReleaseContract,
        release_targets.ReleaseTarget,
        str,
    ]:
        root.chmod(0o700)
        plugin = root / "plugin"
        plugin.mkdir(mode=0o700)
        attempt = root / "attempt"
        attempt.mkdir(mode=0o700)
        assets = attempt / "assets"
        assets.mkdir(mode=0o700)
        release_attempt.write_receipt_no_replace(
            attempt,
            "prepared.json",
            {"ok": True},
        )
        full_contract = release_targets.load_contract()
        target = next(
            item
            for item in full_contract.targets
            if item.family in core.MACOS_FAMILIES
        )
        contract = release_targets.ReleaseContract(
            release_toolchain=full_contract.release_toolchain,
            macos_signing=full_contract.macos_signing,
            targets=(target,),
        )
        archive = assets / target.release_filename("v1.2.3")
        archive.write_bytes(b"signed archive awaiting notarization")
        archive_sha256 = release_files.sha256_file(archive)
        work = release_attempt.private_subdirectory(attempt, "macos-work")
        target_root = release_attempt.private_subdirectory(work, target.id)
        if create_intent:
            release_attempt.write_receipt_no_replace(
                target_root,
                "notary-submission-intent.json",
                {"target": target.id, "archive_sha256": archive_sha256},
            )
        if submission_job_id is not None:
            release_attempt.write_receipt_no_replace(
                target_root,
                "notary-submission.json",
                {
                    "job_id": submission_job_id,
                    "archive_sha256": archive_sha256,
                },
            )
        candidate = (
            target_root
            / "candidate"
            / "app-icon-toolkit"
            / "bin"
            / target.binary_name
        )
        candidate.parent.mkdir(parents=True, mode=0o700)
        candidate.write_bytes(b"signed candidate")
        return (
            attempt,
            assets,
            target_root,
            FinalizationTestCase.options(plugin, attempt),
            contract,
            target,
            archive_sha256,
        )
