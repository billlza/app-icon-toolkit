"""Stable-file and no-replace publication primitives for release tooling."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import BinaryIO, Iterator


COPY_BUFFER_BYTES = 1024 * 1024


class ReleaseFileError(RuntimeError):
    """A release file did not satisfy the stable-file contract."""


class FilePublicationCollision(ReleaseFileError):
    """The final path already names a different filesystem object."""


class FilePublicationIndeterminate(ReleaseFileError):
    """A no-replace publication result could not be proven either way."""


@dataclass(frozen=True)
class FileSnapshot:
    """Identity and mutable metadata for one open or named regular file."""

    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    link_count: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> FileSnapshot:
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            changed_ns=metadata.st_ctime_ns,
            link_count=metadata.st_nlink,
        )

    def same_identity_size_mtime(self, other: FileSnapshot) -> bool:
        """Compare fields that a successful hard-link operation cannot change."""

        return (
            self.device,
            self.inode,
            self.size,
            self.modified_ns,
        ) == (
            other.device,
            other.inode,
            other.size,
            other.modified_ns,
        )


def absolute_path(path: Path | str) -> Path:
    """Make a path absolute without following its final component."""

    return Path(os.path.abspath(os.fspath(path)))


def inspect_regular_file(
    path: Path | str,
    *,
    label: str,
    require_single_link: bool,
    require_nonempty: bool = True,
) -> FileSnapshot:
    """Inspect a named regular non-symlink file without opening it."""

    absolute = absolute_path(path)
    try:
        metadata = os.lstat(absolute)
    except OSError as error:
        raise ReleaseFileError(f"failed to inspect {label} {absolute}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ReleaseFileError(
            f"{label} must be an ordinary non-symlink file: {absolute}"
        )
    if require_single_link and metadata.st_nlink != 1:
        raise ReleaseFileError(
            f"{label} must have exactly one hard link, found "
            f"{metadata.st_nlink}: {absolute}"
        )
    if require_nonempty and metadata.st_size <= 0:
        raise ReleaseFileError(f"{label} must be non-empty: {absolute}")
    return FileSnapshot.from_stat(metadata)


def _open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


@contextmanager
def open_stable_regular_file(
    path: Path | str,
    *,
    label: str,
    require_single_link: bool,
    require_nonempty: bool = True,
) -> Iterator[tuple[BinaryIO, FileSnapshot]]:
    """Open once and reject path swaps or metadata changes around consumption."""

    absolute = absolute_path(path)
    named_before = inspect_regular_file(
        absolute,
        label=label,
        require_single_link=require_single_link,
        require_nonempty=require_nonempty,
    )
    try:
        descriptor = os.open(absolute, _open_flags())
    except OSError as error:
        raise ReleaseFileError(f"failed to open {label} {absolute}: {error}") from error

    try:
        opened = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    try:
        opened_snapshot = FileSnapshot.from_stat(os.fstat(opened.fileno()))
        if opened_snapshot != named_before:
            raise ReleaseFileError(f"{label} changed while it was being opened: {absolute}")
    except BaseException:
        opened.close()
        raise

    consumer_error: BaseException | None = None
    consumer_traceback = None
    stability_issues: list[tuple[str, BaseException | None]] = []
    try:
        try:
            yield opened, opened_snapshot
        except BaseException as error:
            consumer_error = error
            consumer_traceback = error.__traceback__

        try:
            opened_after = FileSnapshot.from_stat(os.fstat(opened.fileno()))
            if opened_after != opened_snapshot:
                stability_issues.append(
                    (f"{label} changed while it was being read: {absolute}", None)
                )
        except BaseException as error:
            stability_issues.append(
                (f"failed to re-inspect open {label} {absolute}: {error}", error)
            )
    finally:
        try:
            opened.close()
        except BaseException as error:
            stability_issues.append(
                (f"failed to close open {label} {absolute}: {error}", error)
            )

    try:
        named_after = inspect_regular_file(
            absolute,
            label=label,
            require_single_link=require_single_link,
            require_nonempty=require_nonempty,
        )
        if named_after != opened_snapshot:
            stability_issues.append(
                (f"{label} path changed while it was being read: {absolute}", None)
            )
    except BaseException as error:
        stability_issues.append(
            (f"failed to re-inspect named {label} {absolute}: {error}", error)
        )

    if stability_issues:
        messages = "; ".join(message for message, _error in stability_issues)
        stability_error = ReleaseFileError(messages)
        if consumer_error is not None:
            raise stability_error from consumer_error
        first_cause = next(
            (error for _message, error in stability_issues if error is not None),
            None,
        )
        if first_cause is not None:
            raise stability_error from first_cause
        raise stability_error
    if consumer_error is not None:
        raise consumer_error.with_traceback(consumer_traceback)


def sha256_file(
    path: Path | str,
    *,
    label: str = "release file",
    require_single_link: bool = True,
    chunk_size: int = COPY_BUFFER_BYTES,
) -> str:
    """Hash stable file bytes while retaining a single open descriptor."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    read_size = 0
    with open_stable_regular_file(
        path,
        label=label,
        require_single_link=require_single_link,
    ) as (source, snapshot):
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            read_size += len(chunk)
            if read_size > snapshot.size:
                raise ReleaseFileError(f"{label} grew while it was being hashed: {path}")
            digest.update(chunk)
        if read_size != snapshot.size:
            raise ReleaseFileError(f"{label} was truncated while it was being hashed: {path}")
    return digest.hexdigest()


