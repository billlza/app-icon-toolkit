"""Canonical source admission and exact release-candidate acquisition."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import sys
from typing import Any, Sequence

import release_artifact_download
import release_artifacts
import release_draft
import release_git_tag
import release_hosted_validation
import release_immutability
from release_attempt import private_subdirectory
from release_files import sha256_file
from release_finalization_core import (
    FinalizationError,
    FinalizationOptions,
    HostedValidationInput,
    archive_paths,
    ensure_receipt,
    required_command,
)
from release_targets import ReleaseContract, verify_release_assets


WORKFLOW_RUN_FIELDS = (
    "databaseId,workflowDatabaseId,attempt,headBranch,headSha,"
    "workflowName,event,status,conclusion"
)


def read_release_workflow(
    options: FinalizationOptions,
) -> release_draft.WorkflowIdentity:
    """Resolve the canonical active source workflow by repository path."""

    payload = required_command(
        (
            "gh",
            "api",
            "--hostname",
            "github.com",
            f"repos/{options.repository}/actions/workflows/release.yml",
        )
    )
    try:
        return release_draft.parse_workflow_identity(
            payload,
            expected_workflow_id=options.binding.workflow_database_id,
            expected_name=release_draft.EXPECTED_WORKFLOW,
            expected_path=release_draft.EXPECTED_WORKFLOW_PATH,
        )
    except release_draft.ReleaseDraftError as error:
        raise FinalizationError(str(error)) from error


def read_workflow_run(options: FinalizationOptions) -> release_draft.WorkflowRun:
    workflow = read_release_workflow(options)
    payload = required_command(
        (
            "gh",
            "run",
            "view",
            str(options.binding.run_id),
            "--repo",
            f"github.com/{options.repository}",
            "--json",
            WORKFLOW_RUN_FIELDS,
        )
    )
    try:
        run = release_draft.parse_workflow_run(
            payload,
            expected_workflow_id=options.binding.workflow_database_id,
            expected_run_id=options.binding.run_id,
            expected_attempt=options.binding.run_attempt,
            expected_tag=options.binding.tag,
            expected_head_sha=options.binding.head_sha,
        )
    except release_draft.ReleaseDraftError as error:
        raise FinalizationError(str(error)) from error
    if run.workflow_id != workflow.workflow_id:
        raise FinalizationError(
            "source workflow run differs from the canonical workflow identity"
        )
    return run


def read_hosted_validation_run(
    options: FinalizationOptions,
    binding: HostedValidationInput,
) -> release_draft.WorkflowRun:
    """Read and bind the exact completed secret-free validation run."""

    payload = required_command(
        (
            "gh",
            "run",
            "view",
            str(binding.run_id),
            "--repo",
            f"github.com/{options.repository}",
            "--json",
            WORKFLOW_RUN_FIELDS,
        )
    )
    try:
        return release_hosted_validation.parse_successful_validation_run(
            payload,
            expected_workflow_id=binding.workflow_id,
            expected_run_id=binding.run_id,
            expected_attempt=binding.run_attempt,
            expected_tag=options.binding.tag,
            expected_head_sha=options.binding.head_sha,
        )
    except release_hosted_validation.HostedValidationError as error:
        raise FinalizationError(str(error)) from error


def read_hosted_validation_workflow(
    options: FinalizationOptions,
    binding: HostedValidationInput,
) -> release_hosted_validation.HostedWorkflowIdentity:
    """Resolve the canonical active workflow by repository path."""

    payload = required_command(
        (
            "gh",
            "api",
            "--hostname",
            "github.com",
            f"repos/{options.repository}/actions/workflows/"
            "validate-signed-draft.yml",
        )
    )
    try:
        return release_hosted_validation.parse_hosted_workflow(
            payload,
            expected_workflow_id=binding.workflow_id,
        )
    except release_hosted_validation.HostedValidationError as error:
        raise FinalizationError(str(error)) from error


def read_hosted_receipt_artifact(
    options: FinalizationOptions,
    binding: HostedValidationInput,
    run: release_draft.WorkflowRun,
) -> release_artifacts.ArtifactRecord:
    """Read one numeric receipt artifact and require its API size and digest."""

    payload = required_command(
        (
            "gh",
            "api",
            "--hostname",
            "github.com",
            f"repos/{options.repository}/actions/artifacts/"
            f"{binding.receipt_artifact_id}",
        )
    )
    try:
        return release_hosted_validation.parse_receipt_artifact(
            payload,
            run=run,
            expected_artifact_id=binding.receipt_artifact_id,
        )
    except release_hosted_validation.HostedValidationError as error:
        raise FinalizationError(str(error)) from error


def read_release_immutability_policy(
    options: FinalizationOptions,
) -> release_immutability.ReleaseImmutabilityPolicy:
    """Require GitHub's repository-level immutable-release policy via read-only API."""

    try:
        repository = release_draft.validate_repository(options.repository)
    except release_draft.ReleaseDraftError as error:
        raise FinalizationError(str(error)) from error
    if repository != options.binding.repository:
        raise FinalizationError(
            "immutable-release policy repository differs from the attempt binding"
        )
    payload = required_command(
        (
            "gh",
            "api",
            "--hostname",
            "github.com",
            "-H",
            "X-GitHub-Api-Version:2026-03-10",
            f"repos/{repository}/immutable-releases",
        )
    )
    try:
        return release_immutability.parse_release_immutability_policy(payload)
    except release_immutability.ReleaseImmutabilityError as error:
        raise FinalizationError(str(error)) from error


