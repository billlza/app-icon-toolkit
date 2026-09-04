"""Extraction, Developer ID signing, packaging, and prepared-asset sealing."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
from typing import Any
import zipfile

import macos_signing
import release_draft
from release_attempt import private_subdirectory
from release_files import (
    ReleaseFileError,
    copy_regular_file,
    inspect_regular_file,
    open_stable_regular_file,
    sha256_file,
    verify_exact_regular_file_set,
)
from release_finalization_core import (
    AuditedMacRunner,
    FinalizationError,
    FinalizationOptions,
    MACOS_FAMILIES,
    archive_paths,
    check_release_binary,
    ensure_receipt,
    required_command,
)
from release_package import (
    STATIC_PATHS,
    expected_archive_members,
    recover_incomplete_extraction,
    safe_extract_archive,
)
from release_targets import ReleaseContract, ReleaseTarget


def _prepared_receipt_payload(
    options: FinalizationOptions,
    assets: tuple[release_draft.LocalAsset, ...],
) -> dict[str, Any]:
    return {
        "binding": asdict(options.binding),
        "assets": [asdict(asset) | {"path": asset.path.name} for asset in assets],
    }


def _persist_or_verify_receipt(
    root: Path,
    name: str,
    payload: dict[str, Any],
    *,
    sealed: bool,
) -> Path:
    if sealed:
        return ensure_receipt(root, name, payload, create_missing=False)
    return ensure_receipt(root, name, payload)


def _require_exact_prepared_files(
    directory: Path,
    expected_names: tuple[str, ...],
) -> None:
    try:
        verify_exact_regular_file_set(
            directory,
            expected_names,
            label="prepared release asset set",
        )
    except ReleaseFileError as error:
        raise FinalizationError("prepared release archive set is not exact") from error


def _read_stable_checksum(path: Path, expected: bytes) -> bytes:
    try:
        with open_stable_regular_file(
            path,
            label="persisted SHA256SUMS",
            require_single_link=True,
        ) as (checksum, _snapshot):
            return checksum.read(len(expected) + 1)
    except (OSError, ReleaseFileError) as error:
        raise FinalizationError(f"cannot read stable persisted SHA256SUMS: {error}") from error


def _validate_complete_prepared_assets(
    options: FinalizationOptions,
    contract: ReleaseContract,
    assets: Path,
) -> tuple[release_draft.LocalAsset, ...]:
    prepared_archives = archive_paths(assets, contract, options.binding.tag)
    checksum_path = assets / release_draft.CHECKSUM_ASSET_NAME
    all_paths = {**prepared_archives, checksum_path.name: checksum_path}
    expected_names = tuple(sorted(all_paths))
    _require_exact_prepared_files(assets, expected_names)

    archive_assets = release_draft.snapshot_local_assets(
        prepared_archives,
        expected_names=tuple(sorted(prepared_archives)),
    )
    expected_checksum = release_draft.render_sha256sums(archive_assets)
    if _read_stable_checksum(checksum_path, expected_checksum) != expected_checksum:
        raise FinalizationError("persisted SHA256SUMS differs from prepared archives")

    all_assets = release_draft.snapshot_local_assets(
        all_paths,
        expected_names=expected_names,
    )
    final_archive_assets = tuple(
        asset
        for asset in all_assets
        if asset.name != release_draft.CHECKSUM_ASSET_NAME
    )
    final_checksum = next(
        asset
        for asset in all_assets
        if asset.name == release_draft.CHECKSUM_ASSET_NAME
    )
    final_checksum_bytes = release_draft.render_sha256sums(final_archive_assets)
    if (
        final_checksum.size != len(final_checksum_bytes)
        or final_checksum.sha256 != hashlib.sha256(final_checksum_bytes).hexdigest()
    ):
        raise FinalizationError("persisted SHA256SUMS changed during prepared validation")
    _require_exact_prepared_files(assets, expected_names)
    return all_assets


def _validate_extracted_candidate(
    plugin_root: Path,
    package: Path,
    target: ReleaseTarget,
) -> Path:
    expected = {
        *(path.as_posix() for path in STATIC_PATHS),
        (Path("bin") / target.binary_name).as_posix(),
    }
    actual: set[str] = set()
    for path in package.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise FinalizationError(f"candidate contains unsupported filesystem entry: {path}")
        if path.is_file():
            actual.add(path.relative_to(package).as_posix())
    if actual != expected:
        raise FinalizationError(
            f"candidate package mismatch; missing={sorted(expected - actual)}; "
            f"extra={sorted(actual - expected)}"
        )
    for relative in STATIC_PATHS:
        packaged = package / relative
        local = plugin_root / relative
        if sha256_file(packaged, label=f"packaged {relative}") != sha256_file(
            local,
            label=f"tagged {relative}",
        ):
            raise FinalizationError(
                f"candidate static file differs from the tagged source: {relative}"
            )
        expected_mode = 0o644
        if stat.S_IMODE(packaged.stat().st_mode) != expected_mode:
            raise FinalizationError(f"candidate static file has wrong mode: {relative}")
    binary = package / "bin" / target.binary_name
    if stat.S_IMODE(binary.stat().st_mode) != 0o755:
        raise FinalizationError(f"candidate binary has wrong mode: {target.id}")
    return binary


def _extract_candidate_archive(
    archive: Path,
    archive_format: str,
    extraction: Path,
    binary_name: str,
    *,
    context: str,
) -> Path:
    """Normalize expected archive-read failures at the finalizer boundary."""

    try:
        return safe_extract_archive(
            archive,
            archive_format,
            extraction,
            expected_archive_members(binary_name),
        )
    except (
        OSError,
        RuntimeError,
        EOFError,
        tarfile.TarError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise FinalizationError(f"{context}: {error}") from error


def _candidate_directory(path: Path, *, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise FinalizationError(f"cannot inspect {label} {path}: {error}") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise FinalizationError(
            f"{label} must be a private owned directory with mode 0700: {path}"
        )


def _require_state_file(path: Path, *, label: str) -> None:
    try:
        inspect_regular_file(
            path,
            label=label,
            require_single_link=True,
        )
    except ReleaseFileError as error:
        raise FinalizationError(f"sealed prepared state has invalid {label}: {path}") from error


def _require_sealed_signing_state(
    target_root: Path,
    target: ReleaseTarget,
) -> None:
    for name in ("signing-intent.json", "signing.json"):
        _require_state_file(
            target_root / name,
            label=f"{target.id} {name}",
        )


def _require_sealed_macos_target_state(
    target_root: Path,
    target: ReleaseTarget,
) -> None:
    _candidate_directory(
        target_root,
        label=f"sealed macOS work root for {target.id}",
    )
    _candidate_directory(
        target_root / "candidate",
        label=f"sealed candidate directory for {target.id}",
    )
    _candidate_directory(
        target_root / "apple-command-receipts",
        label=f"sealed command receipt directory for {target.id}",
    )
    _require_sealed_signing_state(target_root, target)
    _require_state_file(
        target_root / "archive.json",
        label=f"{target.id} archive.json",
    )


def _fsync_candidate_directory(path: Path, *, label: str) -> None:
    descriptor = -1
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        named = os.lstat(path)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise FinalizationError(
                f"{label} is not the expected directory during synchronization"
            )
        os.fsync(descriptor)
    except FinalizationError:
        raise
    except OSError as error:
        raise FinalizationError(f"cannot synchronize {label} {path}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_candidate_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise FinalizationError(
                f"candidate contains unsupported filesystem entry: {path}"
            )
        if path.is_dir():
            directories.append(path)
    for directory in sorted(
        directories,
        key=lambda path: len(path.relative_to(root).parts),
        reverse=True,
    ):
        _fsync_candidate_directory(directory, label="candidate directory")


def _candidate_signing_state_exists(target_root: Path) -> bool:
    return any(
        os.path.lexists(target_root / name)
        for name in ("signing-intent.json", "signing.json")
    )


def _promote_candidate(partial: Path, candidate: Path, target_root: Path) -> None:
    _candidate_directory(partial, label="candidate staging directory")
    before = os.lstat(partial)
    if os.path.lexists(candidate):
        raise FinalizationError(f"refusing to replace existing candidate: {candidate}")
    rename_error: OSError | None = None
    try:
        os.rename(partial, candidate)
    except OSError as error:
        rename_error = error

    try:
        partial_after = os.lstat(partial)
    except FileNotFoundError:
        partial_after = None
    except OSError as error:
        raise FinalizationError(
            f"candidate promotion outcome is indeterminate; preserve {partial}: {error}"
        ) from error
    try:
        candidate_after = os.lstat(candidate)
    except FileNotFoundError:
        candidate_after = None
    except OSError as error:
        raise FinalizationError(
            f"candidate promotion outcome is indeterminate; preserve {partial}: {error}"
        ) from error

    original_identity = (before.st_dev, before.st_ino)
    candidate_identity = (
        None
        if candidate_after is None
        else (candidate_after.st_dev, candidate_after.st_ino)
    )
    partial_identity = (
        None if partial_after is None else (partial_after.st_dev, partial_after.st_ino)
    )
    if candidate_identity == original_identity and partial_after is None:
        _candidate_directory(candidate, label="candidate directory")
        _fsync_candidate_directory(target_root, label="candidate parent directory")
        return
    if (
        rename_error is not None
        and partial_identity == original_identity
        and candidate_after is None
    ):
        raise FinalizationError(
            f"candidate promotion did not commit; preserved {partial}: {rename_error}"
        ) from rename_error
    detail = "no rename diagnostic" if rename_error is None else str(rename_error)
    raise FinalizationError(
        "candidate promotion outcome is indeterminate; preserve both paths and "
        f"reconcile before retry: {detail}"
    ) from rename_error


def extract_candidate(
    options: FinalizationOptions,
    target: ReleaseTarget,
    archive: Path,
    target_root: Path,
    *,
    sealed: bool = False,
) -> tuple[Path, Path]:
    candidate = target_root / "candidate"
    partial = target_root / "candidate.partial"
    expected = expected_archive_members(target.binary_name)
    if sealed:
        _candidate_directory(
            candidate,
            label=f"sealed candidate directory for {target.id}",
        )
        if os.path.lexists(partial):
            raise FinalizationError(
                f"sealed prepared state contains candidate.partial for {target.id}"
            )
    signing_state_exists = _candidate_signing_state_exists(target_root)

    if os.path.lexists(candidate):
        _candidate_directory(candidate, label="candidate directory")
        if os.path.lexists(partial):
            raise FinalizationError(
                "candidate and candidate.partial both exist; preserve and reconcile "
                f"the attempt before retry: {target.id}"
            )
        package = candidate / "app-icon-toolkit"
        try:
            binary = _validate_extracted_candidate(options.plugin_root, package, target)
            if not signing_state_exists:
                original_sha256 = candidate_binary_sha256(options, target, archive)
                if sha256_file(
                    binary,
                    label=f"resumed unsigned {target.id} binary",
                ) != original_sha256:
                    raise FinalizationError(
                        f"resumed unsigned candidate differs from its archive: {target.id}"
                    )
            return package, binary
        except FinalizationError:
            if sealed or signing_state_exists:
                raise
            try:
                recover_incomplete_extraction(candidate, expected)
                os.rmdir(candidate)
            except (OSError, RuntimeError) as error:
                raise FinalizationError(
                    f"incomplete candidate cannot be recovered safely: {target.id}: {error}"
                ) from error

    if sealed:
        raise FinalizationError(
            f"sealed prepared state is missing its candidate for {target.id}"
        )
    if signing_state_exists:
        raise FinalizationError(
            f"candidate is missing after signing state was persisted: {target.id}"
        )

    if os.path.lexists(partial):
        _candidate_directory(partial, label="candidate staging directory")
        try:
            recover_incomplete_extraction(partial, expected)
        except RuntimeError as error:
            raise FinalizationError(
                f"candidate staging cannot be recovered safely: {target.id}: {error}"
            ) from error
    else:
        try:
            partial.mkdir(mode=0o700)
        except OSError as error:
            raise FinalizationError(
                f"cannot create candidate staging root for {target.id}: {error}"
            ) from error
        _candidate_directory(partial, label="candidate staging directory")
        _fsync_candidate_directory(target_root, label="candidate parent directory")

    package = _extract_candidate_archive(
        archive,
        target.archive_format,
        partial,
        target.binary_name,
        context=f"cannot extract downloaded candidate for {target.id}",
    )
    binary = _validate_extracted_candidate(options.plugin_root, package, target)
    _fsync_candidate_tree(partial)
    _promote_candidate(partial, candidate, target_root)
    promoted_package = candidate / "app-icon-toolkit"
    promoted_binary = _validate_extracted_candidate(
        options.plugin_root,
        promoted_package,
        target,
    )
    return promoted_package, promoted_binary


def _signature_payload(
    input_sha256: str,
    verification: macos_signing.SignatureVerificationReceipt,
) -> dict[str, Any]:
    return {
        "input_sha256": input_sha256,
        "signed": asdict(verification),
    }


def candidate_binary_sha256(
    options: FinalizationOptions,
    target: ReleaseTarget,
    candidate_archive: Path,
) -> str:
    """Hash the immutable unsigned binary directly from the downloaded archive."""

    with tempfile.TemporaryDirectory(prefix="app-icon-unsigned-candidate-") as temporary:
        extraction = Path(temporary)
        package = _extract_candidate_archive(
            candidate_archive,
            target.archive_format,
            extraction,
            target.binary_name,
            context=f"cannot inspect original candidate archive for {target.id}",
        )
        binary = _validate_extracted_candidate(options.plugin_root, package, target)
        return sha256_file(binary, label=f"original unsigned {target.id} binary")


def sign_candidate(
    options: FinalizationOptions,
    contract: ReleaseContract,
    target: ReleaseTarget,
    candidate_archive: Path,
    target_root: Path,
    binary: Path,
    runner: AuditedMacRunner,
    *,
    sealed: bool = False,
) -> macos_signing.SignatureVerificationReceipt:
    if sealed:
        _require_sealed_signing_state(target_root, target)
    expected_architectures = target.macos_architectures()
    check_release_binary(options.plugin_root, target, binary)
    unsigned_sha256 = candidate_binary_sha256(options, target, candidate_archive)
    intent = {
        "target": target.id,
        "candidate_archive_sha256": sha256_file(
            candidate_archive,
            label=f"candidate {target.id} archive",
        ),
        "unsigned_binary_sha256": unsigned_sha256,
        "identity_sha1": options.identity_sha1,
        "identifier": contract.macos_signing.code_identifier,
        "team_id": contract.macos_signing.team_id,
        "architectures": list(expected_architectures),
    }
    intent_path = target_root / "signing-intent.json"
    intent_existed = os.path.lexists(intent_path)
    _persist_or_verify_receipt(
        target_root,
        intent_path.name,
        intent,
        sealed=sealed,
    )
    signing_receipt = target_root / "signing.json"

    if sealed:
        _require_state_file(
            signing_receipt,
            label=f"{target.id} signing.json",
        )
    if sealed or os.path.lexists(signing_receipt):
        verification = macos_signing.verify_signed(
            binary,
            expected_architectures=expected_architectures,
            identity_sha1=options.identity_sha1,
            identifier=contract.macos_signing.code_identifier,
            team_id=contract.macos_signing.team_id,
            runner=runner,
        )
        _persist_or_verify_receipt(
            target_root,
            signing_receipt.name,
            _signature_payload(unsigned_sha256, verification),
            sealed=sealed,
        )
        check_release_binary(options.plugin_root, target, binary)
        return verification

    verification: macos_signing.SignatureVerificationReceipt | None = None
    signed_error: macos_signing.MacSigningError | None = None
    if intent_existed:
        try:
            verification = macos_signing.verify_signed(
                binary,
                expected_architectures=expected_architectures,
                identity_sha1=options.identity_sha1,
                identifier=contract.macos_signing.code_identifier,
                team_id=contract.macos_signing.team_id,
                runner=runner,
            )
        except macos_signing.MacSigningError as error:
            signed_error = error

    if verification is None:
        try:
            actual = macos_signing.architectures(binary, runner)
            if len(actual) != len(expected_architectures) or set(actual) != set(
                expected_architectures
            ):
                raise FinalizationError("candidate Mach-O architectures differ from the contract")
            pre_signatures = macos_signing.inspect_pre_signatures(
                binary,
                expected_architectures,
                runner,
            )
        except (macos_signing.MacSigningError, FinalizationError) as pre_sign_error:
            detail = "no prior signing intent" if signed_error is None else str(signed_error)
            raise FinalizationError(
                f"candidate is not a uniformly unsigned input and signed recovery failed: "
                f"signed={detail}; pre-sign={pre_sign_error}"
            ) from pre_sign_error
        if any(
            state.kind is not macos_signing.PreSignKind.UNSIGNED
            for state in pre_signatures
        ):
            raise FinalizationError(
                f"release candidates must be uniformly unsigned before local signing: {target.id}"
            )
        current_sha256 = sha256_file(binary, label=f"unsigned {target.id} binary")
        if current_sha256 != unsigned_sha256:
            raise FinalizationError(
                f"unsigned working binary differs from the downloaded candidate: {target.id}"
            )
        signed = macos_signing.sign_and_verify(
            binary,
            expected_architectures=expected_architectures,
            identity_sha1=options.identity_sha1,
            identifier=contract.macos_signing.code_identifier,
            team_id=contract.macos_signing.team_id,
            runner=runner,
        )
        if signed.input_sha256 != unsigned_sha256:
            raise FinalizationError("signing receipt input digest differs from the candidate")
        verification = macos_signing.SignatureVerificationReceipt(
            signed_sha256=signed.signed_sha256,
            identity_sha1=signed.identity_sha1,
            identifier=signed.identifier,
            team_id=signed.team_id,
            architectures=signed.architectures,
            slices=signed.slices,
        )

    _persist_or_verify_receipt(
        target_root,
        signing_receipt.name,
        _signature_payload(unsigned_sha256, verification),
        sealed=sealed,
    )
    check_release_binary(options.plugin_root, target, binary)
    return verification


def _run_packager(
    options: FinalizationOptions,
    target: ReleaseTarget,
    binary: Path,
    assets: Path,
    source_epoch: int,
) -> None:
    required_command(
        (
            sys.executable,
            str(options.plugin_root / "scripts" / "prepare-release-package.py"),
            "--plugin-root",
            str(options.plugin_root),
            "--binary",
            str(binary),
            "--target",
            target.id,
            "--tag",
            options.binding.tag,
            "--format",
            target.archive_format,
            "--output",
            str(assets),
            "--source-date-epoch",
            str(source_epoch),
            "--verification-mode",
            "static-only",
        ),
        cwd=options.plugin_root,
    )


def validate_signed_archive(
    options: FinalizationOptions,
    contract: ReleaseContract,
    target: ReleaseTarget,
    archive: Path,
    signed_binary_sha256: str,
    runner: AuditedMacRunner,
) -> None:
    with tempfile.TemporaryDirectory(prefix="app-icon-signed-archive-") as temporary:
        extraction = Path(temporary)
        package = safe_extract_archive(
            archive,
            target.archive_format,
            extraction,
            expected_archive_members(target.binary_name),
        )
        binary = _validate_extracted_candidate(options.plugin_root, package, target)
        archived_binary_sha256 = sha256_file(
            binary,
            label=f"archived signed {target.id} binary",
        )
        if archived_binary_sha256 != signed_binary_sha256:
            raise FinalizationError(f"signed archive contains the wrong binary: {target.id}")
        check_release_binary(options.plugin_root, target, binary)
        macos_signing.verify_signed(
            binary,
            expected_architectures=target.macos_architectures(),
            identity_sha1=options.identity_sha1,
            identifier=contract.macos_signing.code_identifier,
            team_id=contract.macos_signing.team_id,
            runner=runner,
        )


def prepare_assets(
    options: FinalizationOptions,
    contract: ReleaseContract,
    attempt_root: Path,
    downloads: Path,
    source_epoch: int,
) -> tuple[Path, tuple[release_draft.LocalAsset, ...]]:
    prepared_receipt = attempt_root / "prepared.json"
    sealed = os.path.lexists(prepared_receipt)
    assets_path = attempt_root / "assets"
    work_path = attempt_root / "macos-work"
    if sealed:
        _candidate_directory(assets_path, label="sealed prepared asset directory")
        _candidate_directory(work_path, label="sealed macOS work directory")
        assets = assets_path
        work = work_path
        sealed_assets = _validate_complete_prepared_assets(options, contract, assets)
        _persist_or_verify_receipt(
            attempt_root,
            "prepared.json",
            _prepared_receipt_payload(options, sealed_assets),
            sealed=True,
        )
        for target in contract.targets:
            if target.family in MACOS_FAMILIES:
                _require_sealed_macos_target_state(work / target.id, target)
    else:
        assets = private_subdirectory(attempt_root, "assets")
        work = private_subdirectory(attempt_root, "macos-work")

    for target in contract.targets:
        name = target.release_filename(options.binding.tag)
        source = downloads / name
        destination = assets / name
        if target.family not in MACOS_FAMILIES:
            source_sha256 = sha256_file(source, label=f"CI archive {name}")
            if os.path.lexists(destination):
                if sha256_file(destination, label=f"prepared archive {name}") != source_sha256:
                    raise FinalizationError(f"prepared non-macOS archive differs from CI: {name}")
            else:
                if sealed:
                    raise FinalizationError(
                        f"sealed prepared state is missing archive {name}"
                    )
                copy_regular_file(
                    source,
                    destination,
                    mode=0o644,
                    label=f"CI archive {name}",
                )
            continue

        if sealed:
            target_root = work / target.id
            _require_sealed_macos_target_state(target_root, target)
            audit = target_root / "apple-command-receipts"
        else:
            target_root = private_subdirectory(work, target.id)
            audit = private_subdirectory(target_root, "apple-command-receipts")
        runner = AuditedMacRunner(audit)
        _package, binary = extract_candidate(
            options,
            target,
            source,
            target_root,
            sealed=sealed,
        )
        verification = sign_candidate(
            options,
            contract,
            target,
            source,
            target_root,
            binary,
            runner,
            sealed=sealed,
        )
        if not os.path.lexists(destination):
            if sealed:
                raise FinalizationError(
                    f"sealed prepared state is missing archive {name}"
                )
            _run_packager(options, target, binary, assets, source_epoch)
        validate_signed_archive(
            options,
            contract,
            target,
            destination,
            verification.signed_sha256,
            runner,
        )
        _persist_or_verify_receipt(
            target_root,
            "archive.json",
            {
                "target": target.id,
                "archive": name,
                "archive_sha256": sha256_file(
                    destination,
                    label=f"signed release archive {name}",
                ),
                "signed_binary_sha256": verification.signed_sha256,
            },
            sealed=sealed,
        )

    prepared_archives = archive_paths(assets, contract, options.binding.tag)
    checksum_path = assets / release_draft.CHECKSUM_ASSET_NAME
    preliminary_names = tuple(sorted(prepared_archives))
    checksum_exists = os.path.lexists(checksum_path)
    if checksum_exists:
        preliminary_names = tuple(sorted((*preliminary_names, checksum_path.name)))
    _require_exact_prepared_files(assets, preliminary_names)
    archive_assets = release_draft.snapshot_local_assets(
        prepared_archives,
        expected_names=tuple(sorted(prepared_archives)),
    )
    expected_checksum = release_draft.render_sha256sums(archive_assets)
    if checksum_exists:
        actual_checksum = _read_stable_checksum(checksum_path, expected_checksum)
        if actual_checksum != expected_checksum:
            raise FinalizationError("persisted SHA256SUMS differs from prepared archives")
    else:
        if sealed:
            raise FinalizationError(
                "sealed prepared state is missing its SHA256SUMS asset"
            )
        release_draft.generate_sha256sums(archive_assets, checksum_path)
    all_assets = _validate_complete_prepared_assets(options, contract, assets)
    _persist_or_verify_receipt(
        attempt_root,
        "prepared.json",
        _prepared_receipt_payload(options, all_assets),
        sealed=sealed,
    )
    return assets, all_assets
