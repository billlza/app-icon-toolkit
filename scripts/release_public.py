"""Anonymous public-release download and byte-verification primitives."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from release_draft import CHECKSUM_ASSET_NAME, LocalAsset, render_sha256sums
from release_files import (
    FilePublicationIndeterminate,
    ReleaseFileError,
    inspect_regular_file,
    open_stable_regular_file,
    publish_sibling_no_replace,
    sha256_file,
)
from release_targets import validate_release_tag, validate_repository


MAX_PUBLIC_ASSETS = 64
MAX_PUBLIC_ASSET_BYTES = 512 * 1024 * 1024
MAX_TOTAL_PUBLIC_BYTES = 2 * 1024 * 1024 * 1024
MAX_PUBLIC_RELEASE_JSON_BYTES = 1024 * 1024
_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PublicVerificationError(RuntimeError):
    """The published release cannot yet be verified from anonymous bytes."""


@dataclass(frozen=True)
class PublicAssetPlan:
    asset_id: int
    name: str
    url: str
    destination: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class AnonymousPublicAsset:
    asset_id: int
    name: str
    size: int
    sha256: str
    api_url: str


@dataclass(frozen=True)
class AnonymousPublicRelease:
    repository: str
    release_id: int
    tag: str
    name: str
    body: str
    assets: tuple[AnonymousPublicAsset, ...]


def validate_private_directory(path: Path, context: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise PublicVerificationError(f"cannot inspect {context}: {error}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PublicVerificationError(
            f"{context} must be a current-user-owned 0700 directory: {path}"
        )


def _validate_asset(asset: LocalAsset) -> None:
    if _ASSET_NAME.fullmatch(asset.name) is None:
        raise PublicVerificationError(f"unsafe public asset name: {asset.name!r}")
    if asset.path.name != asset.name:
        raise PublicVerificationError(
            f"public asset name and local path differ: {asset.name!r}"
        )
    if isinstance(asset.size, bool) or not isinstance(asset.size, int) or asset.size <= 0:
        raise PublicVerificationError(f"invalid public asset size: {asset.name}")
    if not isinstance(asset.sha256, str) or _SHA256.fullmatch(asset.sha256) is None:
        raise PublicVerificationError(f"invalid public asset digest: {asset.name}")


def _strict_json_object(payload: bytes) -> dict[str, Any]:
    if not payload or len(payload) > MAX_PUBLIC_RELEASE_JSON_BYTES:
        raise PublicVerificationError(
            "anonymous public release JSON is empty or oversized"
        )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise PublicVerificationError(
            "anonymous public release JSON is not UTF-8"
        ) from error

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise PublicVerificationError(
                    f"anonymous public release JSON repeats key {key!r}"
                )
            value[key] = item
        return value

    def reject_constant(constant: str) -> None:
        raise PublicVerificationError(
            f"anonymous public release JSON contains {constant}"
        )

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except PublicVerificationError:
        raise
    except json.JSONDecodeError as error:
        raise PublicVerificationError(
            f"anonymous public release JSON is invalid: {error.msg}"
        ) from error
    if not isinstance(value, dict):
        raise PublicVerificationError(
            "anonymous public release JSON root must be an object"
        )
    return value


def _positive_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PublicVerificationError(f"{context} must be a positive integer")
    return value


def _text(value: object, context: str, maximum_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value:
        raise PublicVerificationError(f"{context} must be non-empty text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError as error:
        raise PublicVerificationError(f"{context} is not UTF-8") from error
    if size > maximum_bytes:
        raise PublicVerificationError(f"{context} exceeds its size limit")
    return value


def parse_anonymous_public_release(
    payload: bytes,
    *,
    repository: str,
    expected_release_id: int,
    expected_tag: str,
    expected_name: str,
    expected_body: str,
    expected_assets: Sequence[LocalAsset],
) -> AnonymousPublicRelease:
    """Validate GitHub's credential-free numeric release representation."""

    try:
        repository = validate_repository(repository)
        expected_tag = validate_release_tag(expected_tag)
    except RuntimeError as error:
        raise PublicVerificationError(str(error)) from error
    expected_release_id = _positive_int(expected_release_id, "expected release id")
    expected_name = _text(expected_name, "expected release name")
    if not isinstance(expected_body, str) or not expected_body:
        raise PublicVerificationError("expected release body must be non-empty text")
    expected_by_name: dict[str, LocalAsset] = {}
    total_size = 0
    for asset in expected_assets:
        _validate_asset(asset)
        if asset.name in expected_by_name:
            raise PublicVerificationError("expected public assets contain duplicate names")
        if asset.size > MAX_PUBLIC_ASSET_BYTES:
            raise PublicVerificationError(f"public asset is oversized: {asset.name}")
        total_size += asset.size
        if total_size > MAX_TOTAL_PUBLIC_BYTES:
            raise PublicVerificationError("public release assets exceed the total size limit")
        expected_by_name[asset.name] = asset
    if not expected_by_name or len(expected_by_name) > MAX_PUBLIC_ASSETS:
        raise PublicVerificationError("expected public asset allowlist is empty or too large")

    value = _strict_json_object(payload)
    release_id = _positive_int(value.get("id"), "anonymous release id")
    tag = _text(value.get("tag_name"), "anonymous release tag")
    name = _text(value.get("name"), "anonymous release name")
    body = value.get("body")
    if not isinstance(body, str):
        raise PublicVerificationError("anonymous release body must be text")
    if (
        release_id != expected_release_id
        or tag != expected_tag
        or name != expected_name
        or body != expected_body
        or value.get("draft") is not False
        or value.get("prerelease") is not False
        or value.get("immutable") is not True
    ):
        raise PublicVerificationError(
            "anonymous public release metadata differs from the published contract"
        )
    raw_assets = value.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) > MAX_PUBLIC_ASSETS:
        raise PublicVerificationError("anonymous public release assets are invalid or too many")

    assets: list[AnonymousPublicAsset] = []
    seen_names: set[str] = set()
    seen_ids: set[int] = set()
    for index, raw in enumerate(raw_assets):
        context = f"anonymous release assets[{index}]"
        if not isinstance(raw, dict):
            raise PublicVerificationError(f"{context} must be an object")
        asset_id = _positive_int(raw.get("id"), f"{context}.id")
        asset_name = _text(raw.get("name"), f"{context}.name")
        if _ASSET_NAME.fullmatch(asset_name) is None:
            raise PublicVerificationError(f"{context}.name is unsafe")
        size = _positive_int(raw.get("size"), f"{context}.size")
        digest = _text(raw.get("digest"), f"{context}.digest")
        digest_match = re.fullmatch(r"sha256:([0-9a-f]{64})", digest)
        api_url = _text(raw.get("url"), f"{context}.url", maximum_bytes=2048)
        expected_api_url = (
            f"https://api.github.com/repos/{repository}/releases/assets/{asset_id}"
        )
        browser_url = _text(
            raw.get("browser_download_url"),
            f"{context}.browser_download_url",
            maximum_bytes=4096,
        )
        expected_browser_url = (
            f"https://github.com/{repository}/releases/download/"
            f"{quote(expected_tag, safe='')}/{quote(asset_name, safe='')}"
        )
        expected = expected_by_name.get(asset_name)
        if (
            expected is None
            or digest_match is None
            or size != expected.size
            or digest_match.group(1) != expected.sha256
            or raw.get("state") != "uploaded"
            or api_url != expected_api_url
            or browser_url != expected_browser_url
        ):
            raise PublicVerificationError(
                f"{context} differs from the prepared asset contract"
            )
        if asset_name in seen_names or asset_id in seen_ids:
            raise PublicVerificationError("anonymous public release repeats an asset identity")
        seen_names.add(asset_name)
        seen_ids.add(asset_id)
        assets.append(
            AnonymousPublicAsset(
                asset_id=asset_id,
                name=asset_name,
                size=size,
                sha256=expected.sha256,
                api_url=api_url,
            )
        )
    if seen_names != set(expected_by_name):
        raise PublicVerificationError(
            "anonymous public release asset names differ from the prepared set"
        )
    return AnonymousPublicRelease(
        repository=repository,
        release_id=release_id,
        tag=tag,
        name=name,
        body=body,
        assets=tuple(sorted(assets, key=lambda asset: asset.name)),
    )


