#!/usr/bin/env python3
"""Load and validate the release target contract used by CI and packaging."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


CONTRACT_PATH = Path(__file__).with_name("release-targets.json")
TARGET_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
RUST_TRIPLE = re.compile(r"^[a-z0-9_]+-[a-z0-9_]+-[a-z0-9_.-]+$")
TOOLCHAIN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
STABLE_TAG = re.compile(
    r"^v(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)$"
)
REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")
CODE_IDENTIFIER_COMPONENT = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
CODE_IDENTIFIER = re.compile(
    rf"^{CODE_IDENTIFIER_COMPONENT}(?:\.{CODE_IDENTIFIER_COMPONENT})+$"
)
ROOT_FIELDS = frozenset(
    {"schema_version", "release_toolchain", "macos_signing", "targets"}
)
MACOS_SIGNING_FIELDS = frozenset({"team_id", "code_identifier"})
TARGET_FIELDS = frozenset(
    {
        "id",
        "runner",
        "rust_targets",
        "test_target",
        "archive_format",
        "binary_name",
        "family",
        "python",
        "codex_install_verify",
        "glibc_max",
        "macos_minimum",
        "native_verify_runner",
    }
)
FAMILIES = frozenset(
    {"linux_gnu", "linux_musl", "macos", "macos_universal2", "windows_msvc"}
)


@dataclass(frozen=True)
class MacOSSigningPolicy:
    """Stable Developer ID identity fields shared by every macOS target."""

    team_id: str
    code_identifier: str


def validate_release_tag(tag: str) -> str:
    """Return one portable stable version tag or fail closed."""

    if not isinstance(tag, str) or STABLE_TAG.fullmatch(tag) is None:
        raise RuntimeError(f"release tag is not a stable semantic version: {tag!r}")
    return tag


def validate_repository(repository: str) -> str:
    """Return one bounded GitHub owner/repository slug or fail closed."""

    if not isinstance(repository, str) or REPOSITORY.fullmatch(repository) is None:
        raise RuntimeError(f"invalid GitHub repository name: {repository!r}")
    return repository


def validate_commit_sha(head_sha: str) -> str:
    """Return one exact lowercase Git commit SHA-1 or fail closed."""

    if not isinstance(head_sha, str) or COMMIT_SHA.fullmatch(head_sha) is None:
        raise RuntimeError("release commit must be an exact lowercase SHA-1")
    return head_sha


@dataclass(frozen=True)
class ReleaseTarget:
    """One independently built and smoke-tested release archive."""

    id: str
    runner: str
    rust_targets: tuple[str, ...]
    test_target: str | None
    archive_format: str
    binary_name: str
    family: str
    python: str
    codex_install_verify: bool
    glibc_max: str | None = None
    macos_minimum: str | None = None
    native_verify_runner: str | None = None

    @property
    def artifact_name(self) -> str:
        """Return the stable base of the GitHub Actions artifact name."""

        return f"app-icon-toolkit-{self.id}"

    def artifact_name_for_attempt(self, run_attempt: int) -> str:
        """Bind the Actions artifact name to one full workflow run attempt."""

        if (
            isinstance(run_attempt, bool)
            or not isinstance(run_attempt, int)
            or run_attempt <= 0
        ):
            raise RuntimeError("workflow run attempt must be a positive integer")
        return f"{self.artifact_name}-attempt-{run_attempt}"

    def release_filename(self, tag: str) -> str:
        """Return the public archive filename for a version tag."""

        return (
            f"app-icon-toolkit-{validate_release_tag(tag)}-"
            f"{self.id}.{self.archive_format}"
        )

    def matrix_entry(self) -> dict[str, Any]:
        """Return the JSON object consumed by a GitHub Actions matrix."""

        return {
            "id": self.id,
            "artifact_name": self.artifact_name,
            "runner": self.runner,
            "rust_targets": list(self.rust_targets),
            "test_target": self.test_target or "",
            "archive_format": self.archive_format,
            "binary_name": self.binary_name,
            "family": self.family,
            "python": self.python,
            "codex_install_verify": self.codex_install_verify,
            "native_verify_runner": self.native_verify_runner or "",
        }

    def macos_architectures(self) -> tuple[str, ...]:
        """Return the exact Mach-O architecture contract for a macOS target."""

        if self.family not in {"macos", "macos_universal2"}:
            raise RuntimeError(f"release target is not macOS: {self.id}")
        mapping = {"aarch64": "arm64", "x86_64": "x86_64"}
        return tuple(
            sorted(mapping[triple.split("-", maxsplit=1)[0]] for triple in self.rust_targets)
        )


@dataclass(frozen=True)
class ReleaseContract:
    """Validated release toolchain and target inventory."""

    release_toolchain: str
    macos_signing: MacOSSigningPolicy
    targets: tuple[ReleaseTarget, ...]

    def target(self, target_id: str) -> ReleaseTarget:
        """Return one target or fail instead of accepting an unapproved identifier."""

        for target in self.targets:
            if target.id == target_id:
                return target
        raise RuntimeError(f"unsupported release target: {target_id}")

    def rust_targets(self) -> tuple[str, ...]:
        """Return the sorted distinct Rust triples needed by all releases."""

        return tuple(sorted({triple for target in self.targets for triple in target.rust_targets}))


def _expect_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} must be a JSON object")
    return value


def _expect_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{context} must be a non-empty string")
    return value


def _optional_string(value: Any, context: str) -> str | None:
    if value is None:
        return None
    return _expect_string(value, context)


def _expect_bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{context} must be a boolean")
    return value


def _parse_macos_signing(raw_value: Any) -> MacOSSigningPolicy:
    context = "macos_signing"
    raw = _expect_object(raw_value, context)
    unknown = set(raw) - MACOS_SIGNING_FIELDS
    missing = MACOS_SIGNING_FIELDS - set(raw)
    if unknown:
        raise RuntimeError(f"{context} has unknown fields: {sorted(unknown)}")
    if missing:
        raise RuntimeError(f"{context} is missing fields: {sorted(missing)}")

    team_id = _expect_string(raw["team_id"], f"{context}.team_id")
    if TEAM_ID.fullmatch(team_id) is None:
        raise RuntimeError(
            f"{context}.team_id must be exactly 10 uppercase ASCII letters or digits"
        )

    code_identifier = _expect_string(
        raw["code_identifier"], f"{context}.code_identifier"
    )
    if CODE_IDENTIFIER.fullmatch(code_identifier) is None:
        raise RuntimeError(
            f"{context}.code_identifier must be a dot-separated code-signing identifier"
        )
    return MacOSSigningPolicy(team_id=team_id, code_identifier=code_identifier)


def _parse_target(raw_value: Any, index: int) -> ReleaseTarget:
    context = f"targets[{index}]"
    raw = _expect_object(raw_value, context)
    unknown = set(raw) - TARGET_FIELDS
    if unknown:
        raise RuntimeError(f"{context} has unknown fields: {sorted(unknown)}")
    required = TARGET_FIELDS - {"glibc_max", "macos_minimum", "native_verify_runner"}
    missing = required - set(raw)
    if missing:
        raise RuntimeError(f"{context} is missing fields: {sorted(missing)}")

    target_id = _expect_string(raw["id"], f"{context}.id")
    if TARGET_ID.fullmatch(target_id) is None:
        raise RuntimeError(f"{context}.id is not a portable identifier: {target_id}")

    rust_targets_value = raw["rust_targets"]
    if not isinstance(rust_targets_value, list) or not rust_targets_value:
        raise RuntimeError(f"{context}.rust_targets must be a non-empty array")
    rust_targets = tuple(
        _expect_string(value, f"{context}.rust_targets") for value in rust_targets_value
    )
    if len(set(rust_targets)) != len(rust_targets):
        raise RuntimeError(f"{context}.rust_targets contains a duplicate")
    invalid_triples = [triple for triple in rust_targets if RUST_TRIPLE.fullmatch(triple) is None]
    if invalid_triples:
        raise RuntimeError(f"{context}.rust_targets contains invalid triples: {invalid_triples}")

    test_target = _optional_string(raw["test_target"], f"{context}.test_target")
    if test_target is not None and test_target not in rust_targets:
        raise RuntimeError(f"{context}.test_target must be one of rust_targets")

    archive_format = _expect_string(raw["archive_format"], f"{context}.archive_format")
    if archive_format not in {"tar.gz", "zip"}:
        raise RuntimeError(f"{context}.archive_format is unsupported: {archive_format}")

    family = _expect_string(raw["family"], f"{context}.family")
    if family not in FAMILIES:
        raise RuntimeError(f"{context}.family is unsupported: {family}")

    target = ReleaseTarget(
        id=target_id,
        runner=_expect_string(raw["runner"], f"{context}.runner"),
        rust_targets=rust_targets,
        test_target=test_target,
        archive_format=archive_format,
        binary_name=_expect_string(raw["binary_name"], f"{context}.binary_name"),
        family=family,
        python=_expect_string(raw["python"], f"{context}.python"),
        codex_install_verify=_expect_bool(
            raw["codex_install_verify"], f"{context}.codex_install_verify"
        ),
        glibc_max=_optional_string(raw.get("glibc_max"), f"{context}.glibc_max"),
        macos_minimum=_optional_string(
            raw.get("macos_minimum"), f"{context}.macos_minimum"
        ),
        native_verify_runner=_optional_string(
            raw.get("native_verify_runner"), f"{context}.native_verify_runner"
        ),
    )
    _validate_target_relationships(target, context)
    return target


def _validate_target_relationships(target: ReleaseTarget, context: str) -> None:
    is_windows = target.family == "windows_msvc"
    expected_binary = "app-icon-toolkit-mcp.exe" if is_windows else "app-icon-toolkit-mcp"
    if target.binary_name != expected_binary:
        raise RuntimeError(f"{context}.binary_name does not match its platform family")
    expected_archive_format = (
        "zip"
        if target.family in {"macos", "macos_universal2", "windows_msvc"}
        else "tar.gz"
    )
    if target.archive_format != expected_archive_format:
        raise RuntimeError(
            f"{context}.archive_format must be {expected_archive_format} for {target.family}"
        )
    if target.family == "linux_gnu":
        if target.glibc_max is None or re.fullmatch(r"[0-9]+\.[0-9]+", target.glibc_max) is None:
            raise RuntimeError(f"{context}.glibc_max must be a numeric major.minor version")
    elif target.glibc_max is not None:
        raise RuntimeError(f"{context}.glibc_max is valid only for linux_gnu")

    is_macos = target.family in {"macos", "macos_universal2"}
    if is_macos:
        if target.macos_minimum is None or re.fullmatch(
            r"[0-9]+\.[0-9]+", target.macos_minimum
        ) is None:
            raise RuntimeError(f"{context}.macos_minimum must be a numeric major.minor version")
    elif target.macos_minimum is not None:
        raise RuntimeError(f"{context}.macos_minimum is valid only for macOS")

    if target.family == "macos_universal2":
        if target.id != "universal2-apple-darwin":
            raise RuntimeError(f"{context}.id must name the universal2 release explicitly")
        if set(target.rust_targets) != {
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
        }:
            raise RuntimeError(f"{context}.rust_targets must contain both macOS slices")
        if target.test_target is not None or target.native_verify_runner is None:
            raise RuntimeError(
                f"{context} must defer execution to its two native architecture jobs"
            )
    else:
        if len(target.rust_targets) != 1 or target.test_target != target.rust_targets[0]:
            raise RuntimeError(f"{context} must build and test one native Rust target")
        if target.id != target.rust_targets[0]:
            raise RuntimeError(f"{context}.id must equal its single Rust target")
        if target.native_verify_runner is not None:
            raise RuntimeError(f"{context}.native_verify_runner is valid only for universal2")

    rust_arches = {triple.split("-", maxsplit=1)[0] for triple in target.rust_targets}
    if target.family == "linux_gnu" and target.rust_targets != (
        "x86_64-unknown-linux-gnu",
    ):
        raise RuntimeError(f"{context} is not an approved GNU Linux target")
    if target.family == "linux_musl" and not all(
        triple.endswith("-unknown-linux-musl") for triple in target.rust_targets
    ):
        raise RuntimeError(f"{context}.family does not match its Rust target")
    if target.family in {"macos", "macos_universal2"} and not all(
        triple.endswith("-apple-darwin") for triple in target.rust_targets
    ):
        raise RuntimeError(f"{context}.family does not match its Rust target")
    if target.family == "windows_msvc" and not all(
        triple.endswith("-pc-windows-msvc") for triple in target.rust_targets
    ):
        raise RuntimeError(f"{context}.family does not match its Rust target")
    if not rust_arches <= {"aarch64", "x86_64"}:
        raise RuntimeError(f"{context} contains an unapproved release architecture")

    if target.family in {"linux_gnu", "linux_musl", "windows_msvc"}:
        is_arm_runner = target.runner.endswith("-arm")
        if is_arm_runner != (rust_arches == {"aarch64"}):
            raise RuntimeError(f"{context}.runner architecture does not match its Rust target")
    if target.family == "macos" and ("intel" in target.runner) != (rust_arches == {"x86_64"}):
        raise RuntimeError(f"{context}.runner architecture does not match its Rust target")
    expected_python = "python" if is_windows else "python3"
    if target.python != expected_python:
        raise RuntimeError(f"{context}.python does not match its runner operating system")


def _host_operating_system(family: str) -> str:
    if family.startswith("linux_"):
        return "linux"
    if family.startswith("macos"):
        return "macos"
    if family == "windows_msvc":
        return "windows"
    raise AssertionError(f"unhandled validated release family: {family}")


def load_contract(path: Path = CONTRACT_PATH) -> ReleaseContract:
    """Read and strictly validate the JSON release contract."""

    try:
        raw_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"failed to read release target contract {path}: {error}") from error
    raw = _expect_object(raw_value, "release target contract")
    unknown = set(raw) - ROOT_FIELDS
    missing = ROOT_FIELDS - set(raw)
    if unknown:
        raise RuntimeError(f"release target contract has unknown fields: {sorted(unknown)}")
    if missing:
        raise RuntimeError(f"release target contract is missing fields: {sorted(missing)}")
    if raw["schema_version"] != 3:
        raise RuntimeError("release target contract schema_version must be 3")
    toolchain = _expect_string(raw["release_toolchain"], "release_toolchain")
    if TOOLCHAIN.fullmatch(toolchain) is None:
        raise RuntimeError("release_toolchain must be an exact stable Rust version")
    macos_signing = _parse_macos_signing(raw["macos_signing"])
    targets_value = raw["targets"]
    if not isinstance(targets_value, list) or not targets_value:
        raise RuntimeError("release target contract targets must be a non-empty array")
    targets = tuple(_parse_target(value, index) for index, value in enumerate(targets_value))
    identifiers = [target.id for target in targets]
    if len(set(identifiers)) != len(identifiers):
        raise RuntimeError("release target contract contains duplicate target ids")
    artifact_names = [target.artifact_name for target in targets]
    if len(set(artifact_names)) != len(artifact_names):
        raise RuntimeError("release target contract contains duplicate artifact names")
    verified_operating_systems = sorted(
        _host_operating_system(target.family)
        for target in targets
        if target.codex_install_verify
    )
    if verified_operating_systems != ["linux", "macos", "windows"]:
        raise RuntimeError(
            "clean Codex install verification must select exactly one target each "
            "for Linux, macOS, and Windows"
        )
    return ReleaseContract(
        release_toolchain=toolchain,
        macos_signing=macos_signing,
        targets=targets,
    )


def verify_release_assets(contract: ReleaseContract, directory: Path, tag: str) -> None:
    """Reject missing, extra, non-regular, or symlinked public release assets."""

    if not directory.is_dir():
        raise RuntimeError(f"release asset directory does not exist: {directory}")
    expected = {target.release_filename(tag) for target in contract.targets}
    entries = list(directory.iterdir())
    actual = {entry.name for entry in entries}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(f"release asset mismatch; missing={missing}; extra={extra}")
    invalid = sorted(
        entry.name
        for entry in entries
        if entry.is_symlink() or not entry.is_file() or entry.stat().st_size == 0
    )
    if invalid:
        raise RuntimeError(f"release assets must be non-empty regular non-symlink files: {invalid}")


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("matrix")
    subparsers.add_parser("universal-verify-matrix")
    subparsers.add_parser("codex-install-matrix")
    subparsers.add_parser("toolchain")
    subparsers.add_parser("artifact-names")
    subparsers.add_parser("rust-targets")
    target_details = subparsers.add_parser("target-details")
    target_details.add_argument("--target", required=True)
    verify_assets = subparsers.add_parser("verify-assets")
    verify_assets.add_argument("--directory", type=Path, required=True)
    verify_assets.add_argument("--tag", required=True)
    arguments = parser.parse_args()

    contract = load_contract(arguments.contract)
    if arguments.command == "matrix":
        print(
            json.dumps(
                {"include": [target.matrix_entry() for target in contract.targets]},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    elif arguments.command == "universal-verify-matrix":
        entries = [
            {
                "id": target.id,
                "runner": target.native_verify_runner,
                "archive_format": target.archive_format,
                "binary_name": target.binary_name,
            }
            for target in contract.targets
            if target.native_verify_runner is not None
        ]
        if len(entries) != 1:
            raise RuntimeError(
                f"expected exactly one cross-architecture native verifier; found {len(entries)}"
            )
        print(json.dumps({"include": entries}, separators=(",", ":"), sort_keys=True))
    elif arguments.command == "codex-install-matrix":
        entries = [
            target.matrix_entry()
            for target in contract.targets
            if target.codex_install_verify
        ]
        if not entries:
            raise RuntimeError("expected at least one clean Codex installation target")
        print(json.dumps({"include": entries}, separators=(",", ":"), sort_keys=True))
    elif arguments.command == "toolchain":
        print(contract.release_toolchain)
    elif arguments.command == "artifact-names":
        for target in contract.targets:
            print(target.artifact_name)
    elif arguments.command == "rust-targets":
        for target in contract.rust_targets():
            print(target)
    elif arguments.command == "target-details":
        print(
            json.dumps(
                contract.target(arguments.target).matrix_entry(),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    elif arguments.command == "verify-assets":
        verify_release_assets(contract, arguments.directory, arguments.tag)
    else:
        raise AssertionError(f"unhandled command: {arguments.command}")


if __name__ == "__main__":
    _main()
