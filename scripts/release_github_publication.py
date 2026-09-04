"""The sole GitHub release PATCH transaction and its recovery semantics."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
from typing import Any, Sequence

import release_draft
import release_draft_staging as staging
import release_publication_preflight as preflight
from release_attempt import read_receipt
from release_finalization_core import (
    ExternalMutationOutcomeUnknown,
    FinalizationError,
    FinalizationOptions,
    GitHubRunner,
    ensure_receipt,
)
from release_targets import ReleaseContract


def _validate_public_release(
    release: release_draft.ReleaseSnapshot,
    run: release_draft.WorkflowRun,
    notes: str,
    assets: Sequence[release_draft.LocalAsset],
) -> None:
    if release.is_draft or release.is_prerelease:
        raise FinalizationError("release is not a stable public release")
    if release.name != f"App Icon Toolkit {run.tag}" or release.body != notes:
        raise FinalizationError("public release metadata differs from the tagged contract")
    local = {asset.name: asset for asset in assets}
    remote = {asset.name: asset for asset in release.assets}
    if set(local) != set(remote):
        raise FinalizationError("public release asset names differ from the prepared set")
    for name, expected in local.items():
        actual = remote[name]
        if actual.size != expected.size or actual.sha256 != expected.sha256:
            raise FinalizationError(f"public release asset differs from prepared bytes: {name}")


def _persisted_publication_receipt(
    path: Path,
    options: FinalizationOptions,
    run: release_draft.WorkflowRun,
) -> release_draft.PublicationReceipt:
    """Parse and bind an immutable publication receipt to this attempt."""

    value = read_receipt(path)
    expected_fields = {
        "repository",
        "release_id",
        "release_database_id",
        "tag",
        "head_sha",
        "workflow_id",
        "run_id",
        "run_attempt",
        "reconciled_after_unknown_mutation",
    }
    if set(value) != expected_fields:
        raise FinalizationError("persisted publication receipt is malformed")
    release_id = value["release_id"]
    release_database_id = value["release_database_id"]
    reconciled = value["reconciled_after_unknown_mutation"]
    for field in ("repository", "tag", "head_sha"):
        if not isinstance(value[field], str) or not value[field]:
            raise FinalizationError(
                f"persisted publication receipt has an invalid {field}"
            )
    for field in ("workflow_id", "run_id", "run_attempt"):
        field_value = value[field]
        if (
            isinstance(field_value, bool)
            or not isinstance(field_value, int)
            or field_value <= 0
        ):
            raise FinalizationError(
                f"persisted publication receipt has an invalid {field}"
            )
    if not isinstance(release_id, str) or not release_id or len(release_id) > 512:
        raise FinalizationError("persisted publication receipt has an invalid release ID")
    if (
        isinstance(release_database_id, bool)
        or not isinstance(release_database_id, int)
        or release_database_id <= 0
    ):
        raise FinalizationError(
            "persisted publication receipt has an invalid numeric release ID"
        )
    if not isinstance(reconciled, bool):
        raise FinalizationError(
            "persisted publication receipt has an invalid reconciliation state"
        )
    receipt = release_draft.PublicationReceipt(
        repository=value["repository"],
        release_id=release_id,
        release_database_id=release_database_id,
        tag=value["tag"],
        head_sha=value["head_sha"],
        workflow_id=value["workflow_id"],
        run_id=value["run_id"],
        run_attempt=value["run_attempt"],
        reconciled_after_unknown_mutation=reconciled,
    )
    if (
        receipt.repository != options.repository
        or receipt.tag != run.tag
        or receipt.head_sha != run.head_sha
        or receipt.workflow_id != run.workflow_id
        or receipt.run_id != run.run_id
        or receipt.run_attempt != run.attempt
    ):
        raise FinalizationError(
            "persisted publication receipt differs from the release attempt binding"
        )
    return receipt


def _publication_intent_payload(
    verified: release_draft.VerifiedDraft,
) -> dict[str, Any]:
    """Bind the sole irreversible GitHub PATCH to its verified draft state."""

    return {
        "repository": verified.repository,
        "release_id": verified.release_id,
        "release_database_id": verified.release_database_id,
        "tag": verified.run.tag,
        "head_sha": verified.run.head_sha,
        "workflow_id": verified.run.workflow_id,
        "run_id": verified.run.run_id,
        "run_attempt": verified.run.attempt,
        "expected_body": verified.expected_body,
        "assets": staging.asset_receipt_summary(verified.assets),
    }


def _persisted_publication_intent(
    path: Path,
    options: FinalizationOptions,
    run: release_draft.WorkflowRun,
    assets: Sequence[release_draft.LocalAsset],
    notes: str,
) -> release_draft.VerifiedDraft:
    """Parse and bind a durable authorization record for one numeric release."""

    value = read_receipt(path)
    expected_fields = {
        "repository",
        "release_id",
        "release_database_id",
        "tag",
        "head_sha",
        "workflow_id",
        "run_id",
        "run_attempt",
        "expected_body",
        "assets",
    }
    if set(value) != expected_fields:
        raise FinalizationError("persisted publication intent is malformed")
    release_id = value["release_id"]
    release_database_id = value["release_database_id"]
    if not isinstance(release_id, str) or not release_id or len(release_id) > 512:
        raise FinalizationError("persisted publication intent has an invalid release ID")
    if (
        isinstance(release_database_id, bool)
        or not isinstance(release_database_id, int)
        or release_database_id <= 0
    ):
        raise FinalizationError(
            "persisted publication intent has an invalid numeric release ID"
        )
    for field in ("repository", "tag", "head_sha", "expected_body"):
        if not isinstance(value[field], str) or not value[field]:
            raise FinalizationError(
                f"persisted publication intent has an invalid {field}"
            )
    for field in ("workflow_id", "run_id", "run_attempt"):
        field_value = value[field]
        if (
            isinstance(field_value, bool)
            or not isinstance(field_value, int)
            or field_value <= 0
        ):
            raise FinalizationError(
                f"persisted publication intent has an invalid {field}"
            )
    verified = release_draft.VerifiedDraft(
        repository=value["repository"],
        run=release_draft.WorkflowRun(
            workflow_id=value["workflow_id"],
            run_id=value["run_id"],
            attempt=value["run_attempt"],
            tag=value["tag"],
            head_sha=value["head_sha"],
        ),
        release_id=release_id,
        release_database_id=release_database_id,
        expected_body=value["expected_body"],
        assets=tuple(assets),
    )
    if (
        verified.repository != options.repository
        or verified.run != run
        or verified.expected_body != notes
        or value["assets"] != staging.asset_receipt_summary(assets)
    ):
        raise FinalizationError(
            "persisted publication intent differs from the release attempt binding"
        )
    return verified


def publish_assets(
    options: FinalizationOptions,
    contract: ReleaseContract,
    attempt_root: Path,
    run: release_draft.WorkflowRun,
    all_assets: tuple[release_draft.LocalAsset, ...],
    notes: str,
) -> release_draft.PublicationReceipt:
    if not os.path.lexists(attempt_root / "notarized.json"):
        raise FinalizationError("notarization phase receipt is missing")
    publication_path = attempt_root / "published.json"
    persisted = (
        _persisted_publication_receipt(publication_path, options, run)
        if os.path.lexists(publication_path)
        else None
    )
    verified_draft_path = attempt_root / "github-assets-verified.json"
    verified_draft_identity = (
        staging.persisted_verified_draft_identity(verified_draft_path, all_assets)
        if os.path.lexists(verified_draft_path)
        else None
    )
    publication_intent_path = attempt_root / "github-publication-intent.json"
    publication_intent = (
        _persisted_publication_intent(
            publication_intent_path,
            options,
            run,
            all_assets,
            notes,
        )
        if os.path.lexists(publication_intent_path)
        else None
    )
    if publication_intent is not None:
        intent_identity = (
            publication_intent.release_id,
            publication_intent.release_database_id,
        )
        if verified_draft_identity is None:
            raise FinalizationError(
                "persisted publication intent exists without a verified draft receipt"
            )
        if verified_draft_identity != intent_identity:
            raise FinalizationError(
                "persisted publication intent differs from the verified draft identity"
            )
    try:
        release_payload = staging.read_release_json(options)
    except FinalizationError as error:
        if publication_intent is not None and persisted is None:
            raise ExternalMutationOutcomeUnknown(
                "a prior GitHub publication intent exists, but the release could not "
                "be read for reconciliation; no mutation was attempted"
            ) from error
        raise
    try:
        release = release_draft.parse_release(
            release_payload,
            expected_tag=options.binding.tag,
        )
    except release_draft.ReleaseDraftError as error:
        if publication_intent is not None and persisted is None:
            raise ExternalMutationOutcomeUnknown(
                "a prior GitHub publication intent exists, but the release response "
                "could not be validated; no mutation was attempted"
            ) from error
        raise FinalizationError(str(error)) from error

    if not release.is_draft:
        _validate_public_release(release, run, notes, all_assets)
        preflight.require_persisted_hosted_validation(
            options,
            contract,
            attempt_root,
            release_draft.VerifiedDraft(
                repository=options.repository,
                run=run,
                release_id=release.release_id,
                release_database_id=release.database_id,
                expected_body=notes,
                assets=all_assets,
            ),
        )
        if publication_intent is not None and (
            publication_intent.release_id != release.release_id
            or publication_intent.release_database_id != release.database_id
        ):
            raise FinalizationError(
                "public release identity differs from the persisted publication intent"
            )
        if verified_draft_identity is not None and verified_draft_identity != (
            release.release_id,
            release.database_id,
        ):
            raise FinalizationError(
                "public release identity differs from the persisted verified draft receipt"
            )
        if persisted is not None:
            if (
                persisted.release_id != release.release_id
                or persisted.release_database_id != release.database_id
            ):
                raise FinalizationError(
                    "public release identity differs from the persisted publication receipt"
                )
            return persisted
        if publication_intent is None:
            raise FinalizationError(
                "public release has no persisted publication intent for this attempt"
            )
        receipt = release_draft.PublicationReceipt(
            repository=options.repository,
            release_id=release.release_id,
            release_database_id=release.database_id,
            tag=run.tag,
            head_sha=run.head_sha,
            workflow_id=run.workflow_id,
            run_id=run.run_id,
            run_attempt=run.attempt,
            reconciled_after_unknown_mutation=True,
        )
        ensure_receipt(attempt_root, "published.json", asdict(receipt))
        return receipt

    if persisted is not None:
        raise FinalizationError(
            "persisted publication receipt exists but the bound release is still a draft"
        )

    try:
        if publication_intent is not None:
            try:
                plan = release_draft.plan_draft_uploads(
                    options.repository,
                    run,
                    release,
                    all_assets,
                    expected_body=notes,
                )
            except release_draft.ReleaseDraftError as error:
                raise ExternalMutationOutcomeUnknown(
                    "a prior GitHub publication intent exists, but the live draft no "
                    "longer matches its publication contract; no mutation was attempted"
                ) from error
            if (
                publication_intent.release_id != release.release_id
                or publication_intent.release_database_id != release.database_id
            ):
                raise FinalizationError(
                    "draft release identity differs from the persisted publication intent"
                )
            try:
                verified = release_draft.verify_complete_draft(plan, release)
            except release_draft.ReleaseDraftError as error:
                raise ExternalMutationOutcomeUnknown(
                    "a prior GitHub publication intent exists, but the live draft no "
                    "longer matches its complete asset contract; no mutation was attempted"
                ) from error
            if not options.reconcile_github_publish:
                raise ExternalMutationOutcomeUnknown(
                    "a prior GitHub publication is outcome-unknown; the exact release is "
                    "still a draft, so no mutation was attempted. Pass "
                    "--reconcile-github-publish to authorize one explicit retry"
                )
            preflight.verify_hosted_validation(
                options,
                contract,
                attempt_root,
                run,
                verified,
            )
            try:
                receipt = release_draft.publish_verified_draft(
                    verified,
                    GitHubRunner(),
                    lambda: staging.read_release_json(options),
                    before_mutation=lambda: preflight.require_prepublication_state(
                        options,
                        contract,
                        attempt_root,
                        verified,
                    ),
                )
            except release_draft.PublicationOutcomeUnknown as error:
                raise ExternalMutationOutcomeUnknown(str(error)) from error
            except release_draft.ReleaseDraftError as error:
                raise ExternalMutationOutcomeUnknown(
                    "the explicitly retried GitHub publication did not reach a "
                    "trustworthy terminal state"
                ) from error
            receipt = release_draft.PublicationReceipt(
                repository=receipt.repository,
                release_id=receipt.release_id,
                release_database_id=receipt.release_database_id,
                tag=receipt.tag,
                head_sha=receipt.head_sha,
                workflow_id=receipt.workflow_id,
                run_id=receipt.run_id,
                run_attempt=receipt.run_attempt,
                reconciled_after_unknown_mutation=True,
            )
            ensure_receipt(attempt_root, "published.json", asdict(receipt))
            return receipt

        verified = staging.stage_assets(
            options,
            attempt_root,
            run,
            all_assets,
            notes,
            release,
        )
        preflight.verify_hosted_validation(
            options,
            contract,
            attempt_root,
            run,
            verified,
        )
        ensure_receipt(
            attempt_root,
            publication_intent_path.name,
            _publication_intent_payload(verified),
        )
        receipt = release_draft.publish_verified_draft(
            verified,
            GitHubRunner(),
            lambda: staging.read_release_json(options),
            before_mutation=lambda: preflight.require_prepublication_state(
                options,
                contract,
                attempt_root,
                verified,
            ),
        )
    except release_draft.PublicationOutcomeUnknown as error:
        raise ExternalMutationOutcomeUnknown(str(error)) from error
    except release_draft.ReleaseDraftError as error:
        raise FinalizationError(str(error)) from error
    ensure_receipt(attempt_root, "published.json", asdict(receipt))
    return receipt