def require_immutable_release_before_publication(
    options: FinalizationOptions,
) -> None:
    """Recheck the repository policy at the final irreversible boundary."""

    read_release_immutability_policy(options)


def read_artifact_inventory(
    options: FinalizationOptions,
    run: release_draft.WorkflowRun,
    expected_names: Sequence[str],
) -> tuple[release_artifacts.ArtifactRecord, ...]:
    payload = required_command(
        (
            "gh",
            "api",
            "--hostname",
            "github.com",
            f"repos/{options.repository}/actions/runs/{run.run_id}/artifacts?per_page=100",
        )
    )
    try:
        return release_artifacts.parse_artifact_inventory(
            payload,
            run=run,
            expected_names=expected_names,
        )
    except release_artifacts.ReleaseArtifactError as error:
        raise FinalizationError(str(error)) from error


def validate_checkout(options: FinalizationOptions) -> int:
    root = options.plugin_root
    status = required_command(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=root,
    )
    if status.strip():
        raise FinalizationError("release finalization requires a clean checkout")
    head = required_command(("git", "rev-parse", "HEAD"), cwd=root).strip()
    tagged = required_command(
        ("git", "rev-list", "-n", "1", options.binding.tag), cwd=root
    ).strip()
    tag_type = required_command(
        ("git", "cat-file", "-t", f"refs/tags/{options.binding.tag}"), cwd=root
    ).strip()
    if head != options.binding.head_sha or tagged != options.binding.head_sha:
        raise FinalizationError("checkout HEAD, release tag, and workflow SHA are not identical")
    if tag_type != "tag":
        raise FinalizationError("release finalization requires an annotated tag")
    required_command(
        (
            sys.executable,
            str(root / "scripts" / "check-release-version.py"),
            options.binding.tag,
            "--plugin-root",
            str(root),
        ),
        cwd=root,
        allow_stdout=False,
    )
    epoch_text = required_command(
        ("git", "show", "-s", "--format=%ct", options.binding.head_sha), cwd=root
    ).strip()
    try:
        epoch = int(epoch_text)
    except ValueError as error:
        raise FinalizationError("git returned an invalid source timestamp") from error
    if epoch <= 0:
        raise FinalizationError("git returned a non-positive source timestamp")
    return epoch