def plan_public_downloads(
    release: AnonymousPublicRelease,
    assets: Sequence[LocalAsset],
    destination_directory: Path,
) -> tuple[PublicAssetPlan, ...]:
    """Plan exact credential-free downloads by immutable numeric asset ID."""

    if not assets or len(assets) > MAX_PUBLIC_ASSETS:
        raise PublicVerificationError("public asset allowlist is empty or too large")
    names = [asset.name for asset in assets]
    if len(set(names)) != len(names):
        raise PublicVerificationError("public asset allowlist contains duplicate names")
    total_size = 0
    for asset in assets:
        _validate_asset(asset)
        if asset.size > MAX_PUBLIC_ASSET_BYTES:
            raise PublicVerificationError(f"public asset is oversized: {asset.name}")
        total_size += asset.size
        if total_size > MAX_TOTAL_PUBLIC_BYTES:
            raise PublicVerificationError("public release assets exceed the total size limit")
    public_by_name = {asset.name: asset for asset in release.assets}
    if set(public_by_name) != set(names):
        raise PublicVerificationError(
            "anonymous release asset identities differ from the local allowlist"
        )
    plans = []
    for asset in assets:
        public = public_by_name[asset.name]
        if public.size != asset.size or public.sha256 != asset.sha256:
            raise PublicVerificationError(
                f"anonymous release asset differs from local bytes: {asset.name}"
            )
        plans.append(
            PublicAssetPlan(
                asset_id=public.asset_id,
                name=asset.name,
                url=public.api_url,
                destination=destination_directory / asset.name,
                size=asset.size,
                sha256=asset.sha256,
            )
        )
    return tuple(sorted(plans, key=lambda plan: plan.name))


