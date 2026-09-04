"""Static package validation for anonymously downloaded release archives."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Sequence

import macos_signing
import release_files
import release_public
from release_package import STATIC_PATHS, expected_archive_members, safe_extract_archive
from release_targets import ReleaseContract, ReleaseTarget


MAX_COMMAND_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True)
class StaticFileReceipt:
    path: str
    mode: int
    sha256: str


@dataclass(frozen=True)
class MacSignatureSliceReceipt:
    architecture: str
    leaf_authority: str
    cdhash: str
    timestamp: str
    designated_requirement: str


@dataclass(frozen=True)
class MacBinaryReceipt:
    signed_sha256: str
    identity_sha1: str
    identifier: str
    team_id: str
    architectures: tuple[str, ...]
    slices: tuple[MacSignatureSliceReceipt, ...]
    online_notarization_ticket_valid: bool


@dataclass(frozen=True)
class PublicArchiveReceipt:
    target_id: str
    asset_id: int
    asset_name: str
    archive_format: str
    archive_size: int
    archive_sha256: str
    binary_name: str
    binary_mode: int
    binary_sha256: str
    member_count: int
    static_files_match: bool
    macos: MacBinaryReceipt | None


def normalize_plugin_root(path: Path) -> Path:
    """Return an absolute ordinary directory without following its leaf."""

    root = Path(os.path.abspath(path))
    try:
        metadata = os.lstat(root)
    except OSError as error:
        raise release_public.PublicVerificationError(
            f"cannot inspect plugin root: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise release_public.PublicVerificationError(
            f"plugin root must be an ordinary directory: {root}"
        )
    return root


def snapshot_static_files(plugin_root: Path) -> tuple[StaticFileReceipt, ...]:
    """Bind every packaged static file to stable tagged-source bytes."""

    receipts: list[StaticFileReceipt] = []
    for relative in STATIC_PATHS:
        source = plugin_root / relative
        try:
            snapshot = release_files.inspect_regular_file(
                source,
                label=f"tagged static package file {relative.as_posix()}",
                require_single_link=True,
            )
            digest = release_files.sha256_file(
                source,
                label=f"tagged static package file {relative.as_posix()}",
                require_single_link=True,
            )
            after = release_files.inspect_regular_file(
                source,
                label=f"tagged static package file {relative.as_posix()}",
                require_single_link=True,
            )
        except release_files.ReleaseFileError as error:
            raise release_public.PublicVerificationError(str(error)) from error
        if after != snapshot:
            raise release_public.PublicVerificationError(
                f"tagged static package file changed: {relative.as_posix()}"
            )
        receipts.append(
            StaticFileReceipt(path=relative.as_posix(), mode=0o644, sha256=digest)
        )
    return tuple(receipts)


def _require_static_command(
    runner: macos_signing.CommandRunner,
    argv: tuple[str, ...],
) -> None:
    result = runner.run(argv)
    if result.argv != argv:
        raise release_public.PublicVerificationError(
            "static binary inspector returned a mismatched command identity"
        )
    try:
        output_size = len(result.stdout.encode("utf-8", errors="strict")) + len(
            result.stderr.encode("utf-8", errors="strict")
        )
    except UnicodeError as error:
        raise release_public.PublicVerificationError(
            "static binary inspector output is not UTF-8"
        ) from error
    if output_size > MAX_COMMAND_OUTPUT_BYTES:
        raise release_public.PublicVerificationError(
            "static binary inspector output exceeded its size limit"
        )
    if result.returncode != 0:
        diagnostic = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise release_public.PublicVerificationError(
            f"static binary inspector failed with exit {result.returncode}: "
            f"{diagnostic[:1000]}"
        )
    if result.stdout.strip() or result.stderr.strip():
        raise release_public.PublicVerificationError(
            "static binary inspector produced unexpected output"
        )


def _check_static_files(
    package: Path,
    expected: Sequence[StaticFileReceipt],
) -> None:
    for receipt in expected:
        packaged = package / receipt.path
        try:
            metadata = os.lstat(packaged)
        except OSError as error:
            raise release_public.PublicVerificationError(
                f"cannot inspect packaged static file {receipt.path}: {error}"
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != receipt.mode
        ):
            raise release_public.PublicVerificationError(
                f"packaged static file has the wrong type or mode: {receipt.path}"
            )
        try:
            digest = release_files.sha256_file(
                packaged,
                label=f"packaged static file {receipt.path}",
                require_single_link=True,
            )
        except release_files.ReleaseFileError as error:
            raise release_public.PublicVerificationError(str(error)) from error
        if digest != receipt.sha256:
            raise release_public.PublicVerificationError(
                f"packaged static file differs from the tagged source: {receipt.path}"
            )


def _mac_receipt(
    *,
    plugin_root: Path,
    contract: ReleaseContract,
    identity_sha1: str,
    target: ReleaseTarget,
    binary: Path,
    binary_sha256: str,
    runner: macos_signing.CommandRunner,
) -> MacBinaryReceipt:
    _require_static_command(
        runner,
        (
            sys.executable,
            str(plugin_root / "scripts" / "check-release-binary.py"),
            "--plugin-root",
            str(plugin_root),
            "--target",
            target.id,
            "--binary",
            str(binary),
        ),
    )
    verification = macos_signing.verify_signed(
        binary,
        expected_architectures=target.macos_architectures(),
        identity_sha1=identity_sha1,
        identifier=contract.macos_signing.code_identifier,
        team_id=contract.macos_signing.team_id,
        runner=runner,
    )
    if verification.signed_sha256 != binary_sha256:
        raise release_public.PublicVerificationError(
            f"macOS signature verified different bytes: {target.id}"
        )
    macos_signing.check_notarization_ticket(binary, runner)
    try:
        final_sha256 = release_files.sha256_file(
            binary,
            label=f"accepted public macOS binary {target.id}",
            require_single_link=True,
        )
    except release_files.ReleaseFileError as error:
        raise release_public.PublicVerificationError(str(error)) from error
    if final_sha256 != binary_sha256:
        raise release_public.PublicVerificationError(
            f"public macOS binary changed during static acceptance: {target.id}"
        )
    return MacBinaryReceipt(
        signed_sha256=verification.signed_sha256,
        identity_sha1=verification.identity_sha1,
        identifier=verification.identifier,
        team_id=verification.team_id,
        architectures=verification.architectures,
        slices=tuple(
            MacSignatureSliceReceipt(
                architecture=item.architecture,
                leaf_authority=item.leaf_authority,
                cdhash=item.cdhash,
                timestamp=item.timestamp,
                designated_requirement=item.designated_requirement,
            )
            for item in verification.slices
        ),
        online_notarization_ticket_valid=True,
    )


def validate_archive(
    *,
    plugin_root: Path,
    contract: ReleaseContract,
    identity_sha1: str,
    target: ReleaseTarget,
    plan: release_public.PublicAssetPlan,
    static_files: Sequence[StaticFileReceipt],
    runner: macos_signing.CommandRunner,
) -> PublicArchiveReceipt:
    """Safely extract one archive and statically validate its package contract."""

    with tempfile.TemporaryDirectory(prefix=f"public-{target.id}-") as temporary:
        extraction_root = Path(temporary)
        package = safe_extract_archive(
            plan.destination,
            target.archive_format,
            extraction_root,
            expected_archive_members(target.binary_name),
        )
        _check_static_files(package, static_files)
        binary = package / "bin" / target.binary_name
        try:
            metadata = os.lstat(binary)
        except OSError as error:
            raise release_public.PublicVerificationError(
                f"cannot inspect public binary for {target.id}: {error}"
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o755
        ):
            raise release_public.PublicVerificationError(
                f"public binary has the wrong type or mode: {target.id}"
            )
        try:
            binary_sha256 = release_files.sha256_file(
                binary,
                label=f"public binary {target.id}",
                require_single_link=True,
            )
        except release_files.ReleaseFileError as error:
            raise release_public.PublicVerificationError(str(error)) from error
        macos = None
        if target.family in {"macos", "macos_universal2"}:
            macos = _mac_receipt(
                plugin_root=plugin_root,
                contract=contract,
                identity_sha1=identity_sha1,
                target=target,
                binary=binary,
                binary_sha256=binary_sha256,
                runner=runner,
            )
        if not release_public.matches_public_asset(plan.destination, plan):
            raise release_public.PublicVerificationError(
                f"public archive changed during static acceptance: {plan.name}"
            )
        return PublicArchiveReceipt(
            target_id=target.id,
            asset_id=plan.asset_id,
            asset_name=plan.name,
            archive_format=target.archive_format,
            archive_size=plan.size,
            archive_sha256=plan.sha256,
            binary_name=target.binary_name,
            binary_mode=0o755,
            binary_sha256=binary_sha256,
            member_count=len(expected_archive_members(target.binary_name)),
            static_files_match=True,
            macos=macos,
        )
