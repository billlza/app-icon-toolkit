#!/usr/bin/env python3
"""Fail-closed building blocks for publishing a verified GitHub draft release.

This module deliberately has no command-line coordinator.  Callers must fetch
fresh JSON snapshots at each boundary and inject the command runner used for
the small, explicit set of GitHub CLI mutations constructed here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Protocol
from urllib.parse import quote

from release_files import (
    FilePublicationCollision,
    FilePublicationIndeterminate,
    ReleaseFileError,
    absolute_path,
    inspect_regular_file,
    publish_sibling_no_replace,
    sha256_file,
)
from release_targets import (
    validate_commit_sha,
    validate_release_tag,
    validate_repository as validate_repository_identifier,
)


EXPECTED_WORKFLOW = "Release"
EXPECTED_WORKFLOW_PATH = ".github/workflows/release.yml"
EXPECTED_EVENT = "push"
RELEASE_NAME_PREFIX = "App Icon Toolkit "
RELEASE_VIEW_FIELDS = "id,databaseId,tagName,name,body,isDraft,isPrerelease,assets"
CHECKSUM_ASSET_NAME = "SHA256SUMS"
MAX_JSON_BYTES = 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_FIELD_CHARS = 512
MAX_RELEASE_BODY_BYTES = 256 * 1024
MAX_ASSETS = 64

_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_REMOTE_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


class ReleaseDraftError(RuntimeError):
    """The draft release cannot safely advance."""


class MutationOutcomeUnknown(ReleaseDraftError):
    """A mutation crossed the process boundary without a trustworthy result."""


class PublicationOutcomeUnknown(ReleaseDraftError):
    """Publication could not be reconciled to a trustworthy terminal state."""


class CommandRunner(Protocol):
    """Injected command executor returning a text-mode CompletedProcess."""

    def __call__(
        self, command: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class WorkflowRun:
    workflow_id: int
    run_id: int
    attempt: int
    tag: str
    head_sha: str


@dataclass(frozen=True)
class WorkflowIdentity:
    """Canonical repository workflow resolved by its checked-in path."""

    workflow_id: int
    name: str
    path: str
    state: str


@dataclass(frozen=True)
class RemoteAsset:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ReleaseSnapshot:
    release_id: str
    database_id: int
    tag: str
    name: str
    body: str
    is_draft: bool
    is_prerelease: bool
    assets: tuple[RemoteAsset, ...]


@dataclass(frozen=True)
class LocalAsset:
    name: str
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class DraftUploadPlan:
    repository: str
    run: WorkflowRun
    release_id: str
    release_database_id: int
    expected_body: str
    assets: tuple[LocalAsset, ...]
    missing_names: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedDraft:
    """Capability produced only after a complete remote asset comparison."""

    repository: str
    run: WorkflowRun
    release_id: str
    release_database_id: int
    expected_body: str
    assets: tuple[LocalAsset, ...]


@dataclass(frozen=True)
class PublicationReceipt:
    repository: str
    release_id: str
    release_database_id: int
    tag: str
    head_sha: str
    workflow_id: int
    run_id: int
    run_attempt: int
    reconciled_after_unknown_mutation: bool


def _validate_tag(tag: str) -> str:
    try:
        return validate_release_tag(tag)
    except RuntimeError as error:
        raise ReleaseDraftError(str(error)) from error


def _validate_sha(head_sha: str) -> str:
    try:
        return validate_commit_sha(head_sha)
    except RuntimeError as error:
        raise ReleaseDraftError(str(error)) from error


def validate_repository(repository: str) -> str:
    """Return one bounded owner/repository slug or fail closed."""

    try:
        return validate_repository_identifier(repository)
    except RuntimeError as error:
        raise ReleaseDraftError(str(error)) from error


def _validate_asset_name(name: str) -> str:
    if not isinstance(name, str) or _ASSET_NAME.fullmatch(name) is None:
        raise ReleaseDraftError(f"unsafe release asset name: {name!r}")
    return name


def _load_json_object(payload: str, context: str) -> dict[str, object]:
    if not isinstance(payload, str):
        raise ReleaseDraftError(f"{context} JSON must be text")
    try:
        payload_size = len(payload.encode("utf-8", errors="strict"))
    except UnicodeError as error:
        raise ReleaseDraftError(f"{context} JSON is not valid UTF-8 text") from error
    if payload_size == 0 or payload_size > MAX_JSON_BYTES:
        raise ReleaseDraftError(
            f"{context} JSON size {payload_size} is outside the allowed range"
        )

    def reject_duplicates(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ReleaseDraftError(f"{context} repeats JSON key {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except ReleaseDraftError:
        raise
    except json.JSONDecodeError as error:
        raise ReleaseDraftError(f"{context} JSON is invalid: {error.msg}") from error
    if not isinstance(value, dict):
        raise ReleaseDraftError(f"{context} JSON must contain one object")
    return value


def _required_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseDraftError(f"{context} must be a non-empty string")
    if len(value) > MAX_FIELD_CHARS:
        raise ReleaseDraftError(f"{context} exceeds {MAX_FIELD_CHARS} characters")
    return value


def _required_positive_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReleaseDraftError(f"{context} must be a positive integer")
    return value


def _release_body(value: object) -> str:
    if not isinstance(value, str):
        raise ReleaseDraftError("release body must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ReleaseDraftError("release body is not valid UTF-8 text") from error
    if not encoded or len(encoded) > MAX_RELEASE_BODY_BYTES:
        raise ReleaseDraftError(
            f"release body size {len(encoded)} is outside the allowed range"
        )
    return value


def parse_workflow_identity(
    payload: str,
    *,
    expected_workflow_id: int,
    expected_name: str,
    expected_path: str,
) -> WorkflowIdentity:
    """Bind a numeric workflow ID to one canonical active workflow path."""

    expected_workflow_id = _required_positive_int(
        expected_workflow_id,
        "expected workflow id",
    )
    expected_name = _required_string(expected_name, "expected workflow name")
    expected_path = _required_string(expected_path, "expected workflow path")
    value = _load_json_object(payload, "GitHub workflow")
    workflow = WorkflowIdentity(
        workflow_id=_required_positive_int(value.get("id"), "workflow id"),
        name=_required_string(value.get("name"), "workflow name"),
        path=_required_string(value.get("path"), "workflow path"),
        state=_required_string(value.get("state"), "workflow state"),
    )
    if workflow.workflow_id != expected_workflow_id:
        raise ReleaseDraftError("workflow ID differs from the canonical path")
    if workflow.name != expected_name:
        raise ReleaseDraftError("workflow name differs from the canonical contract")
    if workflow.path != expected_path:
        raise ReleaseDraftError("workflow path differs from the canonical contract")
    if workflow.state != "active":
        raise ReleaseDraftError("workflow is not active")
    return workflow


def parse_workflow_run(
    payload: str,
    *,
    expected_workflow_id: int,
    expected_run_id: int,
    expected_attempt: int,
    expected_tag: str,
    expected_head_sha: str,
) -> WorkflowRun:
    """Validate one exact successful tag-push run of the Release workflow."""

    expected_tag = _validate_tag(expected_tag)
    expected_head_sha = _validate_sha(expected_head_sha)
    expected_workflow_id = _required_positive_int(
        expected_workflow_id, "expected workflow id"
    )
    expected_run_id = _required_positive_int(expected_run_id, "expected workflow run id")
    expected_attempt = _required_positive_int(
        expected_attempt, "expected workflow run attempt"
    )
    value = _load_json_object(payload, "GitHub workflow run")
    workflow_id = _required_positive_int(
        value.get("workflowDatabaseId"), "workflow workflowDatabaseId"
    )
    run_id = _required_positive_int(value.get("databaseId"), "workflow databaseId")
    attempt = _required_positive_int(value.get("attempt"), "workflow attempt")
    tag = _required_string(value.get("headBranch"), "workflow headBranch")
    head_sha = _required_string(value.get("headSha"), "workflow headSha")
    workflow = _required_string(value.get("workflowName"), "workflow workflowName")
    event = _required_string(value.get("event"), "workflow event")
    status = _required_string(value.get("status"), "workflow status")
    conclusion = _required_string(value.get("conclusion"), "workflow conclusion")

    mismatches = []
    if workflow_id != expected_workflow_id:
        mismatches.append(
            f"workflowDatabaseId={workflow_id}, expected {expected_workflow_id}"
        )
    if run_id != expected_run_id:
        mismatches.append(f"databaseId={run_id}, expected {expected_run_id}")
    if attempt != expected_attempt:
        mismatches.append(f"attempt={attempt}, expected {expected_attempt}")
    if tag != expected_tag:
        mismatches.append(f"tag={tag!r}, expected {expected_tag!r}")
    if head_sha != expected_head_sha:
        mismatches.append(f"headSha={head_sha!r}, expected {expected_head_sha!r}")
    if workflow != EXPECTED_WORKFLOW:
        mismatches.append(f"workflowName={workflow!r}, expected {EXPECTED_WORKFLOW!r}")
    if event != EXPECTED_EVENT:
        mismatches.append(f"event={event!r}, expected {EXPECTED_EVENT!r}")
    if status != "completed":
        mismatches.append(f"status={status!r}, expected 'completed'")
    if conclusion != "success":
        mismatches.append(f"conclusion={conclusion!r}, expected 'success'")
    if mismatches:
        raise ReleaseDraftError("workflow run binding failed: " + "; ".join(mismatches))
    return WorkflowRun(
        workflow_id=workflow_id,
        run_id=run_id,
        attempt=attempt,
        tag=tag,
        head_sha=head_sha,
    )


def parse_release(payload: str, *, expected_tag: str) -> ReleaseSnapshot:
    """Parse a bounded release snapshot and its release-asset digests."""

    expected_tag = _validate_tag(expected_tag)
    value = _load_json_object(payload, "GitHub release")
    release_id = _required_string(value.get("id"), "release id")
    database_id = _required_positive_int(
        value.get("databaseId"), "release databaseId"
    )
    tag = _required_string(value.get("tagName"), "release tagName")
    release_name = _required_string(value.get("name"), "release name")
    body = _release_body(value.get("body"))
    is_draft = value.get("isDraft")
    if not isinstance(is_draft, bool):
        raise ReleaseDraftError("release isDraft must be a boolean")
    is_prerelease = value.get("isPrerelease")
    if not isinstance(is_prerelease, bool):
        raise ReleaseDraftError("release isPrerelease must be a boolean")
    if tag != expected_tag:
        raise ReleaseDraftError(
            f"release tag {tag!r} does not match expected tag {expected_tag!r}"
        )

    raw_assets = value.get("assets")
    if not isinstance(raw_assets, list):
        raise ReleaseDraftError("release assets must be an array")
    if len(raw_assets) > MAX_ASSETS:
        raise ReleaseDraftError(f"release has more than {MAX_ASSETS} assets")

    assets = []
    seen = set()
    for index, raw_asset in enumerate(raw_assets):
        context = f"release assets[{index}]"
        if not isinstance(raw_asset, dict):
            raise ReleaseDraftError(f"{context} must be an object")
        asset_name = _validate_asset_name(
            _required_string(raw_asset.get("name"), f"{context}.name")
        )
        if asset_name in seen:
            raise ReleaseDraftError(
                f"release contains duplicate asset name: {asset_name}"
            )
        seen.add(asset_name)
        size = _required_positive_int(raw_asset.get("size"), f"{context}.size")
        digest = _required_string(raw_asset.get("digest"), f"{context}.digest")
        digest_match = _REMOTE_DIGEST.fullmatch(digest)
        if digest_match is None:
            raise ReleaseDraftError(f"{context}.digest is not an exact SHA-256 digest")
        state = raw_asset.get("state")
        if state is not None and state != "uploaded":
            raise ReleaseDraftError(f"{context}.state is not 'uploaded': {state!r}")
        assets.append(
            RemoteAsset(name=asset_name, size=size, sha256=digest_match.group(1))
        )
    return ReleaseSnapshot(
        release_id=release_id,
        database_id=database_id,
        tag=tag,
        name=release_name,
        body=body,
        is_draft=is_draft,
        is_prerelease=is_prerelease,
        assets=tuple(sorted(assets, key=lambda asset: asset.name)),
    )


def _snapshot_path(name: str, raw_path: Path) -> LocalAsset:
    name = _validate_asset_name(name)
    path = absolute_path(raw_path)
    if path.name != name:
        raise ReleaseDraftError(
            f"asset path basename {path.name!r} does not match explicit name {name!r}"
        )
    try:
        before = inspect_regular_file(
            path,
            label=f"local release asset {name}",
            require_single_link=True,
        )
        digest = sha256_file(
            path,
            label=f"local release asset {name}",
            require_single_link=True,
        )
        after = inspect_regular_file(
            path,
            label=f"local release asset {name}",
            require_single_link=True,
        )
    except ReleaseFileError as error:
        raise ReleaseDraftError(str(error)) from error
    if before != after:
        raise ReleaseDraftError(f"local release asset changed while hashing: {name}")
    return LocalAsset(
        name=name,
        path=path,
        size=after.size,
        sha256=digest,
    )


def snapshot_local_assets(
    paths: Mapping[str, Path], *, expected_names: Sequence[str]
) -> tuple[LocalAsset, ...]:
    """Hash an explicit, exact local allowlist without accepting globs."""

    if not expected_names or len(expected_names) > MAX_ASSETS:
        raise ReleaseDraftError("local release asset allowlist is empty or too large")
    names = tuple(_validate_asset_name(name) for name in expected_names)
    if len(set(names)) != len(names):
        raise ReleaseDraftError("local release asset allowlist is empty, duplicated, or too large")
    if not paths or len(paths) > MAX_ASSETS:
        raise ReleaseDraftError("supplied local release asset set is empty or too large")
    supplied = {_validate_asset_name(name) for name in paths}
    expected = set(names)
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        raise ReleaseDraftError(
            f"local release asset mismatch: missing={missing}; extra={extra}"
        )
    assets = tuple(_snapshot_path(name, paths[name]) for name in sorted(names))
    if len({asset.path for asset in assets}) != len(assets):
        raise ReleaseDraftError("two local release asset names refer to the same path")
    return assets


def _require_unchanged_local_asset(asset: LocalAsset, context: str) -> None:
    current = _snapshot_path(asset.name, asset.path)
    if current.size != asset.size or current.sha256 != asset.sha256:
        raise ReleaseDraftError(f"local release asset changed {context}: {asset.name}")


def render_sha256sums(archive_assets: Sequence[LocalAsset]) -> bytes:
    """Validate archive snapshots and render the canonical checksum bytes."""

    assets = tuple(archive_assets)
    names = [asset.name for asset in assets]
    if not assets or len(assets) > MAX_ASSETS - 1 or len(set(names)) != len(names):
        raise ReleaseDraftError("checksum input asset set is empty, duplicated, or too large")
    if CHECKSUM_ASSET_NAME in names:
        raise ReleaseDraftError("SHA256SUMS cannot include itself")
    for asset in assets:
        _validate_asset_name(asset.name)
        if _REMOTE_DIGEST.fullmatch(f"sha256:{asset.sha256}") is None:
            raise ReleaseDraftError(f"local asset has an invalid SHA-256 digest: {asset.name}")
        if asset.size <= 0:
            raise ReleaseDraftError(f"local asset has an invalid size: {asset.name}")
        _require_unchanged_local_asset(asset, "before checksum generation")

    return "".join(
        f"{asset.sha256}  {asset.name}\n"
        for asset in sorted(assets, key=lambda candidate: candidate.name)
    ).encode("ascii")


def generate_sha256sums(
    archive_assets: Sequence[LocalAsset], destination: Path
) -> LocalAsset:
    """Create, without replacement, a sorted checksum file for archive assets."""

    contents = render_sha256sums(archive_assets)
    path = absolute_path(destination)
    if path.name != CHECKSUM_ASSET_NAME:
        raise ReleaseDraftError(
            f"checksum destination must be named {CHECKSUM_ASSET_NAME}"
        )
    descriptor = -1
    temporary: Path | None = None
    preserve_temporary = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o644)
        offset = 0
        while offset < len(contents):
            written = os.write(descriptor, contents[offset:])
            if written <= 0:
                raise ReleaseDraftError("checksum file write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if not hasattr(os, "fchmod"):
            temporary.chmod(0o644)
        try:
            publish_sibling_no_replace(
                temporary,
                path,
                label="SHA256SUMS",
            )
        except FilePublicationCollision as error:
            raise ReleaseDraftError(str(error)) from error
        except FilePublicationIndeterminate as error:
            preserve_temporary = True
            raise MutationOutcomeUnknown(
                f"SHA256SUMS publication is indeterminate; preserved {temporary}"
            ) from error
        preserve_temporary = True
        try:
            temporary.unlink()
        except OSError as error:
            raise ReleaseDraftError(
                f"published SHA256SUMS but could not remove its sibling {temporary}: {error}"
            ) from error
        preserve_temporary = False
    except (ReleaseDraftError, MutationOutcomeUnknown):
        raise
    except (OSError, ReleaseFileError) as error:
        raise ReleaseDraftError(f"failed to create SHA256SUMS: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None and not preserve_temporary:
            temporary.unlink(missing_ok=True)
    return _snapshot_path(CHECKSUM_ASSET_NAME, path)


def _asset_map(assets: Sequence[LocalAsset]) -> dict[str, LocalAsset]:
    if not assets or len(assets) > MAX_ASSETS:
        raise ReleaseDraftError("local release asset set is empty or too large")
    mapped = {}
    for asset in assets:
        name = _validate_asset_name(asset.name)
        if asset.path.name != name:
            raise ReleaseDraftError(
                f"local asset path basename differs from its name: {name}"
            )
        if asset.size <= 0 or _REMOTE_DIGEST.fullmatch(f"sha256:{asset.sha256}") is None:
            raise ReleaseDraftError(f"local asset snapshot is invalid: {name}")
        mapped[name] = asset
    if not mapped or len(mapped) != len(assets) or len(mapped) > MAX_ASSETS:
        raise ReleaseDraftError("local release asset set is empty, duplicated, or too large")
    return mapped


def _missing_remote_assets(
    release: ReleaseSnapshot,
    local_assets: Mapping[str, LocalAsset],
    *,
    require_complete: bool,
) -> tuple[str, ...]:
    remote = {asset.name: asset for asset in release.assets}
    extra = sorted(set(remote) - set(local_assets))
    if extra:
        raise ReleaseDraftError(f"draft release contains unexpected assets: {extra}")
    for name, actual in remote.items():
        expected = local_assets[name]
        if actual.size != expected.size:
            raise ReleaseDraftError(
                f"remote asset size mismatch for {name}: "
                f"found {actual.size}, expected {expected.size}"
            )
        if actual.sha256 != expected.sha256:
            raise ReleaseDraftError(f"remote asset SHA-256 digest mismatch for {name}")
    missing = tuple(sorted(set(local_assets) - set(remote)))
    if require_complete and missing:
        raise ReleaseDraftError(f"draft release is missing assets: {list(missing)}")
    return missing


def plan_draft_uploads(
    repository: str,
    run: WorkflowRun,
    release: ReleaseSnapshot,
    local_assets: Sequence[LocalAsset],
    *,
    expected_body: str,
) -> DraftUploadPlan:
    """Accept an empty or safely resumable draft and identify missing assets."""

    repository = validate_repository(repository)
    if release.tag != run.tag:
        raise ReleaseDraftError("workflow run and release tags differ")
    expected_name = f"{RELEASE_NAME_PREFIX}{run.tag}"
    if release.name != expected_name:
        raise ReleaseDraftError(
            f"draft release name {release.name!r} does not match {expected_name!r}"
        )
    expected_body = _release_body(expected_body)
    if release.body != expected_body:
        raise ReleaseDraftError("draft release body differs from the tagged release notes")
    if release.is_prerelease:
        raise ReleaseDraftError("stable release draft must not be a prerelease")
    if not release.is_draft:
        raise ReleaseDraftError("refusing to upload assets to a non-draft release")
    assets = tuple(sorted(local_assets, key=lambda asset: asset.name))
    mapped = _asset_map(assets)
    for asset in assets:
        _require_unchanged_local_asset(asset, "before draft admission")
    missing = _missing_remote_assets(release, mapped, require_complete=False)
    return DraftUploadPlan(
        repository=repository,
        run=run,
        release_id=release.release_id,
        release_database_id=release.database_id,
        expected_body=expected_body,
        assets=assets,
        missing_names=missing,
    )


def upload_commands(plan: DraftUploadPlan) -> tuple[tuple[str, ...], ...]:
    """Construct one explicit numeric-release upload per missing file."""

    assets = _asset_map(plan.assets)
    order = sorted(
        plan.missing_names,
        key=lambda name: (name == CHECKSUM_ASSET_NAME, name),
    )
    return tuple(
        (
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            "POST",
            (
                f"https://uploads.github.com/repos/{plan.repository}/releases/"
                f"{plan.release_database_id}/assets"
                f"?name={quote(name, safe='')}"
            ),
            "--header",
            "Content-Type: application/octet-stream",
            "--input",
            str(assets[name].path),
            "--silent",
        )
        for name in order
    )


def _bounded_command_result(
    result: subprocess.CompletedProcess[str], context: str
) -> subprocess.CompletedProcess[str]:
    stdout = result.stdout if result.stdout is not None else ""
    stderr = result.stderr if result.stderr is not None else ""
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise MutationOutcomeUnknown(f"{context} returned non-text command output")
    if (
        len(stdout.encode("utf-8", errors="replace")) > MAX_COMMAND_OUTPUT_BYTES
        or len(stderr.encode("utf-8", errors="replace")) > MAX_COMMAND_OUTPUT_BYTES
    ):
        raise MutationOutcomeUnknown(f"{context} exceeded the command output limit")
    if isinstance(result.returncode, bool) or not isinstance(result.returncode, int):
        raise MutationOutcomeUnknown(f"{context} returned an invalid exit status")
    if result.returncode != 0:
        detail = stderr.strip() or stdout.strip() or "no diagnostic"
        detail = detail[:512]
        raise MutationOutcomeUnknown(
            f"{context} exited {result.returncode}; mutation requires reconciliation: {detail}"
        )
    return result


def _run_mutation(
    runner: CommandRunner, command: tuple[str, ...], context: str
) -> None:
    try:
        result = runner(command)
    except Exception as error:
        # Once the injected runner is called, its failure cannot prove whether
        # the remote mutation crossed the network boundary.  Preserve that
        # uncertainty for every ordinary exception while allowing process-level
        # BaseException signals to retain their normal semantics.
        raise MutationOutcomeUnknown(
            f"{context} did not return a trustworthy result; reconcile before retry"
        ) from error
    _bounded_command_result(result, context)


def run_uploads(
    plan: DraftUploadPlan,
    runner: CommandRunner,
    *,
    before_upload: Callable[[LocalAsset], None] | None = None,
) -> None:
    """Run missing uploads in order, stopping at the first uncertain mutation."""

    assets = _asset_map(plan.assets)
    for command, name in zip(
        upload_commands(plan),
        sorted(
            plan.missing_names,
            key=lambda candidate: (candidate == CHECKSUM_ASSET_NAME, candidate),
        ),
    ):
        asset = assets[name]
        _require_unchanged_local_asset(asset, "before upload")
        if before_upload is not None:
            before_upload(asset)
        _run_mutation(runner, command, f"upload of {name}")


def verify_complete_draft(
    plan: DraftUploadPlan, release: ReleaseSnapshot
) -> VerifiedDraft:
    """Issue publication capability only for a fresh, complete matching draft."""

    if (
        release.release_id != plan.release_id
        or release.database_id != plan.release_database_id
        or release.tag != plan.run.tag
    ):
        raise ReleaseDraftError("refreshed draft identity differs from the upload plan")
    if not release.is_draft:
        raise ReleaseDraftError("release became public before final asset verification")
    if release.name != f"{RELEASE_NAME_PREFIX}{plan.run.tag}":
        raise ReleaseDraftError("draft release name changed before final verification")
    if release.body != plan.expected_body:
        raise ReleaseDraftError("draft release body changed before final verification")
    if release.is_prerelease:
        raise ReleaseDraftError("draft became a prerelease before final verification")
    assets = _asset_map(plan.assets)
    for asset in plan.assets:
        _require_unchanged_local_asset(asset, "before remote verification")
    _missing_remote_assets(release, assets, require_complete=True)
    return VerifiedDraft(
        repository=plan.repository,
        run=plan.run,
        release_id=plan.release_id,
        release_database_id=plan.release_database_id,
        expected_body=plan.expected_body,
        assets=plan.assets,
    )


def publish_command(verified: VerifiedDraft) -> tuple[str, ...]:
    """Construct the sole mutation that may make a verified draft public."""

    return (
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "PATCH",
        f"repos/{verified.repository}/releases/{verified.release_database_id}",
        "-F",
        "draft=false",
        "--silent",
    )


def _validate_release_for_publication(
    verified: VerifiedDraft,
    release: ReleaseSnapshot,
    *,
    expected_draft: bool,
) -> None:
    if (
        release.release_id != verified.release_id
        or release.database_id != verified.release_database_id
        or release.tag != verified.run.tag
    ):
        raise ReleaseDraftError("release identity changed at the publication boundary")
    if release.name != f"{RELEASE_NAME_PREFIX}{verified.run.tag}":
        raise ReleaseDraftError("release name changed at the publication boundary")
    if release.body != verified.expected_body:
        raise ReleaseDraftError("release body changed at the publication boundary")
    if release.is_prerelease:
        raise ReleaseDraftError("release became a prerelease at the publication boundary")
    if release.is_draft != expected_draft:
        state = "draft" if release.is_draft else "public"
        expected = "draft" if expected_draft else "public"
        raise ReleaseDraftError(f"release is {state}; expected {expected}")
    _missing_remote_assets(
        release,
        _asset_map(verified.assets),
        require_complete=True,
    )


def publish_verified_draft(
    verified: VerifiedDraft,
    runner: CommandRunner,
    read_release_json: Callable[[], str],
    *,
    before_mutation: Callable[[], None] | None = None,
) -> PublicationReceipt:
    """Recheck, authorize, publish once, then reconcile without automatic retry."""

    preflight = parse_release(read_release_json(), expected_tag=verified.run.tag)
    _validate_release_for_publication(verified, preflight, expected_draft=True)
    if before_mutation is not None:
        before_mutation()

    mutation_unknown = False
    try:
        _run_mutation(runner, publish_command(verified), "draft publication")
    except MutationOutcomeUnknown:
        mutation_unknown = True

    try:
        observed_payload = read_release_json()
    except Exception as error:
        raise PublicationOutcomeUnknown(
            "draft publication result could not be read for reconciliation"
        ) from error
    try:
        observed = parse_release(observed_payload, expected_tag=verified.run.tag)
        _validate_release_for_publication(verified, observed, expected_draft=False)
    except ReleaseDraftError as error:
        raise PublicationOutcomeUnknown(
            "draft publication remains unknown after one read-only reconciliation"
        ) from error

    return PublicationReceipt(
        repository=verified.repository,
        release_id=verified.release_id,
        release_database_id=verified.release_database_id,
        tag=verified.run.tag,
        head_sha=verified.run.head_sha,
        workflow_id=verified.run.workflow_id,
        run_id=verified.run.run_id,
        run_attempt=verified.run.attempt,
        reconciled_after_unknown_mutation=mutation_unknown,
    )
