#!/usr/bin/env python3
"""Build one deterministic, smoke-tested local plugin release archive."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import filecmp
import gzip
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import BinaryIO
import zipfile

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from release_targets import CONTRACT_PATH, ReleaseTarget, load_contract
from release_files import (
    FileSnapshot,
    FilePublicationIndeterminate,
    copy_regular_file,
    inspect_regular_file,
    open_stable_regular_file,
    publish_sibling_no_replace,
)
from release_package import (
    MAX_ARCHIVE_BYTES,
    MAX_MEMBER_BYTES,
    MAX_MEMBER_NAME_BYTES,
    MAX_MEMBERS,
    MAX_TOTAL_EXTRACTED_BYTES,
    PACKAGE_ROOT_NAME,
    STATIC_PATHS,
    safe_extract_archive,
)


ARCHIVE_COPY_BUFFER_BYTES = 1024 * 1024


class ArchiveVerificationMode(str, Enum):
    """Select whether archive verification may execute the packaged binary."""

    RUNTIME_SMOKE = "runtime-smoke"
    STATIC_ONLY = "static-only"


@dataclass(frozen=True)
class ArchiveEntry:
    """One stable, bounded source admitted to a release archive."""

    source: Path
    name: str
    size: int
    mode: int


@dataclass(frozen=True)
class PackageInput:
    """One source authorized before any package output is created."""

    source: Path
    relative: Path
    name: str
    mode: int
    snapshot: FileSnapshot


class _BoundedArchiveOutput:
    """Seekable writer that refuses to grow a release archive past its limit."""

    def __init__(self, output: BinaryIO, limit: int) -> None:
        self._output = output
        self._limit = limit

    def write(self, payload: bytes | bytearray | memoryview) -> int:
        position = self.tell()
        if len(payload) > self._limit - position:
            raise RuntimeError(
                f"release archive exceeds the {self._limit}-byte output limit"
            )
        written = self._output.write(payload)
        if written != len(payload):
            raise OSError(
                f"release archive write was short: wrote {written}, expected {len(payload)}"
            )
        return written

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        position = self._output.seek(offset, whence)
        if position < 0 or position > self._limit:
            raise RuntimeError("release archive writer sought outside its output limit")
        return position

    def tell(self) -> int:
        return self._output.tell()

    def flush(self) -> None:
        self._output.flush()

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True


def installed_binary_name(target: str, plugin_root: Path | None = None) -> str:
    contract_path = (
        CONTRACT_PATH if plugin_root is None else plugin_root / "scripts" / "release-targets.json"
    )
    return load_contract(contract_path).target(target).binary_name


def validate_archive_format(target: ReleaseTarget, requested_format: str) -> None:
    if requested_format != target.archive_format:
        raise RuntimeError(
            f"release target {target.id} requires {target.archive_format}, not {requested_format}"
        )


def validate_static_input(source: Path) -> None:
    inspect_regular_file(
        source,
        label="release input",
        require_single_link=True,
    )


def copy_package(
    plugin_root: Path, package_root: Path, binary: Path, target: ReleaseTarget
) -> Path:
    inputs = [
        (plugin_root / relative, relative, 0o644)
        for relative in STATIC_PATHS
    ]
    inputs.append((binary, Path("bin") / target.binary_name, 0o755))
    if len(inputs) > MAX_MEMBERS:
        raise RuntimeError("release package has too many inputs")
    authorized: list[PackageInput] = []
    total = 0
    for source, relative, _mode in inputs:
        name = (Path(PACKAGE_ROOT_NAME) / relative).as_posix()
        if len(name.encode("utf-8", errors="strict")) > MAX_MEMBER_NAME_BYTES:
            raise RuntimeError(f"release package member name is too long: {name}")
        snapshot = inspect_regular_file(
            source,
            label="release input",
            require_single_link=True,
        )
        if snapshot.size > MAX_MEMBER_BYTES:
            raise RuntimeError(f"release package member exceeds its size limit: {name}")
        total += snapshot.size
        if total > MAX_TOTAL_EXTRACTED_BYTES:
            raise RuntimeError("release package exceeds its total size limit")
        authorized.append(
            PackageInput(
                source=source,
                relative=relative,
                name=name,
                mode=_mode,
                snapshot=snapshot,
            )
        )

    copied_total = 0
    for package_input in authorized:
        destination = package_root / package_input.relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        copied = copy_regular_file(
            package_input.source,
            destination,
            mode=package_input.mode,
            label="release input",
            expected_source=package_input.snapshot,
            maximum_bytes=min(
                MAX_MEMBER_BYTES,
                MAX_TOTAL_EXTRACTED_BYTES - copied_total,
            ),
        )
        copied_total += copied.size
        if copied_total > MAX_TOTAL_EXTRACTED_BYTES:
            raise RuntimeError("release package exceeds its total size limit")
    return package_root / "bin" / target.binary_name


def archive_entries(package_root: Path) -> list[Path]:
    return sorted(
        (path for path in package_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(package_root).as_posix(),
    )


def validated_archive_entries(package_root: Path) -> tuple[ArchiveEntry, ...]:
    """Freeze the exact bounded member inventory before writing output bytes."""

    sources = archive_entries(package_root)
    if not sources or len(sources) > MAX_MEMBERS:
        raise RuntimeError("release package has an invalid member count")
    entries: list[ArchiveEntry] = []
    total = 0
    for source in sources:
        relative = Path(PACKAGE_ROOT_NAME) / source.relative_to(package_root)
        name = relative.as_posix()
        try:
            encoded_name = name.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise RuntimeError(f"release package member name is not UTF-8: {name!r}") from error
        if len(encoded_name) > MAX_MEMBER_NAME_BYTES:
            raise RuntimeError(f"release package member name is too long: {name}")
        snapshot = inspect_regular_file(
            source,
            label="release package member",
            require_single_link=True,
        )
        if snapshot.size > MAX_MEMBER_BYTES:
            raise RuntimeError(f"release package member exceeds its size limit: {name}")
        total += snapshot.size
        if total > MAX_TOTAL_EXTRACTED_BYTES:
            raise RuntimeError("release package exceeds its total size limit")
        entries.append(
            ArchiveEntry(
                source=source,
                name=name,
                size=snapshot.size,
                mode=0o755 if source.parent.name == "bin" else 0o644,
            )
        )
    return tuple(entries)


def _write_tar_gz(
    entries: tuple[ArchiveEntry, ...],
    raw_output: BinaryIO,
    epoch: int,
) -> None:
    with gzip.GzipFile(fileobj=raw_output, mode="wb", filename="", mtime=epoch) as compressed:
        with tarfile.open(
            fileobj=compressed,
            mode="w",
            format=tarfile.USTAR_FORMAT,
        ) as archive:
            for entry in entries:
                information = tarfile.TarInfo(entry.name)
                information.size = entry.size
                information.mode = entry.mode
                information.mtime = epoch
                information.uid = 0
                information.gid = 0
                information.uname = ""
                information.gname = ""
                with open_stable_regular_file(
                    entry.source,
                    label=f"release package member {entry.name}",
                    require_single_link=True,
                ) as (input_file, snapshot):
                    if snapshot.size != entry.size:
                        raise RuntimeError(
                            f"release package member changed before TAR write: {entry.name}"
                        )
                    archive.addfile(information, input_file)


def _copy_zip_entry(source: BinaryIO, output: BinaryIO, entry: ArchiveEntry) -> None:
    copied = 0
    while True:
        chunk = source.read(ARCHIVE_COPY_BUFFER_BYTES)
        if not chunk:
            break
        copied += len(chunk)
        if copied > entry.size:
            raise RuntimeError(
                f"release package member grew during ZIP write: {entry.name}"
            )
        output.write(chunk)
    if copied != entry.size:
        raise RuntimeError(
            f"release package member was truncated during ZIP write: {entry.name}"
        )


def _write_zip(
    entries: tuple[ArchiveEntry, ...],
    raw_output: BinaryIO,
    epoch: int,
) -> None:
    timestamp = datetime.fromtimestamp(max(epoch, 315532800), tz=timezone.utc)
    date_time = (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second)
    with zipfile.ZipFile(raw_output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for entry in entries:
            information = zipfile.ZipInfo(entry.name, date_time=date_time)
            information.create_system = 3
            information.external_attr = (
                (stat.S_IFREG | entry.mode) & 0xFFFF
            ) << 16
            information.compress_type = zipfile.ZIP_DEFLATED
            information.file_size = entry.size
            with open_stable_regular_file(
                entry.source,
                label=f"release package member {entry.name}",
                require_single_link=True,
            ) as (input_file, snapshot):
                if snapshot.size != entry.size:
                    raise RuntimeError(
                        f"release package member changed before ZIP write: {entry.name}"
                    )
                with archive.open(
                    information,
                    mode="w",
                    force_zip64=False,
                ) as member_output:
                    _copy_zip_entry(input_file, member_output, entry)


def _write_archive(
    package_root: Path, raw_output: BinaryIO, epoch: int, archive_format: str
) -> None:
    entries = validated_archive_entries(package_root)
    bounded_output = _BoundedArchiveOutput(raw_output, MAX_ARCHIVE_BYTES)
    if archive_format == "tar.gz":
        _write_tar_gz(entries, bounded_output, epoch)
    elif archive_format == "zip":
        _write_zip(entries, bounded_output, epoch)
    else:
        raise RuntimeError(f"unsupported release archive format: {archive_format}")
    bounded_output.flush()


def _write_archive_file(
    package_root: Path, destination: Path, epoch: int, archive_format: str
) -> None:
    created = False
    try:
        with destination.open("xb") as raw_output:
            created = True
            _write_archive(package_root, raw_output, epoch, archive_format)
            raw_output.flush()
            os.fsync(raw_output.fileno())
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def write_tar_gz(package_root: Path, destination: Path, epoch: int) -> None:
    _write_archive_file(package_root, destination, epoch, "tar.gz")


def write_zip(package_root: Path, destination: Path, epoch: int) -> None:
    _write_archive_file(package_root, destination, epoch, "zip")


def publish_archive_no_replace(temporary: Path, destination: Path) -> None:
    """Atomically publish a complete sibling archive without replacing a path."""

    publish_sibling_no_replace(
        temporary,
        destination,
        label="release archive",
    )


def prepare_release_archive(
    plugin_root: Path,
    binary: Path,
    target: ReleaseTarget,
    tag: str,
    output: Path,
    source_date_epoch: int,
    verification_mode: ArchiveVerificationMode,
) -> Path:
    """Build, verify, and atomically publish one release archive."""

    destination = output / target.release_filename(tag)
    if os.path.lexists(destination):
        raise RuntimeError(f"refusing to replace existing release archive: {destination}")

    with tempfile.TemporaryDirectory(prefix="app-icon-toolkit-package-") as temporary:
        package_root = Path(temporary) / PACKAGE_ROOT_NAME
        copy_package(plugin_root, package_root, binary, target)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=output,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary_archive = Path(temporary_name)
        preserve_temporary = False
        try:
            with os.fdopen(descriptor, "wb") as raw_output:
                descriptor = -1
                _write_archive(
                    package_root,
                    raw_output,
                    source_date_epoch,
                    target.archive_format,
                )
                raw_output.flush()
                os.fsync(raw_output.fileno())
            temporary_archive.chmod(0o644)
            verify_archive(
                plugin_root,
                package_root,
                temporary_archive,
                target.archive_format,
                verification_mode,
            )
            publish_archive_no_replace(temporary_archive, destination)
        except FilePublicationIndeterminate:
            preserve_temporary = True
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not preserve_temporary:
                temporary_archive.unlink(missing_ok=True)
    return destination


def validate_extracted_package(extraction_root: Path, source_package: Path) -> Path:
    top_level = sorted(path.name for path in extraction_root.iterdir())
    if top_level != [PACKAGE_ROOT_NAME]:
        raise RuntimeError(
            "release archive must contain exactly one app-icon-toolkit root; "
            f"found {top_level}"
        )

    extracted_package = extraction_root / PACKAGE_ROOT_NAME
    for path in extracted_package.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise RuntimeError(f"release archive extracted an unsupported entry: {path}")

    expected = {
        path.relative_to(source_package) for path in archive_entries(source_package)
    }
    actual = {
        path.relative_to(extracted_package) for path in archive_entries(extracted_package)
    }
    if actual != expected:
        missing = sorted(path.as_posix() for path in expected - actual)
        extra = sorted(path.as_posix() for path in actual - expected)
        raise RuntimeError(
            f"release archive contents differ from the package: missing={missing}, extra={extra}"
        )

    for relative in sorted(expected, key=Path.as_posix):
        if not filecmp.cmp(
            source_package / relative,
            extracted_package / relative,
            shallow=False,
        ):
            raise RuntimeError(
                f"release archive changed packaged file bytes: {relative.as_posix()}"
            )
    return extracted_package


def verify_archive(
    plugin_root: Path,
    source_package: Path,
    archive: Path,
    archive_format: str,
    verification_mode: ArchiveVerificationMode,
) -> None:
    if not isinstance(verification_mode, ArchiveVerificationMode):
        raise RuntimeError("archive verification mode must be an ArchiveVerificationMode")
    with tempfile.TemporaryDirectory(prefix="app-icon-toolkit-extracted-") as temporary:
        extraction_root = Path(temporary)
        sources = archive_entries(source_package)
        expected_sizes = {
            (Path(PACKAGE_ROOT_NAME) / source.relative_to(source_package))
            .as_posix(): source.stat().st_size
            for source in sources
        }
        safe_extract_archive(
            archive,
            archive_format,
            extraction_root,
            tuple(expected_sizes),
            expected_sizes,
        )
        extracted_package = validate_extracted_package(extraction_root, source_package)
        if verification_mode is ArchiveVerificationMode.RUNTIME_SMOKE:
            subprocess.run(
                [
                    os.environ.get("PYTHON", "python3"),
                    str(plugin_root / "scripts" / "smoke-installed-plugin.py"),
                    str(extracted_package),
                ],
                check=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, default=Path("."))
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--format", choices=("tar.gz", "zip"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument(
        "--verification-mode",
        choices=tuple(mode.value for mode in ArchiveVerificationMode),
        required=True,
    )
    arguments = parser.parse_args()

    plugin_root = arguments.plugin_root.resolve(strict=True)
    binary = Path(os.path.abspath(arguments.binary))
    try:
        with open_stable_regular_file(
            binary,
            label="release binary",
            require_single_link=True,
        ):
            pass
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    target = load_contract(plugin_root / "scripts" / "release-targets.json").target(
        arguments.target
    )
    try:
        validate_archive_format(target, arguments.format)
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        destination = prepare_release_archive(
            plugin_root,
            binary,
            target,
            arguments.tag,
            output,
            arguments.source_date_epoch,
            ArchiveVerificationMode(arguments.verification_mode),
        )
    except RuntimeError as error:
        raise SystemExit(str(error)) from error

    print(destination)


if __name__ == "__main__":
    main()
