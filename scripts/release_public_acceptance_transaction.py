"""Persist the anonymous public-release acceptance transaction."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path

import release_draft
import release_git_tag
import release_public_acceptance
from release_attempt import ReleaseAttemptError, read_receipt
from release_finalization_core import FinalizationError, FinalizationOptions, ensure_receipt
from release_targets import ReleaseContract


PUBLIC_VERIFICATION_RECEIPT_FILENAME = "public-verified.json"


def accept_public_release(
    options: FinalizationOptions,
    contract: ReleaseContract,
    attempt_root: Path,
    remote_tag: release_git_tag.RemoteTagBinding,
    publication: release_draft.PublicationReceipt,
    all_assets: tuple[release_draft.LocalAsset, ...],
    notes: str,
) -> release_public_acceptance.PublicAcceptanceReceipt:
    """Prove the immutable public bytes through the anonymous API boundary."""

    if not os.path.lexists(attempt_root / "published.json"):
        raise release_public_acceptance.PublicButUnverifiedError(
            "public acceptance requires the durable publication receipt"
        )
    if (
        remote_tag.tag != publication.tag
        or remote_tag.commit_sha != publication.head_sha
    ):
        raise release_public_acceptance.PublicButUnverifiedError(
            "public acceptance tag binding differs from the publication receipt"
        )
    request = release_public_acceptance.PublicAcceptanceRequest(
        repository=options.repository,
        release_id=publication.release_database_id,
        tag=publication.tag,
        expected_tag_object_sha=remote_tag.tag_object_sha,
        expected_head_sha=publication.head_sha,
        name=f"App Icon Toolkit {publication.tag}",
        body=notes,
        local_assets=all_assets,
        contract=contract,
        plugin_root=options.plugin_root,
        identity_sha1=options.identity_sha1,
    )
    receipt = release_public_acceptance.verify_public_release(request)
    try:
        hosted_validation = read_receipt(
            attempt_root / "hosted-validation-verified.json"
        )
    except ReleaseAttemptError as error:
        raise release_public_acceptance.PublicButUnverifiedError(
            f"hosted validation receipt cannot be rebound after publication: {error}"
        ) from error
    try:
        ensure_receipt(
            attempt_root,
            PUBLIC_VERIFICATION_RECEIPT_FILENAME,
            {
                "schema_version": 1,
                "attempt_binding": asdict(options.binding),
                "publication": asdict(publication),
                "hosted_validation": hosted_validation,
                "public_acceptance": receipt.to_json_value(),
            },
        )
    except (FinalizationError, ReleaseAttemptError) as error:
        raise release_public_acceptance.PublicButUnverifiedError(
            f"public verification receipt could not be persisted: {error}"
        ) from error
    return receipt
