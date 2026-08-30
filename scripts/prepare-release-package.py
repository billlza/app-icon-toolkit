#!/usr/bin/env python3
"""Build one deterministic, smoke-tested local plugin release archive."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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


STATIC_PATHS = (
    Path(".agents/plugins/marketplace.json"),
    Path(".codex-plugin/plugin.json"),
    Path(".mcp.json"),
    Path("ARCHITECTURE.md"),
    Path("CHANGELOG.md"),
    Path("LICENSE"),
    Path("README.md"),
    Path("SECURITY.md"),
    Path("THIRD_PARTY_LICENSES.html"),
)


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


def copy_package(
    plugin_root: Path, package_root: Path, binary: Path, target: ReleaseTarget
) -> Path:
    for relative in STATIC_PATHS:
        source = plugin_root / relative
        if not source.is_file():
            raise RuntimeError(f"release input is missing: {source}")
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
        subprocess.run(
            [
                os.environ.get("PYTHON", "python3"),
                str(plugin_root / "scripts" / "smoke-installed-plugin.py"),
                str(package_root),
            ],
            check=True,
        )
        if arguments.format == "tar.gz":
            write_tar_gz(package_root, destination, arguments.source_date_epoch)
        else:
            write_zip(package_root, destination, arguments.source_date_epoch)

    print(destination)


if __name__ == "__main__":
    main()
