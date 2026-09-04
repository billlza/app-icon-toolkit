"""Shared allowlist and safe extraction primitives for release packages."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
from typing import BinaryIO
import zipfile


PACKAGE_ROOT_NAME = "app-icon-toolkit"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 64
COPY_BUFFER_BYTES = 1024 * 1024

STATIC_PATHS = (
    Path(".agents/plugins/marketplace.json"),
    Path(".codex-plugin/plugin.json"),
    Path(".mcp.json"),
    Path("ARCHITECTURE.md"),
    Path("CHANGELOG.md"),
    Path("CODEX_HOST_TEST_VERSION"),
    Path("CONTRIBUTING.md"),
    Path("INSTALL.md"),
    Path("LICENSE"),
    Path("README.md"),
    Path("SECURITY.md"),
    Path("THIRD_PARTY_LICENSES.html"),
)


def expected_archive_members(binary_name: str) -> tuple[str, ...]:
    relatives = (*STATIC_PATHS, Path("bin") / binary_name)
    return tuple(
        (PurePosixPath(PACKAGE_ROOT_NAME) / relative.as_posix()).as_posix()
        for relative in relatives
    )


def safe_extract_archive(
    archive_path: Path,
    archive_format: str,
    extraction_root: Path,
    expected_members: Iterable[str],
    expected_sizes: Mapping[str, int] | None = None,
) -> Path:
    archive_path = Path(os.path.abspath(archive_path))
    if archive_path.is_symlink() or not archive_path.is_file():
        raise RuntimeError(f"release archive is not a regular file: {archive_path}")
    archive_path = archive_path.resolve(strict=True)
    archive_size = archive_path.stat().st_size
    if archive_size == 0 or archive_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError(
            f"release archive size {archive_size} is outside the allowed range"
        )
    if (
        extraction_root.is_symlink()
        or not extraction_root.is_dir()
        or any(extraction_root.iterdir())
    ):
        raise RuntimeError(f"extraction root must be an empty directory: {extraction_root}")

    expected = tuple(expected_members)
    _validate_expected_members(expected)
    if expected_sizes is not None and set(expected_sizes) != set(expected):
        raise RuntimeError("expected archive size map does not match the member allowlist")

    if archive_format == "tar.gz":
        _extract_tar_gz(archive_path, extraction_root, expected, expected_sizes)
    elif archive_format == "zip":
        _extract_zip(archive_path, extraction_root, expected, expected_sizes)
    else:
        raise RuntimeError(f"unsupported release archive format: {archive_format}")
    return extraction_root / PACKAGE_ROOT_NAME


def _validate_expected_members(expected: tuple[str, ...]) -> None:
    if not expected or len(expected) > MAX_MEMBERS:
        raise RuntimeError("release archive member allowlist has an invalid size")
    if len(set(expected)) != len(expected):
        raise RuntimeError("release archive member allowlist contains duplicates")
    for name in expected:
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or path.parts[0] != PACKAGE_ROOT_NAME
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise RuntimeError(f"unsafe expected release archive member: {name!r}")


def _validate_member_names(actual: list[str], expected: tuple[str, ...]) -> None:
    if len(actual) > MAX_MEMBERS:
        raise RuntimeError(f"release archive contains more than {MAX_MEMBERS} members")
    if len(set(actual)) != len(actual):
        raise RuntimeError("release archive contains duplicate members")
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise RuntimeError(
            f"release archive member mismatch: missing={missing}, extra={extra}"
        )


def _expected_mode(name: str) -> int:
    return 0o755 if PurePosixPath(name).parent.name == "bin" else 0o644


def _validate_member_metadata(
    name: str,
    size: int,
    mode: int,
    expected_sizes: Mapping[str, int] | None,
) -> int:
    if size < 0 or size > MAX_MEMBER_BYTES:
        raise RuntimeError(f"release archive member has an invalid size: {name}")
    if expected_sizes is not None and size != expected_sizes[name]:
        raise RuntimeError(
            f"release archive member size changed for {name}: "
            f"found {size}, expected {expected_sizes[name]}"
        )
    expected_mode = _expected_mode(name)
    if mode & 0o777 != expected_mode:
        raise RuntimeError(
            f"release archive member mode changed for {name}: "
            f"found {mode & 0o777:o}, expected {expected_mode:o}"
        )
    return size


def _validate_total_size(total: int) -> None:
    if total > MAX_TOTAL_EXTRACTED_BYTES:
        raise RuntimeError(
            f"release archive expands beyond {MAX_TOTAL_EXTRACTED_BYTES} bytes"
        )


def _destination(extraction_root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    return extraction_root.joinpath(*relative.parts)


def _copy_member(source: BinaryIO, destination: Path, expected_size: int, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    with destination.open("xb") as output:
        while True:
            chunk = source.read(COPY_BUFFER_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > expected_size:
                raise RuntimeError(f"release archive member exceeded its declared size: {destination}")
            output.write(chunk)
    if copied != expected_size:
        raise RuntimeError(
            f"release archive member was truncated: {destination}; "
            f"read {copied}, expected {expected_size}"
        )
    destination.chmod(mode)


def _extract_tar_gz(
    archive_path: Path,
    extraction_root: Path,
    expected: tuple[str, ...],
    expected_sizes: Mapping[str, int] | None,
) -> None:
    with tarfile.open(archive_path, mode="r:gz") as archive:
        expected_set = set(expected)
        seen: set[str] = set()
        by_name: dict[str, tarfile.TarInfo] = {}
        total = 0
        while True:
            member = archive.next()
            if member is None:
                break
            if len(seen) >= MAX_MEMBERS:
                raise RuntimeError(f"release archive contains more than {MAX_MEMBERS} members")
            if member.name in seen:
                raise RuntimeError("release archive contains duplicate members")
            if member.name not in expected_set:
                raise RuntimeError(f"release archive contains an unexpected member: {member.name}")
            if not member.isfile() or member.sparse is not None:
                raise RuntimeError(
                    f"release archive member is not an ordinary file: {member.name}"
                )
            total += _validate_member_metadata(
                member.name, member.size, member.mode, expected_sizes
            )
            _validate_total_size(total)
            seen.add(member.name)
            by_name[member.name] = member

        _validate_member_names(list(seen), expected)

        for name in expected:
            member = by_name[name]
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"release archive member cannot be read: {name}")
            with source:
                _copy_member(
                    source,
                    _destination(extraction_root, name),
                    member.size,
                    _expected_mode(name),
                )


def _extract_zip(
    archive_path: Path,
    extraction_root: Path,
    expected: tuple[str, ...],
    expected_sizes: Mapping[str, int] | None,
) -> None:
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        members = archive.infolist()
        _validate_member_names([member.filename for member in members], expected)
        total = 0
        by_name: dict[str, zipfile.ZipInfo] = {}
        for member in members:
            unix_mode = member.external_attr >> 16
            file_type = stat.S_IFMT(unix_mode)
            if member.is_dir() or file_type not in {0, stat.S_IFREG}:
                raise RuntimeError(
                    f"release archive member is not an ordinary file: {member.filename}"
                )
            if member.flag_bits & 0x1:
                raise RuntimeError(f"release archive member is encrypted: {member.filename}")
            total += _validate_member_metadata(
                member.filename, member.file_size, unix_mode, expected_sizes
            )
            _validate_total_size(total)
            by_name[member.filename] = member

        for name in expected:
            member = by_name[name]
            with archive.open(member, mode="r") as source:
                _copy_member(
                    source,
                    _destination(extraction_root, name),
                    member.file_size,
                    _expected_mode(name),
                )
