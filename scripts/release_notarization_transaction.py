"""Crash-recoverable Apple notarization transaction for prepared macOS assets."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
from typing import Any, Sequence

import macos_signing
from release_attempt import private_subdirectory, read_receipt
from release_files import sha256_file
from release_finalization_core import (
    AuditedMacRunner,
    ExternalMutationOutcomeUnknown,
    FinalizationError,
    FinalizationOptions,
    MACOS_FAMILIES,
    ensure_receipt,
)
from release_targets import ReleaseContract, ReleaseTarget


def _submission_from_receipt(path: Path, expected_sha256: str) -> macos_signing.NotarySubmission:
    value = read_receipt(path)
    if set(value) != {"job_id", "archive_sha256"}:
        raise FinalizationError(f"notary submission receipt is malformed: {path}")
    submission = macos_signing.NotarySubmission(
        job_id=value["job_id"],
        archive_sha256=value["archive_sha256"],
    )
    if submission.archive_sha256 != expected_sha256:
        raise FinalizationError("notary submission receipt archive digest differs")
    return submission


def _preflight_adopted_submissions(
    options: FinalizationOptions,
    mac_targets: Sequence[ReleaseTarget],
    work: Path,
    assets: Path,
) -> None:
    """Reject every unconsumable adoption before any new Apple submission."""

    by_id = {target.id: target for target in mac_targets}
    unsupported = set(options.adopted_submissions) - set(by_id)
    if unsupported:
        raise FinalizationError(
            "--adopt-submission names an unsupported or non-macOS target"
        )
    for target_id, adopted_job_id in options.adopted_submissions.items():
        target = by_id[target_id]
        target_root = private_subdirectory(work, target.id)
        archive = assets / target.release_filename(options.binding.tag)
        archive_sha256 = sha256_file(
            archive,
            label=f"notarization archive {target.id}",
        )
        intent_path = target_root / "notary-submission-intent.json"
        submission_path = target_root / "notary-submission.json"
        if not os.path.lexists(intent_path):
            raise FinalizationError(
                f"--adopt-submission for {target.id} has no existing submission intent"
            )
        ensure_receipt(
            target_root,
            intent_path.name,
            {"target": target.id, "archive_sha256": archive_sha256},
        )
        if os.path.lexists(submission_path):
            existing = _submission_from_receipt(submission_path, archive_sha256)
            if existing.job_id != adopted_job_id:
                raise FinalizationError(
                    f"--adopt-submission for {target.id} differs from the existing job ID"
                )


def notarize_assets(
    options: FinalizationOptions,
    contract: ReleaseContract,
    attempt_root: Path,
    assets: Path,
) -> None:
    if not os.path.lexists(attempt_root / "prepared.json"):
        raise FinalizationError("prepare phase receipt is missing")
    work = private_subdirectory(attempt_root, "macos-work")
    accepted_targets: list[dict[str, Any]] = []
    mac_targets = tuple(target for target in contract.targets if target.family in MACOS_FAMILIES)
    _preflight_adopted_submissions(options, mac_targets, work, assets)
    consumed_adoptions: set[str] = set()

    for target in mac_targets:
        target_root = private_subdirectory(work, target.id)
        audit = private_subdirectory(target_root, "apple-command-receipts")
        runner = AuditedMacRunner(audit)
        archive = assets / target.release_filename(options.binding.tag)
        archive_sha256 = sha256_file(
            archive,
            label=f"notarization archive {target.id}",
        )
        intent_path = target_root / "notary-submission-intent.json"
        intent_existed = os.path.lexists(intent_path)
        ensure_receipt(
            target_root,
            intent_path.name,
            {"target": target.id, "archive_sha256": archive_sha256},
        )
        submission_path = target_root / "notary-submission.json"
        adopted = options.adopted_submissions.get(target.id)
        if os.path.lexists(submission_path):
            submission = _submission_from_receipt(submission_path, archive_sha256)
            if adopted is not None:
                if submission.job_id != adopted:
                    raise FinalizationError(
                        f"--adopt-submission for {target.id} differs from the existing job ID"
                    )
                consumed_adoptions.add(target.id)
        elif intent_existed:
            if adopted is None:
                raise ExternalMutationOutcomeUnknown(
                    f"notary submission for {target.id} has intent but no job ID; "
                    "reconcile Apple history and pass --adopt-submission TARGET=UUID"
                )
            submission = macos_signing.NotarySubmission(
                job_id=adopted,
                archive_sha256=archive_sha256,
            )
            ensure_receipt(target_root, submission_path.name, asdict(submission))
            consumed_adoptions.add(target.id)
        else:
            if adopted is not None:
                raise FinalizationError(
                    f"--adopt-submission for {target.id} has no existing submission intent"
                )
            try:
                submission = macos_signing.submit_notarization(
                    archive,
                    keychain_profile=options.notary_profile,
                    runner=runner,
                )
            except macos_signing.SubmissionOutcomeUnknown as error:
                raise ExternalMutationOutcomeUnknown(str(error)) from error
            ensure_receipt(target_root, submission_path.name, asdict(submission))

        receipt = macos_signing.verify_accepted_notarization(
            archive,
            submission,
            keychain_profile=options.notary_profile,
            timeout=options.notary_timeout,
            runner=runner,
        )
        candidate_binary = (
            target_root / "candidate" / "app-icon-toolkit" / "bin" / target.binary_name
        )
        macos_signing.check_notarization_ticket(candidate_binary, runner)
        accepted = {
            "target": target.id,
            "notarization": asdict(receipt),
            "ticket_binary_sha256": sha256_file(
                candidate_binary,
                label=f"notarized {target.id} binary",
            ),
        }
        ensure_receipt(target_root, "notary-accepted.json", accepted)
        accepted_targets.append(accepted)

    if consumed_adoptions != set(options.adopted_submissions):
        raise FinalizationError("not every --adopt-submission request was consumed exactly")

    ensure_receipt(
        attempt_root,
        "notarized.json",
        {"binding": asdict(options.binding), "targets": accepted_targets},
    )
