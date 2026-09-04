"""Bounded, digest-bound retrieval of GitHub Actions release artifacts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import threading
from typing import BinaryIO, NoReturn, Protocol
import uuid
import zipfile
import zlib

from release_artifacts import ArtifactRecord, MAX_ARTIFACT_ARCHIVE_BYTES
from release_files import (
    ReleaseFileError,
    inspect_regular_file,
    open_stable_regular_file,
    publish_sibling_no_replace,
    sha256_file,
)
from release_package import MAX_ARCHIVE_BYTES
from release_targets import validate_repository
from release_zip_preflight import (
    ZipCentralDirectoryEntry,
    ZipPreflightError,
    scan_classic_zip,
)


MAX_COMMAND_STDERR_BYTES = 4 * 1024 * 1024
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
COPY_BUFFER_BYTES = 1024 * 1024
_ZIP_MAX_COMMENT_BYTES = 65_535
_ZIP_MAX_MEMBER_NAME_BYTES = 255
_ZIP_MAX_CENTRAL_DIRECTORY_BYTES = 256 * 1024
_PARTIAL_SUFFIX = ".partial"


class ArtifactDownloadError(RuntimeError):
    """An Actions artifact cannot be proven to match the release binding."""


class ArtifactZipDownloader(Protocol):
    """Injected read-only boundary for retrieving one raw Actions artifact ZIP."""

    def download(
        self,
        repository: str,
        artifact_id: int,
        destination: Path,
        *,
        expected_size: int,
        label: str,
    ) -> None:
        """Download exactly one numeric artifact into a new temporary path."""


def _require_private_directory(directory: Path, *, label: str) -> None:
    try:
        metadata = os.lstat(directory)
    except OSError as error:
        raise ArtifactDownloadError(f"cannot inspect {label} {directory}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactDownloadError(
            f"{label} must be an ordinary non-symlink directory: {directory}"
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ArtifactDownloadError(f"{label} is not owned by the current user: {directory}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ArtifactDownloadError(
            f"{label} mode must be 0700: {directory}"
        )


def _fsync_private_directory(directory: Path, *, label: str) -> None:
    descriptor = -1
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(directory, flags)
        opened = os.fstat(descriptor)
        named = os.lstat(directory)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise ArtifactDownloadError(
                f"{label} parent is not the expected private directory"
            )
        os.fsync(descriptor)
    except ArtifactDownloadError:
        raise
    except OSError as error:
        raise ArtifactDownloadError(
            f"{label} directory durability is indeterminate: {error}"
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as error:
                raise ArtifactDownloadError(
                    f"cannot close {label} directory descriptor: {error}"
                ) from error


def _resolve_gh_executable(executable: Path | None) -> Path:
    candidate = shutil.which("gh") if executable is None else os.fspath(executable)
    if candidate is None or not Path(candidate).is_absolute():
        raise ArtifactDownloadError("gh must resolve to an absolute executable path")
    try:
        resolved = Path(candidate).resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError) as error:
        raise ArtifactDownloadError(f"cannot resolve gh executable: {error}") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise ArtifactDownloadError(f"gh is not an executable regular file: {resolved}")
    return resolved


def _drain_bounded_stderr(
    stream: BinaryIO,
    buffer: bytearray,
    state: dict[str, object],
) -> None:
    total = 0
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if len(buffer) <= MAX_COMMAND_STDERR_BYTES:
                remaining = MAX_COMMAND_STDERR_BYTES + 1 - len(buffer)
                buffer.extend(chunk[:remaining])
    except OSError as error:
        state["error"] = error
    finally:
        state["total"] = total
        try:
            stream.close()
        except OSError as error:
            state.setdefault("error", error)


def download_command_to_file(
    command: tuple[str, ...],
    destination: Path,
    *,
    expected_size: int,
    label: str,
    timeout_seconds: int,
) -> None:
    """Write command stdout directly to one size-limited private binary file."""

    if os.name != "posix":
        raise ArtifactDownloadError(
            "artifact downloads require POSIX file-size limits"
        )
    if expected_size <= 0 or expected_size > MAX_ARTIFACT_ARCHIVE_BYTES:
        raise ArtifactDownloadError(f"{label} has an invalid expected download size")
    if timeout_seconds <= 0:
        raise ArtifactDownloadError("artifact download timeout must be positive")
    if os.path.lexists(destination):
        raise ArtifactDownloadError(
            f"refusing to replace existing temporary {label}: {destination}"
        )

    try:
        import resource
    except ImportError as error:  # pragma: no cover - finalizer is macOS-only
        raise ArtifactDownloadError(
            "artifact downloads require POSIX file-size limits"
        ) from error

    def limit_stdout_file() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (expected_size, expected_size))

    diagnostics = bytearray()
    stderr_state: dict[str, object] = {}
    process: subprocess.Popen[bytes] | None = None
    reader: threading.Thread | None = None
    timed_out = False
    environment = os.environ.copy()
    environment.pop("GH_DEBUG", None)
    environment["GH_PROMPT_DISABLED"] = "1"
    environment["NO_COLOR"] = "1"
    try:
        try:
            with destination.open("xb") as output:
                if hasattr(os, "fchmod"):
                    os.fchmod(output.fileno(), 0o600)
                try:
                    process = subprocess.Popen(
                        command,
                        cwd="/",
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.PIPE,
                        close_fds=True,
                        preexec_fn=limit_stdout_file,
                    )
                except (OSError, subprocess.SubprocessError) as error:
                    raise ArtifactDownloadError(
                        f"failed to execute artifact download for {label}: {error}"
                    ) from error
                if process.stderr is None:  # pragma: no cover - Popen contract guard
                    process.kill()
                    process.wait()
                    raise ArtifactDownloadError(
                        f"artifact download has no diagnostic pipe: {label}"
                    )
                reader = threading.Thread(
                    target=_drain_bounded_stderr,
                    args=(process.stderr, diagnostics, stderr_state),
                    name="artifact-download-stderr",
                    daemon=True,
                )
                reader.start()
                try:
                    process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.kill()
                    process.wait()
                reader.join(timeout=10)
                output.flush()
                os.fsync(output.fileno())
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if reader is not None and reader.is_alive():
                reader.join(timeout=1)
    except ArtifactDownloadError:
        raise
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        raise ArtifactDownloadError(
            f"local artifact download I/O failed for {label}: {error}"
        ) from error

    if reader is not None and reader.is_alive():
        raise ArtifactDownloadError(
            f"artifact download diagnostic reader did not stop: {label}"
        )
    if "error" in stderr_state:
        raise ArtifactDownloadError(
            f"artifact download diagnostic read failed for {label}: "
            f"{stderr_state['error']}"
        )
    stderr_total = stderr_state.get("total", 0)
    if not isinstance(stderr_total, int) or stderr_total > MAX_COMMAND_STDERR_BYTES:
        raise ArtifactDownloadError(
            f"artifact download diagnostics exceeded the limit: {label}"
        )
    try:
        diagnostic = diagnostics.decode("utf-8", errors="strict").strip()
    except UnicodeError as error:
        raise ArtifactDownloadError(
            f"artifact download diagnostics are not valid UTF-8: {label}"
        ) from error
    if timed_out:
        raise ArtifactDownloadError(f"artifact download timed out: {label}")
    if process is None or process.returncode != 0:
        returncode = "unknown" if process is None else str(process.returncode)
        raise ArtifactDownloadError(
            f"artifact download failed with exit {returncode} for {label}: "
            f"{diagnostic or 'no diagnostic'}"
        )
    if diagnostic:
        raise ArtifactDownloadError(
            f"artifact download wrote unexpected stderr for {label}: "
            f"{diagnostic[:1000]}"
        )
    try:
        downloaded = inspect_regular_file(
            destination,
            label=f"temporary {label}",
            require_single_link=True,
        )
    except ReleaseFileError as error:
        raise ArtifactDownloadError(
            f"downloaded artifact is invalid for {label}: {error}"
        ) from error
    if downloaded.size != expected_size:
        raise ArtifactDownloadError(
            f"artifact download size differs for {label}: "
            f"found {downloaded.size}, expected {expected_size}"
        )


class GitHubArtifactZipDownloader:
    """Shell-free, bounded GitHub CLI adapter for numeric artifact downloads."""

    def __init__(
        self,
        *,
        executable: Path | None = None,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ArtifactDownloadError("artifact download timeout must be positive")
        self.executable = _resolve_gh_executable(executable)
        self.timeout_seconds = timeout_seconds

    def download(
        self,
        repository: str,
        artifact_id: int,
        destination: Path,
        *,
        expected_size: int,
        label: str,
    ) -> None:
        try:
            validated_repository = validate_repository(repository)
        except RuntimeError as error:
            raise ArtifactDownloadError(str(error)) from error
        if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0:
            raise ArtifactDownloadError("artifact ID must be a positive integer")
        download_command_to_file(
            (
                str(self.executable),
                "api",
                "--hostname",
                "github.com",
                f"repos/{validated_repository}/actions/artifacts/{artifact_id}/zip",
            ),
            destination,
            expected_size=expected_size,
            label=label,
            timeout_seconds=self.timeout_seconds,
        )


def _random_temporary_path(directory: Path, *, prefix: str) -> Path:
    for _ in range(16):
        candidate = directory / f"{prefix}{uuid.uuid4().hex}{_PARTIAL_SUFFIX}"
        if not os.path.lexists(candidate):
            return candidate
    raise ArtifactDownloadError("cannot allocate a unique artifact temporary path")


def _matching_temporary_paths(directory: Path, *, prefix: str) -> tuple[Path, ...]:
    matches: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if not name.startswith(prefix) or not name.endswith(_PARTIAL_SUFFIX):
                    continue
                token = name[len(prefix) : -len(_PARTIAL_SUFFIX)]
                if len(token) == 32 and all(character in "0123456789abcdef" for character in token):
                    matches.append(directory / name)
    except OSError as error:
        raise ArtifactDownloadError(
            f"cannot inspect artifact temporary files: {error}"
        ) from error
    return tuple(sorted(matches))


def _remove_known_temporary(path: Path, *, label: str) -> None:
    if not os.path.lexists(path):
        return
    try:
        inspect_regular_file(
            path,
            label=f"failed temporary {label}",
            require_single_link=True,
            require_nonempty=False,
        )
        path.unlink()
    except (OSError, ReleaseFileError) as error:
        raise ArtifactDownloadError(
            f"cannot remove known failed temporary {label}: {error}"
        ) from error
    _fsync_private_directory(path.parent, label=f"failed temporary {label}")


def _cleanup_and_reraise(
    temporary: Path,
    *,
    label: str,
    cause: ArtifactDownloadError,
) -> NoReturn:
    try:
        _remove_known_temporary(temporary, label=label)
    except ArtifactDownloadError as cleanup_error:
        raise ArtifactDownloadError(
            f"{cause}; additionally, cleanup failed: {cleanup_error}"
        ) from cleanup_error
    raise cause


def _validated_archive_name(expected_name: str) -> str:
    expected_path = PurePosixPath(expected_name)
    try:
        encoded_name = expected_name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ArtifactDownloadError(
            f"invalid expected public archive name: {expected_name!r}"
        ) from error
    if (
        not expected_name
        or "\0" in expected_name
        or "\\" in expected_name
        or len(encoded_name) > _ZIP_MAX_MEMBER_NAME_BYTES
        or expected_path.name != expected_name
        or expected_path.is_absolute()
        or any(part in {"", ".", ".."} for part in expected_path.parts)
    ):
        raise ArtifactDownloadError(
            f"invalid expected public archive name: {expected_name!r}"
        )
    return expected_name


def _reconcile_published_alias(
    temporary: Path,
    destination: Path,
    *,
    label: str,
) -> None:
    try:
        temporary_snapshot = inspect_regular_file(
            temporary,
            label=f"temporary {label}",
            require_single_link=False,
        )
        destination_snapshot = inspect_regular_file(
            destination,
            label=f"published {label}",
            require_single_link=False,
        )
        if (
            temporary_snapshot.device != destination_snapshot.device
            or temporary_snapshot.inode != destination_snapshot.inode
            or temporary_snapshot.link_count != 2
            or destination_snapshot.link_count != 2
        ):
            raise ArtifactDownloadError(
                f"temporary and published {label} are distinct or unexpectedly linked"
            )
        temporary.unlink()
    except ArtifactDownloadError:
        raise
    except (OSError, ReleaseFileError) as error:
        raise ArtifactDownloadError(
            f"cannot reconcile {label} publication: {error}"
        ) from error
    _fsync_private_directory(destination.parent, label=f"published {label}")


def _recover_destination_alias(
    destination: Path,
    temporaries: tuple[Path, ...],
    *,
    label: str,
) -> tuple[Path, ...]:
    if not os.path.lexists(destination):
        return temporaries
    try:
        destination_snapshot = inspect_regular_file(
            destination,
            label=f"published {label}",
            require_single_link=False,
        )
    except ReleaseFileError as error:
        raise ArtifactDownloadError(f"published {label} is invalid: {error}") from error
    if destination_snapshot.link_count == 1:
        return temporaries
    if destination_snapshot.link_count != 2:
        raise ArtifactDownloadError(
            f"published {label} has an unrecoverable hard-link count"
        )

    aliases: list[Path] = []
    for temporary in temporaries:
        try:
            metadata = os.lstat(temporary)
        except OSError as error:
            raise ArtifactDownloadError(
                f"cannot inspect temporary alias for {label}: {error}"
            ) from error
        if (
            stat.S_ISREG(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino)
            == (destination_snapshot.device, destination_snapshot.inode)
        ):
            aliases.append(temporary)
    if len(aliases) != 1:
        raise ArtifactDownloadError(
            f"published {label} does not have one recoverable temporary alias"
        )
    _reconcile_published_alias(aliases[0], destination, label=label)
    return tuple(path for path in temporaries if path != aliases[0])


def _publish_temporary(
    temporary: Path,
    destination: Path,
    *,
    label: str,
) -> None:
    try:
        publish_sibling_no_replace(temporary, destination, label=label)
    except ReleaseFileError as error:
        raise ArtifactDownloadError(f"cannot publish {label}: {error}") from error
    _reconcile_published_alias(temporary, destination, label=label)
    try:
        inspect_regular_file(
            destination,
            label=f"published {label}",
            require_single_link=True,
        )
    except ReleaseFileError as error:
        raise ArtifactDownloadError(
            f"cannot verify published {label}: {error}"
        ) from error


def _verify_artifact_zip(path: Path, record: ArtifactRecord) -> None:
    try:
        snapshot = inspect_regular_file(
            path,
            label=f"GitHub artifact {record.name}",
            require_single_link=True,
        )
        digest = sha256_file(
            path,
            label=f"GitHub artifact {record.name}",
            require_single_link=True,
        )
    except ReleaseFileError as error:
        raise ArtifactDownloadError(f"GitHub artifact cache is invalid: {error}") from error
    if snapshot.size != record.size_in_bytes:
        raise ArtifactDownloadError(
            f"GitHub artifact size differs from API metadata: {record.name}"
        )
    if digest != record.archive_sha256:
        raise ArtifactDownloadError(
            f"GitHub artifact SHA-256 differs from API metadata: {record.name}"
        )


def obtain_artifact_zip(
    repository: str,
    record: ArtifactRecord,
    cache_directory: Path,
    downloader: ArtifactZipDownloader,
) -> Path:
    """Return one cached raw artifact ZIP proven by numeric ID and API digest."""

    try:
        validated_repository = validate_repository(repository)
    except RuntimeError as error:
        raise ArtifactDownloadError(str(error)) from error
    if (
        isinstance(record.artifact_id, bool)
        or not isinstance(record.artifact_id, int)
        or record.artifact_id <= 0
    ):
        raise ArtifactDownloadError("artifact ID must be a positive integer")
    _require_private_directory(cache_directory, label="artifact cache")
    destination = cache_directory / f"artifact-{record.artifact_id}.zip"
    temporary_prefix = f".artifact-{record.artifact_id}."
    temporaries = _matching_temporary_paths(
        cache_directory,
        prefix=temporary_prefix,
    )
    temporaries = _recover_destination_alias(
        destination,
        temporaries,
        label=f"GitHub artifact {record.name}",
    )
    if os.path.lexists(destination):
        _verify_artifact_zip(destination, record)
        for stale in temporaries:
            _remove_known_temporary(stale, label=f"GitHub artifact {record.name}")
        return destination

    selected: Path | None = None
    for stale in temporaries:
        try:
            _verify_artifact_zip(stale, record)
        except ArtifactDownloadError:
            _remove_known_temporary(stale, label=f"GitHub artifact {record.name}")
            continue
        if selected is None:
            selected = stale
        else:
            _remove_known_temporary(stale, label=f"GitHub artifact {record.name}")

    if selected is None:
        selected = _random_temporary_path(
            cache_directory,
            prefix=temporary_prefix,
        )
        try:
            downloader.download(
                validated_repository,
                record.artifact_id,
                selected,
                expected_size=record.size_in_bytes,
                label=f"GitHub artifact {record.name}",
            )
            _verify_artifact_zip(selected, record)
        except ArtifactDownloadError as error:
            _cleanup_and_reraise(
                selected,
                label=f"GitHub artifact {record.name}",
                cause=error,
            )

    _publish_temporary(
        selected,
        destination,
        label=f"GitHub artifact {record.name}",
    )
    for stale in _matching_temporary_paths(
        cache_directory,
        prefix=temporary_prefix,
    ):
        _remove_known_temporary(stale, label=f"GitHub artifact {record.name}")
    _verify_artifact_zip(destination, record)
    return destination


def _preflight_single_member_zip(
    opened_zip: BinaryIO,
    archive_size: int,
    expected_name: str,
) -> ZipCentralDirectoryEntry:
    try:
        entries = scan_classic_zip(
            opened_zip,
            archive_size,
            max_entries=1,
            max_name_bytes=_ZIP_MAX_MEMBER_NAME_BYTES,
            max_central_directory_bytes=_ZIP_MAX_CENTRAL_DIRECTORY_BYTES,
            max_archive_comment_bytes=_ZIP_MAX_COMMENT_BYTES,
        )
    except ZipPreflightError as error:
        raise ArtifactDownloadError(
            "GitHub artifact ZIP must have one non-ZIP64, single-disk member "
            f"with bounded metadata: {error}"
        ) from error
    if len(entries) != 1 or entries[0].name != expected_name:
        names = [entry.name for entry in entries]
        raise ArtifactDownloadError(
            "GitHub artifact ZIP must contain exactly the expected public "
            f"archive {expected_name!r}; found {names!r}"
        )
    if entries[0].compression_method not in {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
    }:
        raise ArtifactDownloadError(
            "GitHub artifact ZIP member uses an unsupported compression method"
        )
    opened_zip.seek(0)
    return entries[0]


def _consume_artifact_member(
    artifact_zip: Path,
    expected_name: str,
    *,
    output_path: Path | None,
) -> tuple[int, str]:
    expected_name = _validated_archive_name(expected_name)
    digest = hashlib.sha256()
    copied = 0
    try:
        with open_stable_regular_file(
            artifact_zip,
            label="downloaded GitHub artifact ZIP",
            require_single_link=True,
        ) as (opened_zip, artifact_snapshot):
            preflight_entry = _preflight_single_member_zip(
                opened_zip,
                artifact_snapshot.size,
                expected_name,
            )
            with zipfile.ZipFile(opened_zip, mode="r") as archive:
                members = archive.infolist()
                if len(members) != 1 or members[0].filename != expected_name:
                    names = [member.filename for member in members]
                    raise ArtifactDownloadError(
                        "GitHub artifact ZIP must contain exactly the expected public "
                        f"archive {expected_name!r}; found {names!r}"
                    )
                member = members[0]
                if (
                    member.create_system != preflight_entry.creator_system
                    or member.flag_bits != preflight_entry.flag_bits
                    or member.compress_type != preflight_entry.compression_method
                    or member.compress_size != preflight_entry.compressed_size
                    or member.file_size != preflight_entry.file_size
                    or member.external_attr != preflight_entry.external_attributes
                    or member.header_offset != preflight_entry.local_header_offset
                ):
                    raise ArtifactDownloadError(
                        "GitHub artifact ZIP metadata changed between preflight and parsing"
                    )
                unix_mode = member.external_attr >> 16
                file_type = stat.S_IFMT(unix_mode)
                if member.is_dir() or file_type not in {0, stat.S_IFREG}:
                    raise ArtifactDownloadError(
                        "GitHub artifact ZIP member is not an ordinary file: "
                        f"{member.filename}"
                    )
                if member.flag_bits & 0x1:
                    raise ArtifactDownloadError(
                        f"GitHub artifact ZIP member is encrypted: {member.filename}"
                    )
                if member.file_size <= 0 or member.file_size > MAX_ARCHIVE_BYTES:
                    raise ArtifactDownloadError(
                        "GitHub artifact ZIP member size is outside the limit: "
                        f"{member.filename}"
                    )
                if member.compress_size < 0 or member.compress_size > artifact_snapshot.size:
                    raise ArtifactDownloadError(
                        "GitHub artifact ZIP member compressed size is invalid: "
                        f"{member.filename}"
                    )

                output = None
                if output_path is not None:
                    if os.path.lexists(output_path):
                        raise ArtifactDownloadError(
                            f"refusing to replace temporary public archive: {output_path}"
                        )
                    output = output_path.open("xb")
                    if hasattr(os, "fchmod"):
                        os.fchmod(output.fileno(), 0o600)
                try:
                    with archive.open(member, mode="r") as source:
                        while True:
                            chunk = source.read(COPY_BUFFER_BYTES)
                            if not chunk:
                                break
                            copied += len(chunk)
                            if copied > member.file_size or copied > MAX_ARCHIVE_BYTES:
                                raise ArtifactDownloadError(
                                    "GitHub artifact ZIP member exceeded its declared size"
                                )
                            digest.update(chunk)
                            if output is not None:
                                output.write(chunk)
                    if copied != member.file_size:
                        raise ArtifactDownloadError(
                            "GitHub artifact ZIP member was truncated: "
                            f"read {copied}, expected {member.file_size}"
                        )
                    if output is not None:
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    if output is not None:
                        output.close()
    except ArtifactDownloadError:
        raise
    except (
        OSError,
        ReleaseFileError,
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        zlib.error,
    ) as error:
        raise ArtifactDownloadError(
            f"cannot validate GitHub artifact ZIP for {expected_name}: {error}"
        ) from error
    return copied, digest.hexdigest()


def _verify_public_archive(
    path: Path,
    *,
    expected_name: str,
    expected_size: int,
    expected_digest: str,
) -> None:
    try:
        snapshot = inspect_regular_file(
            path,
            label=f"extracted public archive {expected_name}",
            require_single_link=True,
        )
        observed_digest = sha256_file(
            path,
            label=f"extracted public archive {expected_name}",
            require_single_link=True,
        )
    except ReleaseFileError as error:
        raise ArtifactDownloadError(
            f"extracted public archive is invalid: {error}"
        ) from error
    if snapshot.size != expected_size or observed_digest != expected_digest:
        raise ArtifactDownloadError(
            f"extracted public archive differs from its GitHub artifact: {expected_name}"
        )


def extract_public_archive(
    artifact_zip: Path,
    expected_name: str,
    artifact_id: int,
    output_directory: Path,
) -> Path:
    """Publish the unique validated public archive from one Actions artifact."""

    expected_name = _validated_archive_name(expected_name)
    if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0:
        raise ArtifactDownloadError("artifact ID must be a positive integer")
    _require_private_directory(output_directory, label="artifact output")
    destination = output_directory / expected_name
    temporary_prefix = f".{expected_name}.artifact-{artifact_id}."
    temporaries = _matching_temporary_paths(
        output_directory,
        prefix=temporary_prefix,
    )
    temporaries = _recover_destination_alias(
        destination,
        temporaries,
        label=f"public archive {expected_name}",
    )

    if os.path.lexists(destination):
        expected_size, expected_digest = _consume_artifact_member(
            artifact_zip,
            expected_name,
            output_path=None,
        )
        _verify_public_archive(
            destination,
            expected_name=expected_name,
            expected_size=expected_size,
            expected_digest=expected_digest,
        )
        for stale in temporaries:
            _remove_known_temporary(stale, label=f"public archive {expected_name}")
        return destination

    selected: Path | None = None
    expected_size = 0
    expected_digest = ""
    if temporaries:
        expected_size, expected_digest = _consume_artifact_member(
            artifact_zip,
            expected_name,
            output_path=None,
        )
        for stale in temporaries:
            try:
                _verify_public_archive(
                    stale,
                    expected_name=expected_name,
                    expected_size=expected_size,
                    expected_digest=expected_digest,
                )
            except ArtifactDownloadError:
                _remove_known_temporary(stale, label=f"public archive {expected_name}")
                continue
            if selected is None:
                selected = stale
            else:
                _remove_known_temporary(stale, label=f"public archive {expected_name}")

    if selected is None:
        selected = _random_temporary_path(
            output_directory,
            prefix=temporary_prefix,
        )
        try:
            expected_size, expected_digest = _consume_artifact_member(
                artifact_zip,
                expected_name,
                output_path=selected,
            )
            _verify_public_archive(
                selected,
                expected_name=expected_name,
                expected_size=expected_size,
                expected_digest=expected_digest,
            )
        except ArtifactDownloadError as error:
            _cleanup_and_reraise(
                selected,
                label=f"public archive {expected_name}",
                cause=error,
            )

    _publish_temporary(
        selected,
        destination,
        label=f"public archive {expected_name}",
    )
    for stale in _matching_temporary_paths(
        output_directory,
        prefix=temporary_prefix,
    ):
        _remove_known_temporary(stale, label=f"public archive {expected_name}")
    _verify_public_archive(
        destination,
        expected_name=expected_name,
        expected_size=expected_size,
        expected_digest=expected_digest,
    )
    return destination


def download_public_archive(
    repository: str,
    record: ArtifactRecord,
    expected_name: str,
    cache_directory: Path,
    output_directory: Path,
    downloader: ArtifactZipDownloader,
) -> Path:
    """Retrieve, verify, safely unwrap, and publish one release candidate."""

    artifact_zip = obtain_artifact_zip(
        repository,
        record,
        cache_directory,
        downloader,
    )
    return extract_public_archive(
        artifact_zip,
        expected_name,
        record.artifact_id,
        output_directory,
    )
