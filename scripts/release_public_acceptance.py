"""End-to-end, read-only acceptance of one immutable public GitHub release.

This stage deliberately owns no GitHub mutation capability.  It projects two
anonymous numeric-release snapshots onto the security-relevant release and
asset fields, downloads every asset through its numeric API identity, and then
performs bounded static package validation.  Published executables are never
run by this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
from typing import Callable, TypeVar
import zipfile

import macos_signing
import release_draft
import release_files
import release_git_tag
import release_public
import release_public_download
import release_public_package_validation as package_validation
from release_targets import ReleaseContract


PUBLIC_BUT_UNVERIFIED = "PUBLIC_BUT_UNVERIFIED"
PUBLIC_VERIFIED = "PUBLIC_VERIFIED"
DEFAULT_GET_ATTEMPTS = 3
COMMAND_TIMEOUT_SECONDS = 300


class PublicButUnverifiedError(RuntimeError):
    """The public release exists, but this read-only stage did not accept it."""

    status = PUBLIC_BUT_UNVERIFIED
    github_mutation_performed = False

    def __init__(self, reason: str) -> None:
        super().__init__(f"{PUBLIC_BUT_UNVERIFIED}: {reason}")


@dataclass(frozen=True)
class PublicAssetReceipt:
    asset_id: int
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PublicAcceptanceReceipt:
    schema_version: int
    status: str
    repository: str
    release_id: int
    tag: str
    tag_object_sha: str
    head_sha: str
    identity_sha1: str
    name: str
    body_sha256: str
    immutable: bool
    tag_binding_verified: bool
    snapshot_sha256: str
    snapshots_match: bool
    github_mutation_performed: bool
    candidate_execution_performed: bool
    assets: tuple[PublicAssetReceipt, ...]
    static_files: tuple[package_validation.StaticFileReceipt, ...]
    archives: tuple[package_validation.PublicArchiveReceipt, ...]

    def to_json_value(self) -> dict[str, object]:
        """Return a path-free value accepted by ``json.dumps``."""

        encoded = json.dumps(
            asdict(self),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        value = json.loads(encoded)
        if not isinstance(value, dict):  # pragma: no cover - dataclass root is fixed
            raise AssertionError("public acceptance receipt root must be an object")
        return value


@dataclass(frozen=True)
class PublicAcceptanceRequest:
    repository: str
    release_id: int
    tag: str
    expected_tag_object_sha: str
    expected_head_sha: str
    name: str
    body: str
    local_assets: tuple[release_draft.LocalAsset, ...]
    contract: ReleaseContract
    plugin_root: Path
    identity_sha1: str


T = TypeVar("T")


def _retry_get(operation: Callable[[], T], *, attempts: int, context: str) -> T:
    last_error: release_public.PublicVerificationError | None = None
    for _ in range(attempts):
        try:
            return operation()
        except release_public.PublicVerificationError as error:
            last_error = error
    if last_error is None:
        raise AssertionError("GET retry loop did not execute")
    raise release_public.PublicVerificationError(
        f"{context} failed after {attempts} read-only attempts: {last_error}"
    ) from last_error


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    release_public.validate_private_directory(path, "public acceptance workspace")


def _expected_asset_names(contract: ReleaseContract, tag: str) -> tuple[str, ...]:
    names = tuple(target.release_filename(tag) for target in contract.targets)
    return tuple(sorted((*names, release_draft.CHECKSUM_ASSET_NAME)))


def _fresh_local_assets(
    request: PublicAcceptanceRequest,
) -> tuple[release_draft.LocalAsset, ...]:
    expected_names = _expected_asset_names(request.contract, request.tag)
    supplied = request.local_assets
    if len({asset.name for asset in supplied}) != len(supplied):
        raise release_public.PublicVerificationError(
            "local public asset contract contains duplicate names"
        )
    supplied_by_name = {asset.name: asset for asset in supplied}
    try:
        fresh = release_draft.snapshot_local_assets(
            {name: asset.path for name, asset in supplied_by_name.items()},
            expected_names=expected_names,
        )
    except release_draft.ReleaseDraftError as error:
        raise release_public.PublicVerificationError(str(error)) from error
    for asset in fresh:
        declared = supplied_by_name[asset.name]
        if (
            asset.size != declared.size
            or asset.sha256 != declared.sha256
            or asset.path != release_files.absolute_path(declared.path)
        ):
            raise release_public.PublicVerificationError(
                f"local public asset changed after it was prepared: {asset.name}"
            )
    return fresh


def _snapshot_sha256(release: release_public.AnonymousPublicRelease) -> str:
    value = {
        "repository": release.repository,
        "release_id": release.release_id,
        "tag": release.tag,
        "name": release.name,
        "body": release.body,
        "assets": [asdict(asset) for asset in release.assets],
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(encoded).hexdigest()


def _download_snapshot(
    request: PublicAcceptanceRequest,
    transfer_directory: Path,
    downloader: release_public_download.PublicAssetDownloader,
    *,
    attempts: int,
) -> release_public.AnonymousPublicRelease:
    payload = _retry_get(
        lambda: release_public_download.download_anonymous_release_json(
            request.repository,
            request.release_id,
            transfer_directory,
            downloader,
        ),
        attempts=attempts,
        context="anonymous numeric release GET",
    )
    return release_public.parse_anonymous_public_release(
        payload,
        repository=request.repository,
        expected_release_id=request.release_id,
        expected_tag=request.tag,
        expected_name=request.name,
        expected_body=request.body,
        expected_assets=request.local_assets,
    )


def _download_tag_binding(
    request: PublicAcceptanceRequest,
    transfer_directory: Path,
    downloader: release_public_download.PublicAssetDownloader,
    *,
    attempts: int,
) -> release_git_tag.RemoteTagBinding:
    ref_payload = _retry_get(
        lambda: release_public_download.download_anonymous_tag_ref_json(
            request.repository,
            request.tag,
            transfer_directory,
            downloader,
        ),
        attempts=attempts,
        context="anonymous annotated-tag reference GET",
    )
    try:
        ref_text = ref_payload.decode("utf-8", errors="strict")
        tag_object_sha = release_git_tag.remote_tag_object_sha(
            ref_text,
            expected_tag=request.tag,
            expected_local_tag_object_sha=request.expected_tag_object_sha,
        )
    except (UnicodeError, release_git_tag.ReleaseTagError) as error:
        raise release_public.PublicVerificationError(
            f"anonymous annotated-tag reference is invalid: {error}"
        ) from error
    tag_payload = _retry_get(
        lambda: release_public_download.download_anonymous_tag_object_json(
            request.repository,
            tag_object_sha,
            transfer_directory,
            downloader,
        ),
        attempts=attempts,
        context="anonymous annotated-tag object GET",
    )
    try:
        tag_text = tag_payload.decode("utf-8", errors="strict")
        return release_git_tag.parse_remote_annotated_tag(
            ref_text,
            tag_text,
            expected_tag=request.tag,
            expected_commit_sha=request.expected_head_sha,
            expected_local_tag_object_sha=request.expected_tag_object_sha,
        )
    except (UnicodeError, release_git_tag.ReleaseTagError) as error:
        raise release_public.PublicVerificationError(
            f"anonymous annotated-tag object is invalid: {error}"
        ) from error


def _verify_public_release(
    request: PublicAcceptanceRequest,
    *,
    downloader: release_public_download.PublicAssetDownloader,
    runner: macos_signing.CommandRunner,
    get_attempts: int,
) -> PublicAcceptanceReceipt:
    if os.name != "posix":
        raise release_public.PublicVerificationError(
            "public acceptance requires POSIX private-directory semantics"
        )
    if (
        isinstance(get_attempts, bool)
        or not isinstance(get_attempts, int)
        or get_attempts <= 0
    ):
        raise release_public.PublicVerificationError(
            "public GET attempt count must be a positive integer"
        )
    plugin_root = package_validation.normalize_plugin_root(request.plugin_root)
    if plugin_root != request.plugin_root:
        request = replace(request, plugin_root=plugin_root)
    local_assets = _fresh_local_assets(request)
    if local_assets != request.local_assets:
        request = replace(request, local_assets=local_assets)
    static_files = package_validation.snapshot_static_files(plugin_root)

    with tempfile.TemporaryDirectory(prefix="app-icon-public-acceptance-") as temporary:
        workspace = Path(temporary)
        workspace.chmod(0o700)
        downloads = workspace / "downloads"
        transfer = workspace / "transfer"
        _private_directory(downloads)
        _private_directory(transfer)

        before = _download_snapshot(
            request, transfer, downloader, attempts=get_attempts
        )
        tag_before = _download_tag_binding(
            request,
            transfer,
            downloader,
            attempts=get_attempts,
        )
        plans = release_public.plan_public_downloads(
            before, request.local_assets, downloads
        )
        for plan in plans:
            _retry_get(
                lambda plan=plan: release_public_download.download_public_asset(
                    plan, downloader
                ),
                attempts=get_attempts,
                context=f"anonymous numeric asset GET {plan.asset_id}",
            )
        release_public.validate_public_downloads(plans, downloads)

        by_name = {plan.name: plan for plan in plans}
        archives = tuple(
            package_validation.validate_archive(
                plugin_root=request.plugin_root,
                contract=request.contract,
                identity_sha1=request.identity_sha1,
                target=target,
                plan=by_name[target.release_filename(request.tag)],
                static_files=static_files,
                runner=runner,
            )
            for target in request.contract.targets
        )

        after = _download_snapshot(
            request, transfer, downloader, attempts=get_attempts
        )
        if after != before:
            raise release_public.PublicVerificationError(
                "anonymous public release identity changed between snapshots"
            )
        tag_after = _download_tag_binding(
            request,
            transfer,
            downloader,
            attempts=get_attempts,
        )
        if tag_after != tag_before:
            raise release_public.PublicVerificationError(
                "anonymous annotated-tag binding changed between snapshots"
            )
        if _fresh_local_assets(request) != request.local_assets:
            raise release_public.PublicVerificationError(
                "local prepared assets changed during public acceptance"
            )
        if package_validation.snapshot_static_files(plugin_root) != static_files:
            raise release_public.PublicVerificationError(
                "tagged static package files changed during public acceptance"
            )

        return PublicAcceptanceReceipt(
            schema_version=1,
            status=PUBLIC_VERIFIED,
            repository=before.repository,
            release_id=before.release_id,
            tag=before.tag,
            tag_object_sha=tag_after.tag_object_sha,
            head_sha=tag_after.commit_sha,
            identity_sha1=request.identity_sha1,
            name=before.name,
            body_sha256=hashlib.sha256(
                before.body.encode("utf-8", errors="strict")
            ).hexdigest(),
            immutable=True,
            tag_binding_verified=True,
            snapshot_sha256=_snapshot_sha256(before),
            snapshots_match=True,
            github_mutation_performed=False,
            candidate_execution_performed=False,
            assets=tuple(
                PublicAssetReceipt(
                    asset_id=asset.asset_id,
                    name=asset.name,
                    size=asset.size,
                    sha256=asset.sha256,
                )
                for asset in before.assets
            ),
            static_files=static_files,
            archives=archives,
        )


def verify_public_release(
    request: PublicAcceptanceRequest,
    *,
    downloader: release_public_download.PublicAssetDownloader | None = None,
    runner: macos_signing.CommandRunner | None = None,
    get_attempts: int = DEFAULT_GET_ATTEMPTS,
) -> PublicAcceptanceReceipt:
    """Verify public bytes without credentials, execution, or GitHub mutation."""

    try:
        selected_downloader = downloader
        if selected_downloader is None:
            selected_downloader = release_public_download.CurlPublicAssetDownloader()
        selected_runner = runner
        if selected_runner is None:
            selected_runner = macos_signing.SubprocessRunner(
                timeout_seconds=COMMAND_TIMEOUT_SECONDS
            )
        return _verify_public_release(
            request,
            downloader=selected_downloader,
            runner=selected_runner,
            get_attempts=get_attempts,
        )
    except PublicButUnverifiedError:
        raise
    except (
        EOFError,
        OSError,
        RuntimeError,
        tarfile.TarError,
        UnicodeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise PublicButUnverifiedError(str(error)) from error
