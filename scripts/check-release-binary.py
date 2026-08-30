#!/usr/bin/env python3
"""Fail-closed native dependency and architecture checks for release binaries."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys


SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from release_targets import ReleaseContract, ReleaseTarget, load_contract


WINDOWS_IMPORT = re.compile(r"(?im)^\s*Name:\s*([^\s]+\.dll)\s*$")
BANNED_WINDOWS_CRT = re.compile(
    r"(?i)^(?:vcruntime[^.]*|msvcp[^.]*|msvcr[^.]*|concrt[^.]*|ucrtbase|api-ms-win-crt-[^.]+)\.dll$"
)
GLIBC_VERSION = re.compile(r"\bGLIBC_([0-9]+)\.([0-9]+)\b")
WINDOWS_MACHINE = re.compile(r"(?m)^\s*Machine:\s*(IMAGE_FILE_MACHINE_[A-Z0-9_]+)")
ELF_MACHINE = re.compile(r"(?m)^\s*Machine:\s*(.+?)\s*$")
MACOS_MINIMUM = re.compile(r"(?m)^\s*minos\s+([0-9]+(?:\.[0-9]+){1,2})\s*$")


def run_text(command: list[str]) -> str:
    """Run a required native inspector and return its UTF-8 output."""

    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    if completed.stderr:
        raise RuntimeError(
            f"native inspector wrote to stderr: {' '.join(command)}: {completed.stderr.strip()}"
        )
    return completed.stdout


def parse_windows_imports(output: str) -> set[str]:
    """Extract PE import names and reject an empty or dynamically linked CRT set."""

    imports = {match.group(1) for match in WINDOWS_IMPORT.finditer(output)}
    if not imports:
        raise RuntimeError("PE inspection returned no imported DLL names")
    banned = sorted(name for name in imports if BANNED_WINDOWS_CRT.fullmatch(name))
    if banned:
        raise RuntimeError(f"Windows release binary dynamically imports the CRT: {banned}")
    return imports


def assert_windows_machine(output: str, expected_arch: str) -> None:
    """Require the PE machine field to match the release target architecture."""

    expected = {
        "x86_64": "IMAGE_FILE_MACHINE_AMD64",
        "aarch64": "IMAGE_FILE_MACHINE_ARM64",
    }.get(expected_arch)
    if expected is None:
        raise RuntimeError(f"unsupported Windows release architecture: {expected_arch}")
    matches = set(WINDOWS_MACHINE.findall(output))
    if matches != {expected}:
        raise RuntimeError(f"PE machine values are {sorted(matches)}; expected {expected}")


def assert_elf_machine(output: str, expected_arch: str) -> None:
    """Require the ELF machine field to match the release target architecture."""

    expected = {
        "x86_64": "Advanced Micro Devices X86-64",
        "aarch64": "AArch64",
    }.get(expected_arch)
    if expected is None:
        raise RuntimeError(f"unsupported Linux release architecture: {expected_arch}")
    matches = {match.strip() for match in ELF_MACHINE.findall(output)}
    if matches != {expected}:
        raise RuntimeError(f"ELF machine values are {sorted(matches)}; expected {expected}")


def parse_glibc_versions(output: str, maximum: str) -> set[tuple[int, int]]:
    """Extract GLIBC requirements and enforce an integer major/minor ceiling."""

    if "GLIBC_PRIVATE" in output:
        raise RuntimeError("GNU Linux release binary references GLIBC_PRIVATE")
    versions = {
        (int(match.group(1)), int(match.group(2)))
        for match in GLIBC_VERSION.finditer(output)
    }
    if not versions:
        raise RuntimeError("GNU Linux inspection returned no GLIBC symbol versions")
    maximum_tuple = tuple(int(component) for component in maximum.split("."))
    if len(maximum_tuple) != 2:
        raise RuntimeError(f"invalid GLIBC maximum in release contract: {maximum}")
    too_new = sorted(version for version in versions if version > maximum_tuple)
    if too_new:
        rendered = [".".join(map(str, version)) for version in too_new]
        raise RuntimeError(f"GNU Linux release binary requires GLIBC above {maximum}: {rendered}")
    return versions


def assert_static_musl(program_headers: str, dynamic_entries: str) -> None:
    """Reject an ELF interpreter or any dynamic-library dependency."""

    if re.search(r"(?m)^\s*INTERP\s", program_headers):
        raise RuntimeError("musl release binary contains an ELF INTERP program header")
    if re.search(r"\(NEEDED\)", dynamic_entries):
        raise RuntimeError("musl release binary contains a dynamic NEEDED entry")


def assert_macos_architectures(output: str, expected: set[str]) -> None:
    """Require the exact Mach-O slice set for one macOS archive."""

    actual = set(output.split())
    if actual != expected:
        raise RuntimeError(f"Mach-O architectures are {sorted(actual)}; expected {sorted(expected)}")


def assert_macos_minimum(output: str, expected: str, architecture: str) -> None:
    """Require one Mach-O slice to carry the configured deployment minimum."""

    versions = set(MACOS_MINIMUM.findall(output))
    if len(versions) != 1:
        raise RuntimeError(
            f"Mach-O {architecture} slice reported deployment minimums {sorted(versions)}"
        )

    def version_tuple(value: str) -> tuple[int, int, int]:
        components = [int(component) for component in value.split(".")]
        components.extend([0] * (3 - len(components)))
        return tuple(components[:3])

    actual = next(iter(versions))
    if version_tuple(actual) != version_tuple(expected):
        raise RuntimeError(
            f"Mach-O {architecture} minimum is {actual}; expected {expected}"
        )


def rust_llvm_readobj(contract: ReleaseContract) -> Path:
    """Locate the pinned toolchain's installed llvm-readobj binary."""

    sysroot = Path(
        run_text(["rustc", f"+{contract.release_toolchain}", "--print", "sysroot"]).strip()
    )
    version = run_text(["rustc", f"+{contract.release_toolchain}", "-vV"])
    host_match = re.search(r"(?m)^host:\s*(\S+)\s*$", version)
    if host_match is None:
        raise RuntimeError("pinned rustc did not report its host triple")
    executable = "llvm-readobj.exe" if sys.platform == "win32" else "llvm-readobj"
    inspector = sysroot / "lib" / "rustlib" / host_match.group(1) / "bin" / executable
    if not inspector.is_file():
        raise RuntimeError(
            f"llvm-readobj is unavailable at {inspector}; install llvm-tools-preview"
        )
    return inspector


