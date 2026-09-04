"""Shared allowlist and safe extraction primitives for release packages."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
import gzip
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
from typing import BinaryIO
import zipfile
import zlib

from release_files import (
    FileSnapshot,
    open_stable_regular_file,
    opened_and_named_snapshots_agree,
)
from release_zip_preflight import (
    ZIP_CENTRAL_DIRECTORY_HEADER,
    scan_classic_zip,
)


PACKAGE_ROOT_NAME = "app-icon-toolkit"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 64
COPY_BUFFER_BYTES = 1024 * 1024
MAX_MEMBER_NAME_BYTES = 1024
MAX_TAR_STREAM_BYTES = (
    MAX_TOTAL_EXTRACTED_BYTES
    + MAX_MEMBERS * (2 * tarfile.BLOCKSIZE)
    + tarfile.RECORDSIZE
)

MAX_ZIP_CENTRAL_DIRECTORY_BYTES = MAX_MEMBERS * (
    ZIP_CENTRAL_DIRECTORY_HEADER.size + MAX_MEMBER_NAME_BYTES
)

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


class ReleasePackageError(RuntimeError):
    """A release archive could not be validated or extracted safely."""


class ReleasePackageCleanupError(ReleasePackageError):
    """A failed extraction left state that could not be safely removed."""

    def __init__(self, primary: BaseException, cleanup: BaseException) -> None:
        super().__init__(
            f"release archive extraction failed ({primary}); "
            f"allowlisted cleanup also failed ({cleanup})"
        )
        self.primary = primary
        self.cleanup = cleanup


_DIRECTORY_FD_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
    and os.scandir in os.supports_fd
)


def _is_ordinary_directory(metadata: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not (reparse_attribute and file_attributes & reparse_attribute)
    )


def _filesystem_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_windows_directory_guard(path: Path) -> tuple[object, object]:
    """Open a Windows directory without delete sharing so its name cannot move."""

    if os.name != "nt":
        raise RuntimeError("Windows directory guards are only available on Windows")
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    file_read_attributes = 0x0080
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    handle = create_file(
        str(path),
        file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle, close_handle


class _ExtractionRoot:
    """Stable capability for one caller-provided extraction directory."""

    def __init__(self, path: Path) -> None:
        self.path = Path(os.path.abspath(path))
        self._descriptor: int | None = None
        self._windows_handle: object | None = None
        self._windows_close_handle: object | None = None
        self._created_directories: dict[tuple[str, ...], tuple[int, int]] = {}
        self._created_files: dict[tuple[str, ...], tuple[int, int]] = {}
        self._completed_files: dict[
            tuple[str, ...], tuple[int, int, int, int, int, int, int]
        ] = {}
        try:
            before = os.lstat(self.path)
        except OSError as error:
            raise ReleasePackageError(
                f"cannot inspect extraction root {self.path}: {error}"
            ) from error
        self._validate_directory(before, label="extraction root")
        self._identity = _filesystem_identity(before)

        try:
            if _DIRECTORY_FD_SUPPORTED:
                self._descriptor = os.open(self.path, _directory_open_flags())
                opened = os.fstat(self._descriptor)
                self._validate_directory(opened, label="opened extraction root")
                if _filesystem_identity(opened) != self._identity:
                    raise ReleasePackageError(
                        f"extraction root changed while it was being opened: {self.path}"
                    )
            elif os.name == "nt":
                (
                    self._windows_handle,
                    self._windows_close_handle,
                ) = _open_windows_directory_guard(self.path)
                self.assert_named_identity()
            else:
                raise ReleasePackageError(
                    "this platform cannot bind extraction to a stable directory handle"
                )
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> _ExtractionRoot:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        if self._windows_handle is not None:
            assert self._windows_close_handle is not None
            close_result = self._windows_close_handle(self._windows_handle)
            self._windows_handle = None
            self._windows_close_handle = None
            if not close_result:
                import ctypes

                raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _validate_directory(metadata: os.stat_result, *, label: str) -> None:
        if not _is_ordinary_directory(metadata):
            raise ReleasePackageError(f"{label} is not an ordinary directory")
        if not _entry_is_owned(metadata):
            raise ReleasePackageError(f"{label} is not owned by the current user")
        if hasattr(os, "getuid") and stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ReleasePackageError(
                f"{label} must not be writable by group or other users"
            )

    def assert_named_identity(self) -> None:
        try:
            current = os.lstat(self.path)
        except OSError as error:
            raise ReleasePackageError(
                f"cannot re-inspect extraction root {self.path}: {error}"
            ) from error
        self._validate_directory(current, label="named extraction root")
        if _filesystem_identity(current) != self._identity:
            raise ReleasePackageError(
                f"extraction root path changed during operation: {self.path}"
            )

    def display_path(self, parts: tuple[str, ...]) -> Path:
        return self.path.joinpath(*parts)

    @staticmethod
    def _stat_named_child(parent: int | Path, name: str) -> os.stat_result:
        if isinstance(parent, int):
            return os.stat(
                name,
                dir_fd=parent,
                follow_symlinks=False,
            )
        return os.lstat(parent / name)

    @contextmanager
    def _open_directory(
        self,
        parts: tuple[str, ...],
        expected: Mapping[tuple[str, ...], tuple[int, int]] | None = None,
    ) -> Iterator[int | Path]:
        if self._descriptor is None:
            self.assert_named_identity()
            directory = self.display_path(parts)
            try:
                metadata = os.lstat(directory)
            except OSError as error:
                raise ReleasePackageError(
                    f"cannot inspect extraction directory {directory}: {error}"
                ) from error
            self._validate_directory(metadata, label=f"extraction directory {directory}")
            if expected is not None and parts and (
                expected.get(parts) != _filesystem_identity(metadata)
            ):
                raise ReleasePackageError(
                    f"extraction directory changed during operation: {directory}"
                )
            yield directory
            return

        descriptor = os.dup(self._descriptor)
        traversed: tuple[str, ...] = ()
        try:
            for component in parts:
                traversed = (*traversed, component)
                next_descriptor = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
                metadata = os.fstat(descriptor)
                self._validate_directory(
                    metadata,
                    label=f"extraction directory {self.display_path(traversed)}",
                )
                if expected is not None and (
                    expected.get(traversed) != _filesystem_identity(metadata)
                ):
                    raise ReleasePackageError(
                        "extraction directory changed during operation: "
                        f"{self.display_path(traversed)}"
                    )
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _open_or_create_directory(
        self,
        parts: tuple[str, ...],
    ) -> Iterator[int | Path]:
        if self._descriptor is None:
            self.assert_named_identity()
            traversed: tuple[str, ...] = ()
            for component in parts:
                traversed = (*traversed, component)
                directory = self.display_path(traversed)
                try:
                    directory.mkdir(mode=0o700)
                    created = True
                except FileExistsError:
                    created = False
                metadata = os.lstat(directory)
                self._validate_directory(
                    metadata,
                    label=f"extraction directory {directory}",
                )
                identity = _filesystem_identity(metadata)
                if created:
                    self._created_directories[traversed] = identity
                elif self._created_directories.get(traversed) != identity:
                    raise ReleasePackageError(
                        f"unexpected pre-existing extraction directory: {directory}"
                    )
            yield self.display_path(parts)
            return

        descriptor = os.dup(self._descriptor)
        traversed: tuple[str, ...] = ()
        try:
            for component in parts:
                traversed = (*traversed, component)
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    created = False
                next_descriptor = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
                if created and hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o700)
                metadata = os.fstat(descriptor)
                self._validate_directory(
                    metadata,
                    label=f"extraction directory {self.display_path(traversed)}",
                )
                identity = _filesystem_identity(metadata)
                if created:
                    self._created_directories[traversed] = identity
                elif self._created_directories.get(traversed) != identity:
                    raise ReleasePackageError(
                        "unexpected pre-existing extraction directory: "
                        f"{self.display_path(traversed)}"
                    )
            yield descriptor
        finally:
            os.close(descriptor)

    def children(
        self,
        parts: tuple[str, ...],
    ) -> list[tuple[str, os.stat_result]]:
        with self._open_directory(parts) as directory:
            try:
                with os.scandir(directory) as entries:
                    names = sorted(entry.name for entry in entries)
                return [
                    (name, self._stat_named_child(directory, name))
                    for name in names
                ]
            except OSError as error:
                raise ReleasePackageError(
                    "cannot inspect incomplete extraction directory "
                    f"{self.display_path(parts)}: {error}"
                ) from error

    def copy_member(
        self,
        source: BinaryIO,
        name: str,
        expected_size: int,
        mode: int,
    ) -> None:
        parts = tuple(PurePosixPath(name).parts)
        copied = 0
        descriptor = -1
        with self._open_or_create_directory(parts[:-1]) as parent:
            try:
                if isinstance(parent, int):
                    descriptor = os.open(
                        parts[-1],
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        mode,
                        dir_fd=parent,
                    )
                    output_context = os.fdopen(descriptor, "wb")
                    descriptor = -1
                else:
                    output_context = (parent / parts[-1]).open("xb")
                with output_context as output:
                    opened = os.fstat(output.fileno())
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or not _entry_is_owned(opened)
                    ):
                        raise ReleasePackageError(
                            "new extraction member is not a single-link ordinary file: "
                            f"{self.display_path(parts)}"
                        )
                    named_opened = self._stat_named_child(parent, parts[-1])
                    if (
                        not stat.S_ISREG(named_opened.st_mode)
                        or named_opened.st_nlink != 1
                        or not _entry_is_owned(named_opened)
                        or opened.st_size != 0
                        or named_opened.st_size != 0
                        or not opened_and_named_snapshots_agree(
                            FileSnapshot.from_stat(opened),
                            FileSnapshot.from_stat(named_opened),
                        )
                    ):
                        raise ReleasePackageError(
                            "new extraction member path does not identify the opened file: "
                            f"{self.display_path(parts)}"
                        )
                    opened_identity = _filesystem_identity(opened)
                    named_identity = _filesystem_identity(named_opened)
                    self._created_files[parts] = named_identity
                    while True:
                        chunk = source.read(COPY_BUFFER_BYTES)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > expected_size:
                            raise RuntimeError(
                                "release archive member exceeded its declared size: "
                                f"{self.display_path(parts)}"
                            )
                        output.write(chunk)
                    if hasattr(os, "fchmod"):
                        os.fchmod(output.fileno(), mode)
                    output.flush()
                    os.fsync(output.fileno())
                    opened_after = os.fstat(output.fileno())
                    named_after = self._stat_named_child(parent, parts[-1])
                    if (
                        not stat.S_ISREG(opened_after.st_mode)
                        or opened_after.st_nlink != 1
                        or not _entry_is_owned(opened_after)
                        or _filesystem_identity(opened_after) != opened_identity
                        or not stat.S_ISREG(named_after.st_mode)
                        or named_after.st_nlink != 1
                        or not _entry_is_owned(named_after)
                        or _filesystem_identity(named_after) != named_identity
                        or opened_after.st_size != copied
                        or not opened_and_named_snapshots_agree(
                            FileSnapshot.from_stat(opened_after),
                            FileSnapshot.from_stat(named_after),
                        )
                    ):
                        raise ReleasePackageError(
                            "extraction member path changed while it was being written: "
                            f"{self.display_path(parts)}"
                        )
                if copied != expected_size:
                    raise RuntimeError(
                        f"release archive member was truncated: {self.display_path(parts)}; "
                        f"read {copied}, expected {expected_size}"
                    )
                if not hasattr(os, "fchmod"):
                    assert isinstance(parent, Path)
                    (parent / parts[-1]).chmod(mode)
                completed = self._stat_named_child(parent, parts[-1])
                if (
                    not stat.S_ISREG(completed.st_mode)
                    or completed.st_nlink != 1
                    or not _entry_is_owned(completed)
                    or _filesystem_identity(completed)
                    != self._created_files[parts]
                    or completed.st_size != expected_size
                ):
                    raise ReleasePackageError(
                        "extraction member changed after it was closed: "
                        f"{self.display_path(parts)}"
                    )
                self._completed_files[parts] = _entry_snapshot(completed)
            except BaseException:
                if descriptor >= 0:
                    os.close(descriptor)
                raise

    def assert_completed_members(self, expected: tuple[str, ...]) -> None:
        expected_parts = {tuple(PurePosixPath(name).parts) for name in expected}
        if (
            set(self._created_files) != expected_parts
            or set(self._completed_files) != expected_parts
        ):
            raise ReleasePackageError(
                "extraction did not complete the exact expected member set"
            )
        for parts in sorted(expected_parts):
            with self._open_directory(parts[:-1]) as parent:
                current = self._stat_named_child(parent, parts[-1])
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or not _entry_is_owned(current)
                or _filesystem_identity(current) != self._created_files[parts]
                or _entry_snapshot(current) != self._completed_files[parts]
            ):
                raise ReleasePackageError(
                    "extraction member changed after it was written: "
                    f"{self.display_path(parts)}"
                )

    def remove_file(
        self,
        parts: tuple[str, ...],
        before: os.stat_result,
        directories: Mapping[tuple[str, ...], tuple[int, int]],
    ) -> None:
        with self._open_directory(parts[:-1], directories) as parent:
            try:
                current = self._stat_named_child(parent, parts[-1])
                if _entry_snapshot(current) != _entry_snapshot(before):
                    raise ReleasePackageError(
                        "incomplete extraction file changed before cleanup: "
                        f"{self.display_path(parts)}"
                    )
                if isinstance(parent, int):
                    os.unlink(parts[-1], dir_fd=parent)
                else:
                    (parent / parts[-1]).unlink()
            except ReleasePackageError:
                raise
            except OSError as error:
                raise ReleasePackageError(
                    f"cannot remove incomplete extraction file "
                    f"{self.display_path(parts)}: {error}"
                ) from error

    def remove_directory(
        self,
        parts: tuple[str, ...],
        before: os.stat_result,
        directories: Mapping[tuple[str, ...], tuple[int, int]],
    ) -> None:
        with self._open_directory(parts[:-1], directories) as parent:
            try:
                current = self._stat_named_child(parent, parts[-1])
                if (
                    not _is_ordinary_directory(current)
                    or _filesystem_identity(current) != _filesystem_identity(before)
                    or not _entry_is_owned(current)
                ):
                    raise ReleasePackageError(
                        "incomplete extraction directory changed before cleanup: "
                        f"{self.display_path(parts)}"
                    )
                if isinstance(parent, int):
                    os.rmdir(parts[-1], dir_fd=parent)
                else:
                    (parent / parts[-1]).rmdir()
            except ReleasePackageError:
                raise
            except OSError as error:
                raise ReleasePackageError(
                    f"cannot remove incomplete extraction directory "
                    f"{self.display_path(parts)}: {error}"
                ) from error


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
    extraction_root = Path(os.path.abspath(extraction_root))
    expected = tuple(expected_members)
    _validate_expected_members(expected)
    if expected_sizes is not None and set(expected_sizes) != set(expected):
        raise ReleasePackageError(
            "expected archive size map does not match the member allowlist"
        )

    with _ExtractionRoot(extraction_root) as root:
        if root.children(()):
            raise ReleasePackageError(
                f"extraction root must be an empty directory: {extraction_root}"
            )
        try:
            with open_stable_regular_file(
                archive_path,
                label="release archive",
                require_single_link=True,
            ) as (archive_file, archive_snapshot):
                if archive_snapshot.size > MAX_ARCHIVE_BYTES:
                    raise ReleasePackageError(
                        f"release archive size {archive_snapshot.size} is outside the allowed range"
                    )
                if archive_format == "tar.gz":
                    _extract_tar_gz(
                        archive_file,
                        root,
                        expected,
                        expected_sizes,
                    )
                elif archive_format == "zip":
                    _extract_zip(
                        archive_file,
                        archive_snapshot.size,
                        root,
                        expected,
                        expected_sizes,
                    )
                else:
                    raise ReleasePackageError(
                        f"unsupported release archive format: {archive_format}"
                    )
            root.assert_completed_members(expected)
            root.assert_named_identity()
        except BaseException as error:
            primary: BaseException
            if isinstance(error, ReleasePackageError):
                primary = error
            elif isinstance(
                error,
                (
                    EOFError,
                    OSError,
                    RuntimeError,
                    tarfile.TarError,
                    zipfile.BadZipFile,
                    zlib.error,
                ),
            ):
                primary = ReleasePackageError(
                    f"release archive could not be extracted safely: {error}"
                )
            else:
                primary = error
            try:
                _recover_incomplete_extraction(
                    root,
                    expected,
                    require_created_identity=True,
                )
            except BaseException as cleanup_error:
                raise ReleasePackageCleanupError(primary, cleanup_error) from primary
            if primary is error:
                raise
            raise primary from error
    return extraction_root / PACKAGE_ROOT_NAME


def _entry_snapshot(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _entry_is_owned(metadata: os.stat_result) -> bool:
    return not hasattr(os, "getuid") or metadata.st_uid == os.getuid()


def recover_incomplete_extraction(
    extraction_root: Path,
    expected_members: Iterable[str],
) -> None:
    """Remove only a proven allowlisted extraction subset, leaving its root."""

    root = Path(os.path.abspath(extraction_root))
    expected = tuple(expected_members)
    _validate_expected_members(expected)
    with _ExtractionRoot(root) as root_handle:
        _recover_incomplete_extraction(
            root_handle,
            expected,
            require_created_identity=False,
        )


def _recover_incomplete_extraction(
    root: _ExtractionRoot,
    expected: tuple[str, ...],
    *,
    require_created_identity: bool,
) -> None:
    """Validate first, then remove an allowlisted subset through one root handle."""

    expected_files = {
        tuple(PurePosixPath(name).parts)
        for name in expected
    }
    expected_directories = {
        parts[:depth]
        for parts in expected_files
        for depth in range(1, len(parts))
    }
    files: list[tuple[tuple[str, ...], os.stat_result]] = []
    directories: list[tuple[tuple[str, ...], os.stat_result]] = []
    pending: list[tuple[str, ...]] = [()]

    while pending:
        prefix = pending.pop()
        for entry_name, metadata in root.children(prefix):
            parts = (*prefix, entry_name)
            path = root.display_path(parts)
            if not _entry_is_owned(metadata):
                raise ReleasePackageError(
                    f"incomplete extraction entry is not owned by the current user: {path}"
                )
            if _is_ordinary_directory(metadata):
                if parts not in expected_directories:
                    raise ReleasePackageError(
                        f"incomplete extraction contains an unexpected directory: {path}"
                    )
                if require_created_identity and (
                    root._created_directories.get(parts)
                    != _filesystem_identity(metadata)
                ):
                    raise ReleasePackageError(
                        "incomplete extraction directory was not created by this "
                        f"operation: {path}"
                    )
                directories.append((parts, metadata))
                pending.append(parts)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ReleasePackageError(
                    f"incomplete extraction contains a linked or non-ordinary entry: {path}"
                )
            if parts not in expected_files:
                raise ReleasePackageError(
                    f"incomplete extraction contains an unexpected file: {path}"
                )
            if require_created_identity and (
                root._created_files.get(parts) != _filesystem_identity(metadata)
            ):
                raise ReleasePackageError(
                    "incomplete extraction file was not created by this operation: "
                    f"{path}"
                )
            files.append((parts, metadata))

    directory_identities = {
        parts: _filesystem_identity(metadata) for parts, metadata in directories
    }
    for parts, before in files:
        root.remove_file(parts, before, directory_identities)

    for parts, before in sorted(
        directories,
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        root.remove_directory(parts, before, directory_identities)

    if root.children(()):
        raise ReleasePackageError(
            f"incomplete extraction root is not empty after cleanup: {root.path}"
        )
    root.assert_named_identity()


def _validate_expected_members(expected: tuple[str, ...]) -> None:
    if not expected or len(expected) > MAX_MEMBERS:
        raise RuntimeError("release archive member allowlist has an invalid size")
    if len(set(expected)) != len(expected):
        raise RuntimeError("release archive member allowlist contains duplicates")
    for name in expected:
        path = PurePosixPath(name)
        try:
            encoded_name = name.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise RuntimeError(
                f"unsafe expected release archive member: {name!r}"
            ) from error
        if (
            not name
            or "\0" in name
            or "\\" in name
            or len(encoded_name) > MAX_MEMBER_NAME_BYTES
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
    observed_mode = stat.S_IMODE(mode)
    if observed_mode != expected_mode:
        raise RuntimeError(
            f"release archive member mode changed for {name}: "
            f"found {observed_mode:o}, expected {expected_mode:o}"
        )
    return size


def _validate_total_size(total: int) -> None:
    if total > MAX_TOTAL_EXTRACTED_BYTES:
        raise RuntimeError(
            f"release archive expands beyond {MAX_TOTAL_EXTRACTED_BYTES} bytes"
        )


def _copy_member(
    source: BinaryIO,
    extraction_root: _ExtractionRoot,
    name: str,
    expected_size: int,
    mode: int,
) -> None:
    extraction_root.copy_member(source, name, expected_size, mode)


class _BoundedDecompressedStream:
    """Read a compressed stream without crossing its declared expansion bound."""

    def __init__(self, source: BinaryIO, limit: int) -> None:
        self._source = source
        self._limit = limit
        self._consumed = 0

    def read(self, size: int) -> bytes:
        if size <= 0:
            raise ValueError("bounded stream reads must be positive")
        remaining = self._limit - self._consumed
        chunk = self._source.read(min(size, remaining + 1))
        self._consumed += len(chunk)
        if self._consumed > self._limit:
            raise RuntimeError(
                f"release TAR stream expands beyond {self._limit} bytes"
            )
        return chunk


def _read_exact(source: BinaryIO, size: int, *, label: str) -> bytes:
    if size < 0:
        raise ValueError("exact read size cannot be negative")
    result = bytearray()
    while len(result) < size:
        chunk = source.read(size - len(result))
        if not chunk:
            break
        result.extend(chunk)
    if len(result) != size:
        raise RuntimeError(
            f"{label} is truncated: read {len(result)} bytes, expected {size}"
        )
    return bytes(result)


def _discard_exact(source: BinaryIO, size: int, *, label: str) -> None:
    remaining = size
    while remaining:
        chunk_size = min(remaining, COPY_BUFFER_BYTES)
        _read_exact(source, chunk_size, label=label)
        remaining -= chunk_size


def _parse_tar_octal_size(header: bytes) -> int:
    field = header[124:136]
    if field[0] & 0x80:
        raise RuntimeError("release TAR member uses unsupported base-256 size metadata")
    stripped = field.rstrip(b"\0 ").lstrip(b" ")
    if not stripped:
        return 0
    if any(byte < ord("0") or byte > ord("7") for byte in stripped):
        raise RuntimeError("release TAR member has an invalid octal size")
    return int(stripped, 8)


def _tar_info_from_header(header: bytes) -> tarfile.TarInfo:
    try:
        member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
    except (tarfile.HeaderError, UnicodeError, ValueError) as error:
        raise RuntimeError(f"release TAR contains an invalid header: {error}") from error
    parsed_size = _parse_tar_octal_size(header)
    if parsed_size != member.size:
        raise RuntimeError("release TAR size metadata is ambiguous")
    return member


def _preflight_tar_gz(
    archive_file: BinaryIO,
    expected: tuple[str, ...],
    expected_sizes: Mapping[str, int] | None,
) -> None:
    archive_file.seek(0)
    try:
        with gzip.GzipFile(fileobj=archive_file, mode="rb") as decompressed:
            bounded = _BoundedDecompressedStream(
                decompressed,
                MAX_TAR_STREAM_BYTES,
            )
            expected_set = set(expected)
            seen: list[str] = []
            total = 0

            while True:
                header = _read_exact(
                    bounded,
                    tarfile.BLOCKSIZE,
                    label="release TAR header",
                )
                if header == tarfile.NUL * tarfile.BLOCKSIZE:
                    second_end_block = _read_exact(
                        bounded,
                        tarfile.BLOCKSIZE,
                        label="release TAR end marker",
                    )
                    if second_end_block != tarfile.NUL * tarfile.BLOCKSIZE:
                        raise RuntimeError(
                            "release TAR does not contain the required two-block end marker"
                        )
                    break

                member = _tar_info_from_header(header)
                if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                    raise RuntimeError(
                        "release TAR contains extended metadata or a non-ordinary "
                        f"member: {member.name}"
                    )
                if len(seen) >= MAX_MEMBERS:
                    raise RuntimeError(
                        f"release archive contains more than {MAX_MEMBERS} members"
                    )
                if member.name in seen:
                    raise RuntimeError("release archive contains duplicate members")
                if member.name not in expected_set:
                    raise RuntimeError(
                        f"release archive contains an unexpected member: {member.name}"
                    )

                total += _validate_member_metadata(
                    member.name,
                    member.size,
                    member.mode,
                    expected_sizes,
                )
                _validate_total_size(total)
                seen.append(member.name)

                padded_size = (
                    (member.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
                ) * tarfile.BLOCKSIZE
                _discard_exact(
                    bounded,
                    padded_size,
                    label=f"release TAR payload for {member.name}",
                )

            _validate_member_names(seen, expected)
            while True:
                trailing = bounded.read(COPY_BUFFER_BYTES)
                if not trailing:
                    break
                if trailing.strip(tarfile.NUL):
                    raise RuntimeError(
                        "release TAR contains non-zero data after its end marker"
                    )
    except RuntimeError:
        raise
    except (EOFError, OSError, zlib.error) as error:
        raise RuntimeError(f"release TAR cannot be decompressed safely: {error}") from error


def _extract_tar_gz(
    archive_file: BinaryIO,
    extraction_root: _ExtractionRoot,
    expected: tuple[str, ...],
    expected_sizes: Mapping[str, int] | None,
) -> None:
    _preflight_tar_gz(archive_file, expected, expected_sizes)
    archive_file.seek(0)
    with tarfile.open(fileobj=archive_file, mode="r:gz") as archive:
        expected_set = set(expected)
        seen: set[str] = set()
        ordered_members: list[tarfile.TarInfo] = []
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
            ordered_members.append(member)

        _validate_member_names(list(seen), expected)

        for member in ordered_members:
            name = member.name
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"release archive member cannot be read: {name}")
            with source:
                _copy_member(
                    source,
                    extraction_root,
                    name,
                    member.size,
                    _expected_mode(name),
                )


def _preflight_zip(
    archive_file: BinaryIO,
    archive_size: int,
    expected: tuple[str, ...],
    expected_sizes: Mapping[str, int] | None,
) -> None:
    entries = scan_classic_zip(
        archive_file,
        archive_size,
        max_entries=MAX_MEMBERS,
        max_name_bytes=MAX_MEMBER_NAME_BYTES,
        max_central_directory_bytes=MAX_ZIP_CENTRAL_DIRECTORY_BYTES,
        max_archive_comment_bytes=0,
    )
    if len(entries) != len(expected):
        raise RuntimeError(
            "release ZIP end record does not match the expected member count"
        )
    names = [entry.name for entry in entries]
    _validate_member_names(names, expected)
    total = 0
    for entry in entries:
        if entry.flag_bits & 0x1:
            raise RuntimeError("release ZIP contains an encrypted member")
        if entry.compression_method != zipfile.ZIP_DEFLATED:
            raise RuntimeError(
                "release ZIP member does not use the required DEFLATE method: "
                f"{entry.name}"
            )
        if entry.extra_size != 0 or entry.comment_size != 0:
            raise RuntimeError(
                "release ZIP contains unsupported per-member metadata"
            )
        unix_mode = entry.external_attributes >> 16
        if stat.S_IFMT(unix_mode) != stat.S_IFREG:
            raise RuntimeError(
                f"release archive member is not an ordinary file: {entry.name}"
            )
        total += _validate_member_metadata(
            entry.name,
            entry.file_size,
            unix_mode,
            expected_sizes,
        )
        _validate_total_size(total)
        expected_external_attributes = (
            (stat.S_IFREG | _expected_mode(entry.name)) & 0xFFFF
        ) << 16
        if (
            entry.creator_system != 3
            or entry.external_attributes != expected_external_attributes
        ):
            raise RuntimeError(
                f"release ZIP member metadata is not canonical: {entry.name}"
            )


def _extract_zip(
    archive_file: BinaryIO,
    archive_size: int,
    extraction_root: _ExtractionRoot,
    expected: tuple[str, ...],
    expected_sizes: Mapping[str, int] | None,
) -> None:
    _preflight_zip(archive_file, archive_size, expected, expected_sizes)
    archive_file.seek(0)
    with zipfile.ZipFile(archive_file, mode="r") as archive:
        members = archive.infolist()
        _validate_member_names([member.filename for member in members], expected)
        total = 0
        by_name: dict[str, zipfile.ZipInfo] = {}
        for member in members:
            unix_mode = member.external_attr >> 16
            file_type = stat.S_IFMT(unix_mode)
            if member.is_dir() or file_type != stat.S_IFREG:
                raise RuntimeError(
                    f"release archive member is not an ordinary file: {member.filename}"
                )
            if member.flag_bits & 0x1:
                raise RuntimeError(f"release archive member is encrypted: {member.filename}")
            if member.compress_type != zipfile.ZIP_DEFLATED:
                raise RuntimeError(
                    "release ZIP member does not use the required DEFLATE method: "
                    f"{member.filename}"
                )
            expected_external_attributes = (
                (stat.S_IFREG | _expected_mode(member.filename)) & 0xFFFF
            ) << 16
            if (
                member.create_system != 3
                or member.external_attr != expected_external_attributes
            ):
                raise RuntimeError(
                    "release ZIP member metadata is not canonical: "
                    f"{member.filename}"
                )
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
                    extraction_root,
                    name,
                    member.file_size,
                    _expected_mode(name),
                )