def copy_regular_file(
    source: Path | str,
    destination: Path | str,
    *,
    mode: int,
    label: str,
    require_single_link: bool = True,
    expected_source: FileSnapshot | None = None,
    maximum_bytes: int | None = None,
) -> FileSnapshot:
    """Copy a stable, optionally pre-authorized source to a new destination."""

    if mode not in {0o600, 0o644, 0o700, 0o755}:
        raise ValueError(f"unsupported release file mode: {mode:o}")
    if maximum_bytes is not None and maximum_bytes < 0:
        raise ValueError("maximum_bytes cannot be negative")
    destination_path = absolute_path(destination)
    created = False
    copied = 0
    try:
        with open_stable_regular_file(
            source,
            label=label,
            require_single_link=require_single_link,
        ) as (input_file, source_snapshot):
            if expected_source is not None and source_snapshot != expected_source:
                raise ReleaseFileError(
                    f"{label} changed after it was authorized for copying: {source}"
                )
            if maximum_bytes is not None and source_snapshot.size > maximum_bytes:
                raise ReleaseFileError(
                    f"{label} exceeds its {maximum_bytes}-byte copy limit: {source}"
                )
            with destination_path.open("xb") as output_file:
                created = True
                while True:
                    chunk = input_file.read(COPY_BUFFER_BYTES)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > source_snapshot.size or (
                        maximum_bytes is not None and copied > maximum_bytes
                    ):
                        raise ReleaseFileError(
                            f"{label} grew beyond its authorized copy size: {source}"
                        )
                    output_file.write(chunk)
                if copied != source_snapshot.size:
                    raise ReleaseFileError(
                        f"{label} was truncated while it was being copied: {source}"
                    )
                if hasattr(os, "fchmod"):
                    os.fchmod(output_file.fileno(), mode)
                output_file.flush()
                os.fsync(output_file.fileno())
        if not hasattr(os, "fchmod"):
            destination_path.chmod(mode)
        return inspect_regular_file(
            destination_path,
            label=f"copied {label}",
            require_single_link=True,
        )
    except BaseException:
        if created:
            destination_path.unlink(missing_ok=True)
        raise


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise FilePublicationIndeterminate(
            f"could not inspect publication path {path}: {error}"
        ) from error


def _snapshot_optional(metadata: os.stat_result | None) -> FileSnapshot | None:
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        return None
    return FileSnapshot.from_stat(metadata)


def publish_sibling_no_replace(
    temporary: Path | str,
    destination: Path | str,
    *,
    label: str,
) -> None:
    """Hard-link a complete sibling file and reconcile every syscall result."""

    temporary_path = absolute_path(temporary)
    destination_path = absolute_path(destination)
    try:
        same_parent = os.path.samefile(temporary_path.parent, destination_path.parent)
    except OSError as error:
        raise ReleaseFileError(
            f"cannot compare {label} publication parents: {error}"
        ) from error
    if not same_parent:
        raise ReleaseFileError(f"{label} temporary and final paths must be siblings")
    source = inspect_regular_file(
        temporary_path,
        label=f"temporary {label}",
        require_single_link=True,
    )
    source_digest = sha256_file(
        temporary_path,
        label=f"temporary {label}",
        require_single_link=True,
    )
    if (
        inspect_regular_file(
            temporary_path,
            label=f"temporary {label}",
            require_single_link=True,
        )
        != source
    ):
        raise ReleaseFileError(f"temporary {label} changed before publication")

    link_error: OSError | None = None
    try:
        os.link(temporary_path, destination_path, follow_symlinks=False)
    except OSError as error:
        link_error = error

    try:
        source_after = _snapshot_optional(_lstat_optional(temporary_path))
        destination_after = _snapshot_optional(_lstat_optional(destination_path))
        source_is_unchanged = source_after == source
        linked_object_is_plausible = (
            source_after is not None
            and destination_after is not None
            and source_after == destination_after
            and source_after.link_count == 2
            and source_after.same_identity_size_mtime(source)
        )

        if linked_object_is_plausible:
            digest_after = sha256_file(
                temporary_path,
                label=f"published {label}",
                require_single_link=False,
            )
            source_final = _snapshot_optional(_lstat_optional(temporary_path))
            destination_final = _snapshot_optional(_lstat_optional(destination_path))
            if (
                digest_after == source_digest
                and source_final is not None
                and source_final == destination_final
                and source_final == source_after
            ):
                return
        if isinstance(link_error, FileExistsError) and source_is_unchanged:
            raise FilePublicationCollision(
                f"refusing to replace existing {label}: {destination_path}"
            ) from link_error
    except (FilePublicationCollision, FilePublicationIndeterminate):
        raise
    except (OSError, ReleaseFileError) as error:
        raise FilePublicationIndeterminate(
            f"could not reconcile {label} publication for {destination_path}; "
            f"preserve temporary file {temporary_path}: {error}"
        ) from error

    detail = "no syscall diagnostic" if link_error is None else str(link_error)
    raise FilePublicationIndeterminate(
        f"{label} publication outcome is indeterminate for {destination_path}; "
        f"preserve and reconcile temporary file {temporary_path}: {detail}"
    ) from link_error