def check_binary(contract: ReleaseContract, target: ReleaseTarget, binary: Path) -> None:
    """Run the platform-specific final-binary gate."""

    if binary.is_symlink() or not binary.is_file() or binary.stat().st_size == 0:
        raise RuntimeError(f"release binary must be a non-empty regular non-symlink file: {binary}")

    if target.family == "windows_msvc":
        output = run_text(
            [str(rust_llvm_readobj(contract)), "--file-headers", "--coff-imports", str(binary)]
        )
        assert_windows_machine(output, target.rust_targets[0].split("-", maxsplit=1)[0])
        parse_windows_imports(output)
    elif target.family == "linux_gnu":
        if target.glibc_max is None:
            raise RuntimeError(f"release target {target.id} omitted glibc_max")
        header = run_text(["readelf", "--file-header", "--wide", str(binary)])
        assert_elf_machine(header, target.rust_targets[0].split("-", maxsplit=1)[0])
        output = run_text(["readelf", "--version-info", "--wide", str(binary)])
        parse_glibc_versions(output, target.glibc_max)
    elif target.family == "linux_musl":
        header = run_text(["readelf", "--file-header", "--wide", str(binary)])
        assert_elf_machine(header, target.rust_targets[0].split("-", maxsplit=1)[0])
        program_headers = run_text(["readelf", "--program-headers", "--wide", str(binary)])
        dynamic_entries = run_text(["readelf", "--dynamic", "--wide", str(binary)])
        assert_static_musl(program_headers, dynamic_entries)
    elif target.family in {"macos", "macos_universal2"}:
        output = run_text(["xcrun", "lipo", "-archs", str(binary)])
        expected = (
            {"arm64", "x86_64"}
            if target.family == "macos_universal2"
            else {"arm64" if target.id.startswith("aarch64-") else "x86_64"}
        )
        assert_macos_architectures(output, expected)
        if target.macos_minimum is None:
            raise RuntimeError(f"macOS release target {target.id} omitted macos_minimum")
        for architecture in sorted(expected):
            build = run_text(
                [
                    "xcrun",
                    "vtool",
                    "-arch",
                    architecture,
                    "-show-build",
                    str(binary),
                ]
            )
            assert_macos_minimum(build, target.macos_minimum, architecture)
    else:
        raise AssertionError(f"unhandled release target family: {target.family}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, default=Path("."))
    parser.add_argument("--target", required=True)
    parser.add_argument("--binary", type=Path, required=True)
    arguments = parser.parse_args()

    root = arguments.plugin_root.resolve(strict=True)
    contract = load_contract(root / "scripts" / "release-targets.json")
    target = contract.target(arguments.target)
    binary = Path(os.path.abspath(arguments.binary))
    check_binary(contract, target, binary)


if __name__ == "__main__":
    main()
