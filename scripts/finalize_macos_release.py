"""CLI and orchestration for the local trusted macOS release finalizer."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

import macos_signing
import release_candidate_preparation as candidate
import release_draft
import release_draft_staging as staging
import release_finalization_core as core
import release_finalization_source as source
import release_github_publication as publication
import release_hosted_validation
import release_notarization_transaction as notarization
import release_public_acceptance
import release_public_acceptance_transaction as acceptance
from release_attempt import (
    ReleaseAttemptError,
    ReleaseBinding,
    exclusive_attempt,
    initialize_or_resume,
    require_macos_host,
)
from release_files import ReleaseFileError
from release_notes import ReleaseNotesError, read_release_notes
from release_targets import load_contract


def finalize(options: core.FinalizationOptions) -> dict[str, Any]:
    require_macos_host()
    core.validate_finalization_phase(options.stop_after)
    if options.stop_after == "publish" and options.hosted_validation is None:
        raise core.FinalizationError(
            "publish requires the complete hosted validation binding"
        )
    validated_attempt_root = core.validate_attempt_root_location(
        options.plugin_root,
        options.attempt_root,
    )
    source_epoch = source.validate_checkout(options)
    remote_tag = source.validate_remote_tag(options)
    try:
        contract = load_contract(
            options.plugin_root / "scripts" / "release-targets.json"
        )
    except RuntimeError as error:
        raise core.FinalizationError(
            f"release target contract is invalid: {error}"
        ) from error
    run = source.read_workflow_run(options)
    immutability_policy = source.read_release_immutability_policy(options)
    attempt_root = initialize_or_resume(validated_attempt_root, options.binding)
    with exclusive_attempt(attempt_root):
        downloads = source.download_candidates(options, contract, attempt_root, run)
        assets, all_assets = candidate.prepare_assets(
            options,
            contract,
            attempt_root,
            downloads,
            source_epoch,
        )
        result: dict[str, Any] = {
            "phase": "prepare",
            "attempt_root": str(attempt_root),
            "assets": [asdict(asset) | {"path": str(asset.path)} for asset in all_assets],
            "remote_tag": asdict(remote_tag),
            "release_immutability": asdict(immutability_policy),
        }
        if options.stop_after == "prepare":
            return result

        notarization.notarize_assets(options, contract, attempt_root, assets)
        result["phase"] = "notarize"
        if options.stop_after == "notarize":
            return result

        fresh_run = source.read_workflow_run(options)
        if fresh_run != run:
            raise core.FinalizationError(
                "workflow run binding changed before publication"
            )
        if source.validate_remote_tag(options) != remote_tag:
            raise core.FinalizationError(
                "remote annotated tag binding changed before publication"
            )
        if source.download_candidates(options, contract, attempt_root, fresh_run) != downloads:
            raise core.FinalizationError(
                "candidate download root changed before publication"
            )
        notes = read_release_notes(options.plugin_root / "CHANGELOG.md")
        if options.stop_after == "stage":
            verified_draft = staging.stage_assets(
                options,
                attempt_root,
                fresh_run,
                all_assets,
                notes,
            )
            result["phase"] = "stage"
            result["draft"] = {
                "release_id": verified_draft.release_id,
                "release_database_id": verified_draft.release_database_id,
            }
            return result
        publication_receipt = publication.publish_assets(
            options,
            contract,
            attempt_root,
            fresh_run,
            all_assets,
            notes,
        )
        public_acceptance = acceptance.accept_public_release(
            options,
            contract,
            attempt_root,
            remote_tag,
            publication_receipt,
            all_assets,
            notes,
        )
        result["phase"] = "public-verified"
        result["publication"] = asdict(publication_receipt)
        result["public_acceptance"] = public_acceptance.to_json_value()
        return result


def _hosted_validation_input(
    arguments: argparse.Namespace,
) -> core.HostedValidationInput | None:
    values = (
        arguments.hosted_workflow_id,
        arguments.hosted_run_id,
        arguments.hosted_run_attempt,
        arguments.hosted_receipt_artifact_id,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise core.FinalizationError(
            "hosted validation workflow, run, attempt, and receipt artifact IDs "
            "must be supplied together"
        )
    for name, value in zip(
        (
            "workflow ID",
            "run ID",
            "run attempt",
            "receipt artifact ID",
        ),
        values,
        strict=True,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise core.FinalizationError(
                f"hosted validation {name} must be positive"
            )
    return core.HostedValidationInput(
        workflow_id=values[0],
        run_id=values[1],
        run_attempt=values[2],
        receipt_artifact_id=values[3],
    )


def build_options(arguments: argparse.Namespace) -> core.FinalizationOptions:
    binding = ReleaseBinding(
        repository=arguments.repository,
        tag=arguments.tag,
        head_sha=arguments.head_sha,
        run_id=arguments.run_id,
        run_attempt=arguments.run_attempt,
        workflow_database_id=arguments.workflow_id,
    )
    return core.FinalizationOptions(
        plugin_root=arguments.plugin_root.resolve(strict=True),
        repository=arguments.repository,
        binding=binding,
        identity_sha1=arguments.identity_sha1,
        notary_profile=arguments.notary_profile,
        attempt_root=arguments.attempt_root,
        stop_after=arguments.stop_after,
        notary_timeout=arguments.notary_timeout,
        adopted_submissions=core.parse_adopted_submissions(arguments.adopt_submission),
        reconcile_github_upload=arguments.reconcile_github_upload,
        reconcile_github_publish=arguments.reconcile_github_publish,
        hosted_validation=_hosted_validation_input(arguments),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, default=Path("."))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--workflow-id", required=True, type=int)
    parser.add_argument("--identity-sha1", required=True)
    parser.add_argument("--notary-profile", required=True)
    parser.add_argument("--attempt-root", required=True, type=Path)
    parser.add_argument(
        "--stop-after",
        choices=core.FINALIZATION_PHASES,
        default="prepare",
    )
    parser.add_argument("--notary-timeout", default="60m")
    parser.add_argument("--adopt-submission", action="append", default=[])
    parser.add_argument("--reconcile-github-upload", action="store_true")
    parser.add_argument("--reconcile-github-publish", action="store_true")
    parser.add_argument("--hosted-workflow-id", type=int)
    parser.add_argument("--hosted-run-id", type=int)
    parser.add_argument("--hosted-run-attempt", type=int)
    parser.add_argument("--hosted-receipt-artifact-id", type=int)
    arguments = parser.parse_args()
    try:
        result = finalize(build_options(arguments))
    except (
        core.FinalizationError,
        ReleaseAttemptError,
        ReleaseFileError,
        ReleaseNotesError,
        release_draft.ReleaseDraftError,
        release_hosted_validation.HostedValidationError,
        release_public_acceptance.PublicButUnverifiedError,
        macos_signing.MacSigningError,
    ) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
