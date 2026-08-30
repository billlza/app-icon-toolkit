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
import tarfile
import tempfile
import zipfile


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


def installed_binary_name(target: str) -> str:
    if target == "x86_64-pc-windows-msvc":
        return "app-icon-toolkit-mcp.exe"
    if target in {
        "x86_64-unknown-linux-gnu",
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
    }:
        return "app-icon-toolkit-mcp"
    raise RuntimeError(f"unsupported release target: {target}")


def copy_package(
    plugin_root: Path, package_root: Path, binary: Path, target: str
) -> Path:
    for relative in STATIC_PATHS:
        source = plugin_root / relative
        if not source.is_file():
            raise RuntimeError(f"release input is missing: {source}")
        destination = package_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    installed_name = installed_binary_name(target)
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
    binary = arguments.binary.resolve(strict=True)
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive_name = f"app-icon-toolkit-{arguments.tag}-{arguments.target}.{arguments.format}"
    destination = output / archive_name
    if destination.exists():
        raise SystemExit(f"refusing to replace existing release archive: {destination}")

    with tempfile.TemporaryDirectory(prefix="app-icon-toolkit-package-") as temporary:
        package_root = Path(temporary) / "app-icon-toolkit"
        copy_package(plugin_root, package_root, binary, arguments.target)
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