def matches_public_asset(
    path: Path,
    plan: PublicAssetPlan,
    *,
    require_single_link: bool = True,
) -> bool:
    try:
        snapshot = inspect_regular_file(
            path,
            label=f"anonymous public asset {plan.name}",
            require_single_link=require_single_link,
        )
        return snapshot.size == plan.size and sha256_file(
            path,
            label=f"anonymous public asset {plan.name}",
            require_single_link=require_single_link,
        ) == plan.sha256
    except ReleaseFileError:
        return False


def _read_checksum(path: Path, maximum_bytes: int) -> bytes:
    with open_stable_regular_file(
        path,
        label="anonymous public SHA256SUMS",
        require_single_link=True,
    ) as (opened, snapshot):
        if snapshot.size > maximum_bytes:
            raise PublicVerificationError("anonymous public SHA256SUMS is oversized")
        data = opened.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise PublicVerificationError("anonymous public SHA256SUMS is oversized")
    return data


def validate_public_downloads(
    plans: Sequence[PublicAssetPlan],
    destination_directory: Path,
) -> tuple[Path, ...]:
    """Verify the exact downloaded set, digests, and checksum manifest bytes."""

    if not plans or len(plans) > MAX_PUBLIC_ASSETS:
        raise PublicVerificationError("public download plan is empty or too large")
    validate_private_directory(
        destination_directory, "public download destination"
    )
    by_name: Mapping[str, PublicAssetPlan] = {plan.name: plan for plan in plans}
    if len(by_name) != len(plans):
        raise PublicVerificationError("public download plan contains duplicate names")
    try:
        entries = tuple(destination_directory.iterdir())
    except OSError as error:
        raise PublicVerificationError(
            f"cannot inspect anonymous public download directory: {error}"
        ) from error
    actual_names = {entry.name for entry in entries}
    if actual_names != set(by_name):
        raise PublicVerificationError(
            "anonymous public asset set mismatch; "
            f"missing={sorted(set(by_name) - actual_names)}; "
            f"extra={sorted(actual_names - set(by_name))}"
        )
    for plan in plans:
        if plan.destination.parent != destination_directory or not matches_public_asset(
            plan.destination, plan
        ):
            raise PublicVerificationError(
                f"anonymous public asset failed byte verification: {plan.name}"
            )

    checksum_plan = by_name.get(CHECKSUM_ASSET_NAME)
    if checksum_plan is None:
        raise PublicVerificationError("anonymous public release is missing SHA256SUMS")
    archive_assets = tuple(
        LocalAsset(
            name=plan.name,
            path=plan.destination,
            size=plan.size,
            sha256=plan.sha256,
        )
        for plan in plans
        if plan.name != CHECKSUM_ASSET_NAME
    )
    expected_checksum = render_sha256sums(archive_assets)
    observed_checksum = _read_checksum(
        checksum_plan.destination, len(expected_checksum)
    )
    if observed_checksum != expected_checksum:
        raise PublicVerificationError(
            "anonymous public SHA256SUMS differs from the prepared asset contract"
        )
    return tuple(plan.destination for plan in sorted(plans, key=lambda item: item.name))
