"""Draft lookup, asset staging, and verified-draft persistence."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
from typing import Any, Sequence

import release_draft
import release_hosted_validation
from release_attempt import read_receipt
from release_finalization_core import (
    ExternalMutationOutcomeUnknown,
    FinalizationError,
    FinalizationOptions,
    GitHubRunner,
    ensure_receipt,
    required_command,
)


UPLOAD_INTENT_SCHEMA_VERSION = 1


def _upload_intents(
    plan: release_draft.DraftUploadPlan,
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Bind each possible asset POST to one deterministic append-only receipt."""

    intents: dict[str, tuple[str, dict[str, Any]]] = {}
    for index, asset in enumerate(sorted(plan.assets, key=lambda item: item.name)):
        intents[asset.name] = (
            f"github-upload-intent-{index:03d}.json",
            {
                "schema_version": UPLOAD_INTENT_SCHEMA_VERSION,
                "repository": plan.repository,
                "release_id": plan.release_id,
                "release_database_id": plan.release_database_id,
                "run": asdict(plan.run),
                "asset": asdict(asset) | {"path": asset.path.name},
            },
        )
    return intents


def read_release_json(options: FinalizationOptions) -> str:
    return required_command(
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


def read_numeric_draft_release(
    options: FinalizationOptions,
    release_database_id: int,
) -> release_hosted_validation.DraftRelease:
    """Read the exact numeric REST release, including numeric asset identities."""

    if (
        isinstance(release_database_id, bool)
        or not isinstance(release_database_id, int)
        or release_database_id <= 0
    ):
        raise FinalizationError("numeric draft release ID must be positive")
    payload = required_command(
        (
            "gh",
            "api",
            "--hostname",
            "github.com",
            f"repos/{options.repository}/releases/{release_database_id}",
        )
    )
    try:
        return release_hosted_validation.parse_draft_release(
            payload,
            expected_tag=options.binding.tag,
        )
    except release_hosted_validation.HostedValidationError as error:
        raise FinalizationError(
            f"numeric draft release could not be validated: {error}"
        ) from error


def persisted_verified_draft_identity(
    path: Path,
    assets: Sequence[release_draft.LocalAsset],
) -> tuple[str, int]:
    """Bind public recovery to the exact release verified before PATCH."""

    value = read_receipt(path)
    if set(value) != {"release_id", "release_database_id", "assets"}:
        raise FinalizationError("persisted verified draft receipt is malformed")
    release_id = value["release_id"]
    release_database_id = value["release_database_id"]
    if not isinstance(release_id, str) or not release_id or len(release_id) > 512:
        raise FinalizationError("persisted verified draft receipt has an invalid release ID")
    if (
        isinstance(release_database_id, bool)
        or not isinstance(release_database_id, int)
        or release_database_id <= 0
    ):
        raise FinalizationError(
            "persisted verified draft receipt has an invalid numeric release ID"
        )
    expected_assets = asset_receipt_summary(assets)
    if value["assets"] != expected_assets:
        raise FinalizationError(
            "persisted verified draft receipt differs from the prepared assets"
        )
    return release_id, release_database_id


def asset_receipt_summary(
    assets: Sequence[release_draft.LocalAsset],
) -> list[dict[str, Any]]:
    """Return the complete stable asset identity stored at publication gates."""

    return [asdict(asset) | {"path": asset.path.name} for asset in assets]


def stage_assets(
    options: FinalizationOptions,
    attempt_root: Path,
    run: release_draft.WorkflowRun,
    all_assets: tuple[release_draft.LocalAsset, ...],
    notes: str,
    release_snapshot: release_draft.ReleaseSnapshot | None = None,
) -> release_draft.VerifiedDraft:
    """Upload and verify the exact assets while keeping the release a draft."""

    if not os.path.lexists(attempt_root / "notarized.json"):
        raise FinalizationError("notarization phase receipt is missing")
    if os.path.lexists(attempt_root / "github-publication-intent.json"):
        raise FinalizationError(
            "publication intent already exists; staging cannot alter that attempt"
        )
    verified_path = attempt_root / "github-assets-verified.json"
    persisted_identity = (
        persisted_verified_draft_identity(verified_path, all_assets)
        if os.path.lexists(verified_path)
        else None
    )
    try:
        release = release_snapshot
        if release is None:
            release = release_draft.parse_release(
                read_release_json(options),
                expected_tag=options.binding.tag,
            )
        if not release.is_draft:
            raise FinalizationError(
                "signed assets can only be staged to an unpublished draft"
            )
        plan = release_draft.plan_draft_uploads(
            options.repository,
            run,
            release,
            all_assets,
            expected_body=notes,
        )
        if persisted_identity is not None:
            if persisted_identity != (release.release_id, release.database_id):
                raise FinalizationError(
                    "live draft identity differs from the persisted verified draft"
                )
            return release_draft.verify_complete_draft(plan, release)

        unknown_path = attempt_root / "github-upload-unknown.json"
        intents = _upload_intents(plan)
        pending_prior_intents: list[str] = []
        for asset_name, (receipt_name, payload) in intents.items():
            receipt_path = attempt_root / receipt_name
            if not os.path.lexists(receipt_path):
                continue
            ensure_receipt(attempt_root, receipt_name, payload)
            if asset_name in plan.missing_names:
                pending_prior_intents.append(asset_name)
        if (
            plan.missing_names
            and (os.path.lexists(unknown_path) or pending_prior_intents)
            and not options.reconcile_github_upload
        ):
            names = sorted(pending_prior_intents or plan.missing_names)
            raise ExternalMutationOutcomeUnknown(
                "a prior GitHub asset upload is outcome-unknown for "
                f"{names}; inspect the numeric draft and pass --reconcile-github-upload "
                "to authorize only the reconciled remainder"
            )

        def persist_upload_intent(asset: release_draft.LocalAsset) -> None:
            receipt_name, payload = intents[asset.name]
            ensure_receipt(attempt_root, receipt_name, payload)

        try:
            release_draft.run_uploads(
                plan,
                GitHubRunner(),
                before_upload=persist_upload_intent,
            )
        except release_draft.MutationOutcomeUnknown as error:
            ensure_receipt(
                attempt_root,
                unknown_path.name,
                {"release_database_id": plan.release_database_id, "error": str(error)},
            )
            raise ExternalMutationOutcomeUnknown(str(error)) from error
        refreshed = release_draft.parse_release(
            read_release_json(options),
            expected_tag=options.binding.tag,
        )
        verified = release_draft.verify_complete_draft(plan, refreshed)
        ensure_receipt(
            attempt_root,
            verified_path.name,
            {
                "release_id": verified.release_id,
                "release_database_id": verified.release_database_id,
                "assets": asset_receipt_summary(all_assets),
            },
        )
        return verified
    except (FinalizationError, ExternalMutationOutcomeUnknown):
        raise
    except release_draft.ReleaseDraftError as error:
        raise FinalizationError(str(error)) from error
