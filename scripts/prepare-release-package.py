#!/usr/bin/env python3
"""Build one deterministic, smoke-tested local plugin release archive."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import filecmp
import gzip
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from release_targets import CONTRACT_PATH, ReleaseTarget, load_contract
from release_package import PACKAGE_ROOT_NAME, STATIC_PATHS, safe_extract_archive


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
    if source.is_symlink() or not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError(
            f"release input must be a non-empty regular non-symlink file: {source}"
        )


def copy_package(
    plugin_root: Path, package_root: Path, binary: Path, target: ReleaseTarget
) -> Path:
    for relative in STATIC_PATHS:
        source = plugin_root / relative
        validate_static_input(source)
        destination = package_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    installed_name = target.binary_name
    installed_binary = package_root / "bin" / installed_name
    installed_binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(binary, installed_binary)
    installed_binary.chmod(0o755)
    return installed_binary


def archive_entries(package_root: Path) -> list[Path]:
    return sorted(
        (path for path in package_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(package_root).as_posix(),
    )


def write_tar_gz(package_root: Path, destination: Path, epoch: int) -> None:
    with destination.open("xb") as raw_output:
        with gzip.GzipFile(fileobj=raw_output, mode="wb", filename="", mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for source in archive_entries(package_root):
                    relative = Path("app-icon-toolkit") / source.relative_to(package_root)
                    information = tarfile.TarInfo(relative.as_posix())
                    information.size = source.stat().st_size
                    information.mode = 0o755 if source.parent.name == "bin" else 0o644
                    information.mtime = epoch
                    information.uid = 0
                    information.gid = 0
                    information.uname = ""
                    information.gname = ""
                    with source.open("rb") as input_file:
                        archive.addfile(information, input_file)


def write_zip(package_root: Path, destination: Path, epoch: int) -> None:
    timestamp = datetime.fromtimestamp(max(epoch, 315532800), tz=timezone.utc)
    date_time = (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second)
    with zipfile.ZipFile(destination, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in archive_entries(package_root):
            relative = Path("app-icon-toolkit") / source.relative_to(package_root)
            information = zipfile.ZipInfo(relative.as_posix(), date_time=date_time)
            mode = 0o755 if source.parent.name == "bin" else 0o644
            information.external_attr = (mode & 0xFFFF) << 16
            information.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(information, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


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


def smoke_test_archive(
    plugin_root: Path,
    source_package: Path,
    archive: Path,
    archive_format: str,
) -> None:
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
    arguments = parser.parse_args()

    plugin_root = arguments.plugin_root.resolve(strict=True)
    binary = Path(os.path.abspath(arguments.binary))
    if binary.is_symlink() or not binary.is_file() or binary.stat().st_size == 0:
        raise SystemExit(f"release binary must be a non-empty regular non-symlink file: {binary}")
    target = load_contract(plugin_root / "scripts" / "release-targets.json").target(
        arguments.target
    )
    try:
        validate_archive_format(target, arguments.format)
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive_name = target.release_filename(arguments.tag)
    destination = output / archive_name
    if destination.exists():
        raise SystemExit(f"refusing to replace existing release archive: {destination}")

    with tempfile.TemporaryDirectory(prefix="app-icon-toolkit-package-") as temporary:
        package_root = Path(temporary) / "app-icon-toolkit"
        copy_package(plugin_root, package_root, binary, target)
        if arguments.format == "tar.gz":
            write_tar_gz(package_root, destination, arguments.source_date_epoch)
        else:
            write_zip(package_root, destination, arguments.source_date_epoch)
        smoke_test_archive(
            plugin_root,
            package_root,
            destination,
            arguments.format,
        )

    print(destination)


if __name__ == "__main__":
    main()
