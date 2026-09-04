"""Private, append-only state for a local macOS release-finalization attempt."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path, PurePath
import re
import stat
import sys
import tempfile
from typing import Any, Iterator

from release_files import (
    FilePublicationCollision,
    FilePublicationIndeterminate,
    ReleaseFileError,
    absolute_path,
    open_stable_regular_file,
    publish_sibling_no_replace,
)
from release_targets import (
    validate_commit_sha,
    validate_release_tag,
    validate_repository,
)

try:
    import fcntl
except ImportError:
    fcntl = None


STATE_SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
RECEIPT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.json$")


class ReleaseAttemptError(RuntimeError):
    """A private finalization attempt cannot safely advance."""


class ReceiptPublicationIndeterminate(ReleaseAttemptError):
    """A receipt may or may not have become visible and must be reconciled."""


@dataclass(frozen=True)
class ReleaseBinding:
    repository: str
    tag: str
    head_sha: str
    run_id: int
    run_attempt: int
    workflow_database_id: int


def validate_binding(binding: ReleaseBinding) -> None:
    """Validate every immutable local-attempt binding field."""

    try:
        validate_repository(binding.repository)
    except RuntimeError as error:
        raise ReleaseAttemptError(str(error)) from error
    try:
        validate_release_tag(binding.tag)
    except RuntimeError as error:
        raise ReleaseAttemptError(str(error)) from error
    try:
        validate_commit_sha(binding.head_sha)
    except RuntimeError as error:
        raise ReleaseAttemptError(str(error)) from error
    for name, value in (
        ("run_id", binding.run_id),
        ("run_attempt", binding.run_attempt),
        ("workflow_database_id", binding.workflow_database_id),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ReleaseAttemptError(f"release binding {name} must be a positive integer")


def require_macos_host() -> None:
    """Fail before filesystem mutation when local finalization is not on macOS."""

    if sys.platform != "darwin":
        raise ReleaseAttemptError("macOS release finalization requires a macOS host")


def _inspect_private_directory(path: Path, *, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise ReleaseAttemptError(f"cannot inspect {label} {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseAttemptError(f"{label} must be an ordinary non-symlink directory: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ReleaseAttemptError(f"{label} is not owned by the current user: {path}")
    if stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE:
        raise ReleaseAttemptError(
            f"{label} mode must be 0700, found {stat.S_IMODE(metadata.st_mode):04o}: {path}"
        )


def private_subdirectory(root: Path, relative: str) -> Path:
    """Create or validate one direct private attempt subdirectory."""

    require_macos_host()
    _inspect_private_directory(root, label="release attempt parent")
    relative_path = PurePath(relative)
    if (
        relative_path.is_absolute()
        or len(relative_path.parts) != 1
        or relative_path.parts[0] in {"", ".", ".."}
    ):
        raise ReleaseAttemptError(f"unsafe attempt subdirectory name: {relative!r}")
    path = root / relative
    try:
        path.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    except FileExistsError:
        pass
    except OSError as error:
        raise ReleaseAttemptError(f"cannot create private directory {path}: {error}") from error
    _inspect_private_directory(path, label="attempt subdirectory")
    _fsync_private_directory(
        root,
        context=f"attempt subdirectory {relative!r} creation",
    )
    return path


def _strict_json(payload: bytes, *, context: str) -> dict[str, Any]:
    if not payload or len(payload) > MAX_RECEIPT_BYTES:
        raise ReleaseAttemptError(
            f"{context} size {len(payload)} is outside the allowed range"
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ReleaseAttemptError(f"{context} repeats JSON key {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseAttemptError(f"{context} is not valid strict JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseAttemptError(f"{context} JSON root must be an object")
    return value


def _read_receipt_bytes(path: Path, *, require_single_link: bool) -> bytes:
    """Read stable bounded receipt bytes with an explicit link-count policy."""

    chunks: list[bytes] = []
    size = 0
    try:
        with open_stable_regular_file(
            path,
            label="release receipt",
            require_single_link=require_single_link,
        ) as (source, snapshot):
            if snapshot.size > MAX_RECEIPT_BYTES:
                raise ReleaseAttemptError(f"release receipt is too large: {path}")
            while True:
                chunk = source.read(min(64 * 1024, MAX_RECEIPT_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RECEIPT_BYTES:
                    raise ReleaseAttemptError(f"release receipt is too large: {path}")
    except ReleaseFileError as error:
        raise ReleaseAttemptError(f"cannot read stable release receipt {path}: {error}") from error
    return b"".join(chunks)


def _fsync_private_directory(path: Path, *, context: str) -> None:
    """Make receipt directory-entry changes durable on the required macOS host."""

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
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
            or stat.S_IMODE(opened.st_mode) != PRIVATE_DIRECTORY_MODE
        ):
            raise ReceiptPublicationIndeterminate(
                f"{context} parent is not the expected private directory: {path}"
            )
        os.fsync(descriptor)
    except ReceiptPublicationIndeterminate:
        raise
    except OSError as error:
        raise ReceiptPublicationIndeterminate(
            f"{context} directory durability is indeterminate for {path}: {error}"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _receipt_temporary_alias(destination: Path, metadata: os.stat_result) -> Path:
    """Find the sole private mkstemp alias for a linked receipt destination."""

    prefix = f".{destination.name}."
    matches: list[Path] = []
    try:
        with os.scandir(destination.parent) as entries:
            for entry in entries:
                if not entry.name.startswith(prefix) or not entry.name.endswith(".tmp"):
                    continue
                candidate = entry.stat(follow_symlinks=False)
                if (candidate.st_dev, candidate.st_ino) == (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    matches.append(destination.parent / entry.name)
    except OSError as error:
        raise ReceiptPublicationIndeterminate(
            f"cannot inspect temporary aliases for receipt {destination}: {error}"
        ) from error
    if len(matches) != 1:
        raise ReleaseAttemptError(
            f"linked release receipt does not have one recoverable temporary alias: "
            f"{destination}"
        )
    return matches[0]


def _reconcile_receipt_temporary_alias(
    destination: Path,
    metadata: os.stat_result,
) -> dict[str, Any]:
    """Remove one proven post-link temporary alias and re-read the receipt."""

    _inspect_private_directory(destination.parent, label="release receipt parent")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 2
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE
    ):
        raise ReleaseAttemptError(
            f"release receipt is not a recoverable two-link private file: {destination}"
        )
    temporary = _receipt_temporary_alias(destination, metadata)
    before = _read_receipt_bytes(destination, require_single_link=False)
    value = _strict_json(before, context=f"release receipt {destination.name}")
    try:
        alias = os.lstat(temporary)
        current = os.lstat(destination)
        if (
            (alias.st_dev, alias.st_ino) != (metadata.st_dev, metadata.st_ino)
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
            or alias.st_nlink != 2
            or current.st_nlink != 2
        ):
            raise ReceiptPublicationIndeterminate(
                f"release receipt alias changed during reconciliation: {destination}"
            )
        os.unlink(temporary)
    except ReceiptPublicationIndeterminate:
        raise
    except OSError as error:
        raise ReceiptPublicationIndeterminate(
            f"could not remove temporary alias for receipt {destination}: {error}"
        ) from error
    _fsync_private_directory(
        destination.parent,
        context="release receipt alias reconciliation",
    )
    after = _read_receipt_bytes(destination, require_single_link=True)
    if after != before:
        raise ReceiptPublicationIndeterminate(
            f"release receipt changed during alias reconciliation: {destination}"
        )
    return value


def read_receipt(path: Path) -> dict[str, Any]:
    """Read one stable private JSON receipt, reconciling a proven link alias."""

    require_macos_host()
    destination = absolute_path(path)
    try:
        metadata = os.lstat(destination)
    except OSError as error:
        raise ReleaseAttemptError(
            f"cannot inspect release receipt {destination}: {error}"
        ) from error
    if (
        RECEIPT_NAME.fullmatch(destination.name) is not None
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 2
    ):
        return _reconcile_receipt_temporary_alias(destination, metadata)
    payload = _read_receipt_bytes(destination, require_single_link=True)
    return _strict_json(payload, context=f"release receipt {destination.name}")


def write_receipt_no_replace(root: Path, name: str, value: dict[str, Any]) -> Path:
    """Durably publish one append-only JSON receipt without replacement."""

    require_macos_host()
    _inspect_private_directory(root, label="release receipt parent")
    if PurePath(name).name != name or RECEIPT_NAME.fullmatch(name) is None:
        raise ReleaseAttemptError(f"unsafe release receipt name: {name!r}")
    destination = root / name
    try:
        payload = (
            json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ReleaseAttemptError(f"release receipt is not JSON serializable: {error}") from error
    if len(payload) > MAX_RECEIPT_BYTES:
        raise ReleaseAttemptError("release receipt exceeds the size limit")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=root,
        prefix=f".{name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    preserve_temporary = False
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            os.fchmod(output.fileno(), PRIVATE_FILE_MODE)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            publish_sibling_no_replace(
                temporary,
                destination,
                label="release receipt",
            )
        except FilePublicationIndeterminate as error:
            preserve_temporary = True
            raise ReceiptPublicationIndeterminate(str(error)) from error
        try:
            _fsync_private_directory(
                root,
                context="release receipt publication",
            )
            os.unlink(temporary)
        except ReceiptPublicationIndeterminate:
            preserve_temporary = True
            raise
        except OSError as error:
            preserve_temporary = True
            raise ReceiptPublicationIndeterminate(
                f"release receipt was linked but temporary-alias cleanup is "
                f"indeterminate for {destination}: {error}"
            ) from error
        _fsync_private_directory(
            root,
            context="release receipt temporary-alias cleanup",
        )
        try:
            if _read_receipt_bytes(destination, require_single_link=True) != payload:
                raise ReceiptPublicationIndeterminate(
                    f"published release receipt bytes changed: {destination}"
                )
        except ReleaseAttemptError as error:
            raise ReceiptPublicationIndeterminate(
                f"could not prove the final release receipt: {destination}"
            ) from error
    except (FileExistsError, FilePublicationCollision) as error:
        raise ReleaseAttemptError(
            f"refusing to replace existing release receipt: {destination}"
        ) from error
    except ReleaseFileError as error:
        raise ReleaseAttemptError(f"cannot publish release receipt: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not preserve_temporary:
            temporary.unlink(missing_ok=True)
    return destination


def _binding_payload(binding: ReleaseBinding) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "binding": asdict(binding),
    }


def _parse_binding(value: dict[str, Any]) -> ReleaseBinding:
    if set(value) != {"schema_version", "binding"}:
        raise ReleaseAttemptError("release binding receipt has unexpected fields")
    if value["schema_version"] != STATE_SCHEMA_VERSION:
        raise ReleaseAttemptError("release binding receipt schema version is unsupported")
    raw = value["binding"]
    if not isinstance(raw, dict) or set(raw) != {
        "repository",
        "tag",
        "head_sha",
        "run_id",
        "run_attempt",
        "workflow_database_id",
    }:
        raise ReleaseAttemptError("release binding receipt payload is malformed")
    binding = ReleaseBinding(
        repository=raw["repository"],
        tag=raw["tag"],
        head_sha=raw["head_sha"],
        run_id=raw["run_id"],
        run_attempt=raw["run_attempt"],
        workflow_database_id=raw["workflow_database_id"],
    )
    validate_binding(binding)
    return binding


def initialize_or_resume(root: Path | str, binding: ReleaseBinding) -> Path:
    """Create a private attempt or prove an existing attempt has the same binding."""

    require_macos_host()
    validate_binding(binding)
    path = absolute_path(root)
    _inspect_private_directory(path.parent, label="release attempt container")
    created = False
    try:
        path.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        created = True
    except FileExistsError:
        pass
    except OSError as error:
        raise ReleaseAttemptError(f"cannot create release attempt {path}: {error}") from error
    _inspect_private_directory(path, label="release attempt root")
    _fsync_private_directory(
        path.parent,
        context="release attempt root creation",
    )
    binding_path = path / "binding.json"
    if created:
        write_receipt_no_replace(path, binding_path.name, _binding_payload(binding))
    observed = _parse_binding(read_receipt(binding_path))
    if observed != binding:
        raise ReleaseAttemptError(
            f"release attempt is bound to {observed!r}, not {binding!r}"
        )
    return path


@contextmanager
def exclusive_attempt(root: Path) -> Iterator[None]:
    """Hold the process lock for one private release attempt."""

    require_macos_host()
    _inspect_private_directory(root, label="release attempt root")
    if fcntl is None:
        raise ReleaseAttemptError("release attempt locking requires a Unix host")
    lock_path = root / "attempt.lock"
    existing: os.stat_result | None
    try:
        existing = os.lstat(lock_path)
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise ReleaseAttemptError(f"cannot inspect release attempt lock: {error}") from error
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_nlink != 1
        or (hasattr(os, "getuid") and existing.st_uid != os.getuid())
        or stat.S_IMODE(existing.st_mode) != PRIVATE_FILE_MODE
    ):
        raise ReleaseAttemptError("existing release attempt lock is not a private owned file")

    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, PRIVATE_FILE_MODE)
    except OSError as error:
        raise ReleaseAttemptError(f"cannot open release attempt lock: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise ReleaseAttemptError("release attempt lock is not a single-link regular file")
        current = os.lstat(lock_path)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ReleaseAttemptError("release attempt lock path changed while opening")
        if existing is not None and (existing.st_dev, existing.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise ReleaseAttemptError("release attempt lock was replaced while opening")
        if existing is None:
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseAttemptError("another finalizer holds the release attempt lock") from error
        yield
    finally:
        os.close(descriptor)
