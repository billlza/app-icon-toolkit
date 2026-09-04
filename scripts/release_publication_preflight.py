"""Hosted evidence binding and the final read-only pre-PATCH gate."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import release_artifact_download
import release_draft
import release_draft_staging as staging
import release_finalization_source as source
import release_hosted_validation
from release_attempt import ReleaseAttemptError, private_subdirectory, read_receipt
from release_files import ReleaseFileError, sha256_file
from release_finalization_core import FinalizationError, FinalizationOptions, ensure_receipt
from release_targets import ReleaseContract


HOSTED_RECEIPT_FILENAME = "hosted-validation-receipt.json"


def verify_hosted_validation(
    options: FinalizationOptions,
    contract: ReleaseContract,
    attempt_root: Path,
    source_run: release_draft.WorkflowRun,
    verified_draft: release_draft.VerifiedDraft,
) -> release_hosted_validation.HostedValidationReceipt:
    """Download and bind the exact successful hosted-validation receipt."""

    binding = options.hosted_validation
    if binding is None:
        raise FinalizationError(
            "publication requires --hosted-workflow-id, --hosted-run-id, "
            "--hosted-run-attempt, and --hosted-receipt-artifact-id"
        )
    workflow = source.read_hosted_validation_workflow(options, binding)
    run = source.read_hosted_validation_run(options, binding)
    if run.workflow_id != workflow.workflow_id:
        raise FinalizationError(
            "hosted validation run differs from the canonical workflow identity"
        )
    record = source.read_hosted_receipt_artifact(options, binding, run)
    cache = private_subdirectory(attempt_root, "hosted-receipt-cache")
    output = private_subdirectory(attempt_root, "hosted-receipt")
    try:
        receipt_path = release_artifact_download.download_public_archive(
            options.repository,
            record,
            HOSTED_RECEIPT_FILENAME,
            cache,
            output,
            release_artifact_download.GitHubArtifactZipDownloader(),
        )
        raw_receipt = read_receipt(receipt_path)
        receipt = release_hosted_validation.parse_receipt(
            json.dumps(
                raw_receipt,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            contract=contract,
        )
        release_hosted_validation.bind_receipt(
            receipt,
            repository=options.repository,
            source_run=source_run,
            validation_run=run,
            release_id=verified_draft.release_id,
            release_database_id=verified_draft.release_database_id,
            release_body=verified_draft.expected_body,
            identity_sha1=options.identity_sha1,
            local_assets=verified_draft.assets,
        )
        receipt_sha256 = sha256_file(
            receipt_path,
            label="hosted validation receipt",
            require_single_link=True,
        )
    except (
        release_artifact_download.ArtifactDownloadError,
        release_hosted_validation.HostedValidationError,
        ReleaseAttemptError,
        ReleaseFileError,
        TypeError,
        ValueError,
    ) as error:
        raise FinalizationError(
            f"hosted validation receipt could not be bound: {error}"
        ) from error
    ensure_receipt(
        attempt_root,
        "hosted-validation-verified.json",
        {
            "workflow_id": run.workflow_id,
            "workflow_name": workflow.name,
            "workflow_path": workflow.path,
            "workflow_state": workflow.state,
            "run_id": run.run_id,
            "run_attempt": run.attempt,
            "receipt_artifact_id": record.artifact_id,
            "receipt_artifact_name": record.name,
            "receipt_artifact_size": record.size_in_bytes,
            "receipt_artifact_sha256": record.archive_sha256,
            "receipt_sha256": receipt_sha256,
            "release_id": verified_draft.release_id,
            "release_database_id": verified_draft.release_database_id,
            "assets": staging.asset_receipt_summary(verified_draft.assets),
            "receipt": release_hosted_validation.receipt_payload(receipt),
        },
    )
    return receipt


def require_persisted_hosted_validation(
    options: FinalizationOptions,
    contract: ReleaseContract,
    attempt_root: Path,
    verified_draft: release_draft.VerifiedDraft,
) -> release_hosted_validation.HostedValidationReceipt:
    """Require the durable local proof written before publication intent."""

    binding = options.hosted_validation
    if binding is None:
        raise FinalizationError("hosted validation binding is missing")
    path = attempt_root / "hosted-validation-verified.json"
    if not os.path.lexists(path):
        raise FinalizationError("hosted validation verification receipt is missing")
    value = read_receipt(path)
    expected_fields = {
        "workflow_id",
        "workflow_name",
        "workflow_path",
        "workflow_state",
        "run_id",
        "run_attempt",
        "receipt_artifact_id",
        "receipt_artifact_name",
        "receipt_artifact_size",
        "receipt_artifact_sha256",
        "receipt_sha256",
        "release_id",
        "release_database_id",
        "assets",
        "receipt",
    }
    if set(value) != expected_fields:
        raise FinalizationError("persisted hosted validation receipt is malformed")
    expected_values = {
        "workflow_id": binding.workflow_id,
        "workflow_name": release_hosted_validation.EXPECTED_WORKFLOW_NAME,
        "workflow_path": release_hosted_validation.EXPECTED_WORKFLOW_PATH,
        "workflow_state": "active",
        "run_id": binding.run_id,
        "run_attempt": binding.run_attempt,
        "receipt_artifact_id": binding.receipt_artifact_id,
        "receipt_artifact_name": release_hosted_validation.receipt_artifact_name(
            release_draft.WorkflowRun(
                workflow_id=binding.workflow_id,
                run_id=binding.run_id,
                attempt=binding.run_attempt,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
        ),
        "release_id": verified_draft.release_id,
        "release_database_id": verified_draft.release_database_id,
        "assets": staging.asset_receipt_summary(verified_draft.assets),
    }
    for field, expected in expected_values.items():
        if value[field] != expected:
            raise FinalizationError(
                f"persisted hosted validation receipt differs at {field}"
            )
    for field in ("receipt_artifact_size",):
        field_value = value[field]
        if (
            isinstance(field_value, bool)
            or not isinstance(field_value, int)
            or field_value <= 0
        ):
            raise FinalizationError(
                f"persisted hosted validation receipt has invalid {field}"
            )
    for field in ("receipt_artifact_sha256", "receipt_sha256"):
        if (
            not isinstance(value[field], str)
            or release_hosted_validation.SHA256.fullmatch(value[field]) is None
        ):
            raise FinalizationError(
                f"persisted hosted validation receipt has invalid {field}"
            )
    raw_receipt = value["receipt"]
    if not isinstance(raw_receipt, dict):
        raise FinalizationError("persisted hosted validation receipt body is malformed")
    try:
        canonical_receipt = release_hosted_validation.canonical_json(
            raw_receipt
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise FinalizationError(
            f"persisted hosted validation receipt is not canonical JSON: {error}"
        ) from error
    if hashlib.sha256(canonical_receipt).hexdigest() != value["receipt_sha256"]:
        raise FinalizationError(
            "persisted hosted validation receipt body differs from its artifact digest"
        )
    try:
        receipt = release_hosted_validation.parse_receipt(
            json.dumps(
                raw_receipt,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            contract=contract,
        )
        release_hosted_validation.bind_receipt(
            receipt,
            repository=options.repository,
            source_run=verified_draft.run,
            validation_run=release_draft.WorkflowRun(
                workflow_id=binding.workflow_id,
                run_id=binding.run_id,
                attempt=binding.run_attempt,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            ),
            release_id=verified_draft.release_id,
            release_database_id=verified_draft.release_database_id,
            release_body=verified_draft.expected_body,
            identity_sha1=options.identity_sha1,
            local_assets=verified_draft.assets,
        )
    except release_hosted_validation.HostedValidationError as error:
        raise FinalizationError(
            f"persisted hosted validation receipt could not be bound: {error}"
        ) from error
    return receipt


def require_prepublication_state(
    options: FinalizationOptions,
    contract: ReleaseContract,
    attempt_root: Path,
    verified_draft: release_draft.VerifiedDraft,
) -> None:
    """Rebind every mutable GitHub input immediately before the sole PATCH."""

    source_run = source.read_workflow_run(options)
    if source_run != verified_draft.run:
        raise FinalizationError(
            "source workflow run changed at the publication boundary"
        )
    source.validate_remote_tag(options)

    binding = options.hosted_validation
    if binding is None:
        raise FinalizationError("hosted validation binding is missing")
    workflow = source.read_hosted_validation_workflow(options, binding)
    validation_run = source.read_hosted_validation_run(options, binding)
    if validation_run.workflow_id != workflow.workflow_id:
        raise FinalizationError(
            "hosted validation run differs from the canonical workflow identity"
        )
    receipt = require_persisted_hosted_validation(
        options,
        contract,
        attempt_root,
        verified_draft,
    )

    try:
        release = release_draft.parse_release(
            staging.read_release_json(options),
            expected_tag=verified_draft.run.tag,
        )
        plan = release_draft.plan_draft_uploads(
            verified_draft.repository,
            verified_draft.run,
            release,
            verified_draft.assets,
            expected_body=verified_draft.expected_body,
        )
        refreshed = release_draft.verify_complete_draft(plan, release)
    except release_draft.ReleaseDraftError as error:
        raise FinalizationError(
            f"pre-publication draft validation failed: {error}"
        ) from error
    if refreshed != verified_draft:
        raise FinalizationError(
            "pre-publication draft differs from the verified release capability"
        )
    numeric_release = staging.read_numeric_draft_release(
        options,
        verified_draft.release_database_id,
    )
    try:
        release_hosted_validation.require_exact_draft_release(
            numeric_release,
            expected_release=receipt.release,
            expected_assets=receipt.assets,
        )
    except release_hosted_validation.HostedValidationError as error:
        raise FinalizationError(
            f"numeric draft differs from the hosted validation receipt: {error}"
        ) from error
    source.require_immutable_release_before_publication(options)