def validate_remote_tag(
    options: FinalizationOptions,
) -> release_git_tag.RemoteTagBinding:
    """Bind the GitHub annotated tag object to the same local tag and commit."""

    local_tag_object = required_command(
        ("git", "rev-parse", f"refs/tags/{options.binding.tag}"),
        cwd=options.plugin_root,
    ).strip()
    ref_payload = required_command(
        (
            "gh",
            "api",
            "--hostname",
            "github.com",
            f"repos/{options.repository}/git/ref/tags/{options.binding.tag}",
        )
    )
    try:
        tag_object_sha = release_git_tag.remote_tag_object_sha(
            ref_payload,
            expected_tag=options.binding.tag,
            expected_local_tag_object_sha=local_tag_object,
        )
    except release_git_tag.ReleaseTagError as error:
        raise FinalizationError(str(error)) from error
    tag_payload = required_command(
        (
            "gh",
            "api",
            "--hostname",
            "github.com",
            f"repos/{options.repository}/git/tags/{tag_object_sha}",
        )
    )
    try:
        return release_git_tag.parse_remote_annotated_tag(
            ref_payload,
            tag_payload,
            expected_tag=options.binding.tag,
            expected_commit_sha=options.binding.head_sha,
            expected_local_tag_object_sha=local_tag_object,
        )
    except release_git_tag.ReleaseTagError as error:
        raise FinalizationError(str(error)) from error


def _download_manifest_payload(
    run: release_draft.WorkflowRun,
    inventory: Sequence[release_artifacts.ArtifactRecord],
    archives: dict[str, Path],
) -> dict[str, Any]:
    return {
        "workflow_run": asdict(run),
        "artifacts": [asdict(record) for record in inventory],
        "archives": {
            name: sha256_file(path, label=f"downloaded release archive {name}")
            for name, path in sorted(archives.items())
        },
    }


def download_candidates(
    options: FinalizationOptions,
    contract: ReleaseContract,
    attempt_root: Path,
    run: release_draft.WorkflowRun,
) -> Path:
    downloads = private_subdirectory(attempt_root, "downloads")
    artifact_cache = private_subdirectory(attempt_root, "artifact-downloads")
    expected_artifacts = tuple(
        target.artifact_name_for_attempt(run.attempt) for target in contract.targets
    )
    before = read_artifact_inventory(options, run, expected_artifacts)
    manifest_path = attempt_root / "download-manifest.json"

    if not os.path.lexists(manifest_path):
        try:
            downloader = release_artifact_download.GitHubArtifactZipDownloader()
        except release_artifact_download.ArtifactDownloadError as error:
            raise FinalizationError(str(error)) from error
        targets_by_artifact = {
            target.artifact_name_for_attempt(run.attempt): target
            for target in contract.targets
        }
        for record in before:
            target = targets_by_artifact.get(record.name)
            if target is None:  # pragma: no cover - parser contract guard
                raise FinalizationError(
                    f"artifact inventory returned an unrequested name: {record.name}"
                )
            try:
                extracted = release_artifact_download.download_public_archive(
                    options.repository,
                    record,
                    target.release_filename(options.binding.tag),
                    artifact_cache,
                    downloads,
                    downloader,
                )
            except release_artifact_download.ArtifactDownloadError as error:
                raise FinalizationError(str(error)) from error
            if extracted != downloads / target.release_filename(options.binding.tag):
                raise FinalizationError(
                    f"artifact extraction returned the wrong path: {record.name}"
                )
        try:
            verify_release_assets(contract, downloads, options.binding.tag)
        except RuntimeError as error:
            raise FinalizationError(
                "download directory is incomplete or contaminated; preserve it "
                "and use a new attempt root"
            ) from error
        after_run = read_workflow_run(options)
        after = read_artifact_inventory(options, after_run, expected_artifacts)
        if after_run != run or after != before:
            raise FinalizationError(
                "workflow run or artifact inventory changed during download"
            )
        downloaded_archives = archive_paths(
            downloads,
            contract,
            options.binding.tag,
        )
        ensure_receipt(
            attempt_root,
            manifest_path.name,
            _download_manifest_payload(run, before, downloaded_archives),
        )
    else:
        try:
            verify_release_assets(contract, downloads, options.binding.tag)
        except RuntimeError as error:
            raise FinalizationError(
                "persisted download set no longer satisfies the contract"
            ) from error
        expected_payload = _download_manifest_payload(
            run,
            before,
            archive_paths(downloads, contract, options.binding.tag),
        )
        ensure_receipt(attempt_root, manifest_path.name, expected_payload)
        after_run = read_workflow_run(options)
        after = read_artifact_inventory(options, after_run, expected_artifacts)
        if after_run != run or after != before:
            raise FinalizationError(
                "workflow run or artifact inventory changed while resuming downloads"
            )
    return downloads
