#!/usr/bin/env python3
"""Fail-closed Developer ID signing and notarization primitives for macOS.

This module intentionally contains no GitHub or release-version coordination.
Callers provide an explicit command runner so command execution can be audited
and the state-changing boundaries can be tested without a signing identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Protocol, Sequence
import uuid


SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_files


CODESIGN = "/usr/bin/codesign"
SECURITY = "/usr/bin/security"
XCRUN = "/usr/bin/xcrun"

SHA1_IDENTITY = re.compile(r"^[0-9A-F]{40}$")
SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")
TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$")
ARCHITECTURES = frozenset({"arm64", "x86_64"})
TIMEOUT = re.compile(r"^[1-9][0-9]*(?:s|m|h)?$")
IDENTITY_LINE = re.compile(
    r'^\s*[0-9]+\)\s+([0-9A-F]{40})\s+"([^"]+)"\s*$',
    re.MULTILINE,
)
CODE_DIRECTORY_FLAGS = re.compile(r"\bflags=0x[0-9a-fA-F]+\(([^)]*)\)")
CDHASH = re.compile(r"^[0-9a-fA-F]{40}$")
MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024


class MacSigningError(RuntimeError):
    """Base class for a macOS release-signing failure."""


class InputValidationError(MacSigningError):
    """A caller supplied an unsafe or malformed input."""


class CommandExecutionError(MacSigningError):
    """A required external command failed or violated the runner contract."""


class SignatureValidationError(MacSigningError):
    """A pre-sign or post-sign signature did not satisfy the policy."""


class NotarizationValidationError(MacSigningError):
    """A notary response or archive transition did not satisfy the policy."""


class SubmissionOutcomeUnknown(NotarizationValidationError):
    """The notary submit process failed after the external boundary was invoked."""


@dataclass(frozen=True)
class CommandResult:
    """Complete text result returned by a command runner."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Injectable, shell-free command execution boundary."""

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        """Execute exactly ``argv`` and return its captured text result."""


@dataclass(frozen=True)
class SubprocessRunner:
    """Production shell-free runner for bounded Apple control-plane output.

    ``subprocess.run`` captures before returning. The common ``_invoke`` boundary
    therefore rejects more than ``MAX_COMMAND_OUTPUT_BYTES`` before any output is
    parsed or retained in a receipt. The supported Apple commands emit small
    metadata or notarization JSON; this runner is not for arbitrary child output.
    """

    timeout_seconds: float | None = None

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        try:
            completed = subprocess.run(
                argv,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as error:
            raise CommandExecutionError(
                f"failed to execute command {argv[0]!r}: {error}"
            ) from error
        return CommandResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class PreSignKind(Enum):
    """Only the two signature states accepted before Developer ID signing."""

    UNSIGNED = "unsigned"
    AD_HOC = "ad_hoc"


@dataclass(frozen=True)
class PreSignSlice:
    architecture: str
    kind: PreSignKind


@dataclass(frozen=True)
class SignedSlice:
    architecture: str
    leaf_authority: str
    cdhash: str
    timestamp: str
    designated_requirement: str


@dataclass(frozen=True)
class SigningReceipt:
    input_sha256: str
    signed_sha256: str
    identity_sha1: str
    identifier: str
    team_id: str
    architectures: tuple[str, ...]
    slices: tuple[SignedSlice, ...]


@dataclass(frozen=True)
class SignatureVerificationReceipt:
    signed_sha256: str
    identity_sha1: str
    identifier: str
    team_id: str
    architectures: tuple[str, ...]
    slices: tuple[SignedSlice, ...]


@dataclass(frozen=True)
class NotarySubmission:
    """Safe submission facts; the keychain profile and credentials are not stored."""

    job_id: str
    archive_sha256: str


@dataclass(frozen=True)
class NotaryStatus:
    job_id: str
    status: str


@dataclass(frozen=True)
class NotaryLog:
    job_id: str
    status: str
    archive_sha256: str
    status_code: int
    issue_count: int


@dataclass(frozen=True)
class NotarizationReceipt:
    job_id: str
    archive_sha256: str
    status: str


def _absolute_path(path: Path | str) -> Path:
    return release_files.absolute_path(path)


def validate_regular_single_link(
    path: Path | str, *, label: str
) -> release_files.FileSnapshot:
    """Require one non-empty ordinary file with no symlink or hard-link alias."""

    try:
        return release_files.inspect_regular_file(
            path,
            label=label,
            require_single_link=True,
        )
    except release_files.ReleaseFileError as error:
        raise InputValidationError(str(error)) from error


def sha256_file(path: Path | str, *, chunk_size: int = 1024 * 1024) -> str:
    """Stream a stable ordinary single-link file into SHA-256."""

    try:
        return release_files.sha256_file(
            path,
            label="hashed file",
            require_single_link=True,
            chunk_size=chunk_size,
        )
    except release_files.ReleaseFileError as error:
        raise InputValidationError(str(error)) from error


def _validate_identity_sha1(identity_sha1: str) -> None:
    if (
        not isinstance(identity_sha1, str)
        or SHA1_IDENTITY.fullmatch(identity_sha1) is None
    ):
        raise InputValidationError(
            "Developer ID identity must be an exact 40-character uppercase SHA-1 fingerprint"
        )


def _validate_team_id(team_id: str) -> None:
    if not isinstance(team_id, str) or TEAM_ID.fullmatch(team_id) is None:
        raise InputValidationError(
            "Developer team ID must contain 10 uppercase letters or digits"
        )


def _validate_identifier(identifier: str) -> None:
    if (
        not isinstance(identifier, str)
        or IDENTIFIER.fullmatch(identifier) is None
        or "." not in identifier
        or ".." in identifier
    ):
        raise InputValidationError(f"invalid macOS signing identifier: {identifier!r}")


def _validate_profile(profile: str) -> None:
    if (
        not isinstance(profile, str)
        or not profile
        or "\x00" in profile
        or "\n" in profile
        or "\r" in profile
    ):
        raise InputValidationError("notary keychain profile must be a non-empty single line")


def _validate_job_id(job_id: str) -> str:
    try:
        parsed = uuid.UUID(job_id)
    except (ValueError, AttributeError) as error:
        raise NotarizationValidationError(f"invalid notarization job ID: {job_id!r}") from error
    canonical = str(parsed)
    if job_id.lower() != canonical:
        raise NotarizationValidationError(f"non-canonical notarization job ID: {job_id!r}")
    return canonical


def _validate_sha256(value: str, *, context: str) -> str:
    if not isinstance(value, str):
        raise NotarizationValidationError(f"{context} is not a SHA-256 digest")
    normalized = value.lower()
    if SHA256_DIGEST.fullmatch(normalized) is None:
        raise NotarizationValidationError(f"{context} is not a SHA-256 digest")
    return normalized


def _invoke(runner: CommandRunner, argv: Sequence[str]) -> CommandResult:
    command = tuple(argv)
    try:
        result = runner.run(command)
    except MacSigningError:
        raise
    except Exception as error:
        raise CommandExecutionError(
            f"command runner failed for {command[0]!r}: {error}"
        ) from error
    if not isinstance(result, CommandResult):
        raise CommandExecutionError("command runner returned a non-CommandResult value")
    if result.argv != command:
        raise CommandExecutionError(
            f"command runner result argv differs from the requested command: {command!r}"
        )
    if isinstance(result.returncode, bool) or not isinstance(result.returncode, int):
        raise CommandExecutionError("command runner returned a non-integer return code")
    if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
        raise CommandExecutionError("command runner returned non-text output")
    try:
        output_bytes = len(result.stdout.encode("utf-8")) + len(
            result.stderr.encode("utf-8")
        )
    except UnicodeError as error:
        raise CommandExecutionError("command runner returned invalid UTF-8 text") from error
    if output_bytes > MAX_COMMAND_OUTPUT_BYTES:
        raise CommandExecutionError(
            f"command output exceeded {MAX_COMMAND_OUTPUT_BYTES} bytes"
        )
    return result


def _diagnostic(result: CommandResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
    return detail[:2000]


def _reject_warning(result: CommandResult, *, context: str) -> None:
    combined = f"{result.stdout}\n{result.stderr}"
    if re.search(r"(?im)^\s*warning(?:\s|:)", combined):
        raise CommandExecutionError(f"{context} emitted a warning: {_diagnostic(result)}")


def _require_success(result: CommandResult, *, context: str) -> CommandResult:
    if result.returncode != 0:
        raise CommandExecutionError(
            f"{context} failed with exit code {result.returncode}: {_diagnostic(result)}"
        )
    _reject_warning(result, context=context)
    return result


def _combined_output(result: CommandResult) -> str:
    return "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)


def _field_values(output: str, key: str) -> tuple[str, ...]:
    prefix = f"{key}="
    return tuple(
        line[len(prefix) :].strip()
        for line in output.splitlines()
        if line.startswith(prefix)
    )


def _single_field(output: str, key: str, *, context: str) -> str:
    values = _field_values(output, key)
    if len(values) != 1 or not values[0]:
        raise SignatureValidationError(
            f"{context} must report exactly one non-empty {key}, found {values!r}"
        )
    return values[0]


def _code_directory_flags(output: str, *, context: str) -> frozenset[str]:
    matches = CODE_DIRECTORY_FLAGS.findall(output)
    if len(matches) != 1:
        raise SignatureValidationError(
            f"{context} must report exactly one CodeDirectory flags field"
        )
    flags = frozenset(flag.strip() for flag in matches[0].split(",") if flag.strip())
    if not flags:
        raise SignatureValidationError(f"{context} reported empty CodeDirectory flags")
    return flags


def architectures(binary: Path | str, runner: CommandRunner) -> tuple[str, ...]:
    """Return the exact supported Mach-O architecture set."""

    path = _absolute_path(binary)
    validate_regular_single_link(path, label="macOS signing input")
    result = _require_success(
        _invoke(runner, (XCRUN, "lipo", "-archs", str(path))),
        context="Mach-O architecture inspection",
    )
    values = tuple(result.stdout.split())
    if not values or len(set(values)) != len(values) or not set(values) <= ARCHITECTURES:
        raise SignatureValidationError(
            f"unsupported or ambiguous Mach-O architecture list: {values!r}"
        )
    return values


def _expected_architecture_set(
    expected_architectures: Sequence[str],
) -> frozenset[str]:
    if isinstance(expected_architectures, str):
        raise InputValidationError(
            "expected macOS architectures must be a sequence of architecture names"
        )
    try:
        values = tuple(expected_architectures)
    except TypeError as error:
        raise InputValidationError(
            "expected macOS architectures must be a sequence of architecture names"
        ) from error
    if (
        not values
        or len(set(values)) != len(values)
        or not set(values) <= ARCHITECTURES
    ):
        raise InputValidationError(
            f"invalid expected macOS architecture set: {values!r}"
        )
    return frozenset(values)


def _require_expected_architectures(
    actual: tuple[str, ...], expected_architectures: Sequence[str]
) -> None:
    expected = _expected_architecture_set(expected_architectures)
    if frozenset(actual) != expected:
        raise SignatureValidationError(
            f"Mach-O architectures are {sorted(actual)}; expected {sorted(expected)}"
        )


def _display_signature(
    binary: Path, architecture: str, runner: CommandRunner
) -> CommandResult:
    return _invoke(
        runner,
        (CODESIGN, "--display", "--verbose=4", "--arch", architecture, str(binary)),
    )


def _parse_pre_sign_slice(
    result: CommandResult, architecture: str
) -> PreSignSlice | tuple[str, str]:
    output = _combined_output(result)
    context = f"pre-sign {architecture} slice"
    if result.returncode != 0:
        if "code object is not signed at all" in output:
            return PreSignSlice(architecture, PreSignKind.UNSIGNED)
        raise CommandExecutionError(
            f"{context} inspection failed with exit code {result.returncode}: "
            f"{_diagnostic(result)}"
        )
    _reject_warning(result, context=context)
    signature = _field_values(output, "Signature")
    authorities = _field_values(output, "Authority")
    team = _field_values(output, "TeamIdentifier")
    flags = _code_directory_flags(output, context=context)
    timestamp = _field_values(output, "Timestamp")
    if signature == ("adhoc",) and not authorities and team in {(), ("not set",)}:
        if flags not in {
            frozenset({"adhoc"}),
            frozenset({"adhoc", "linker-signed"}),
        }:
            raise SignatureValidationError(
                f"{context} is not a pure ad-hoc/linker-signed signature: {sorted(flags)}"
            )
        if timestamp:
            raise SignatureValidationError(f"{context} ad-hoc signature has a timestamp")
        if _single_field(output, "Internal requirements", context=context) != "none":
            raise SignatureValidationError(
                f"{context} ad-hoc signature contains internal requirements"
            )
        _single_field(output, "Identifier", context=context)
        cdhash = _single_field(output, "CDHash", context=context)
        if CDHASH.fullmatch(cdhash) is None:
            raise SignatureValidationError(f"{context} reported an invalid CDHash")
        return PreSignSlice(architecture, PreSignKind.AD_HOC)
    leaf = authorities[0] if authorities else ""
    return (architecture, leaf)


def inspect_pre_signatures(
    binary: Path | str,
    expected_architectures: Sequence[str],
    runner: CommandRunner,
) -> tuple[PreSignSlice, ...]:
    """Allow only uniformly unsigned or uniformly pure ad-hoc slices."""

    _expected_architecture_set(expected_architectures)
    path = _absolute_path(binary)
    parsed = tuple(
        _parse_pre_sign_slice(_display_signature(path, architecture, runner), architecture)
        for architecture in expected_architectures
    )
    kinds = {
        item.kind if isinstance(item, PreSignSlice) else "non_ad_hoc" for item in parsed
    }
    if len(kinds) != 1:
        raise SignatureValidationError(
            "universal Mach-O has mixed pre-sign signature states"
        )
    if "non_ad_hoc" in kinds:
        details = tuple(item for item in parsed if not isinstance(item, PreSignSlice))
        raise SignatureValidationError(
            f"refusing to replace an existing non-ad-hoc or Developer ID signature: {details!r}"
        )
    slices = tuple(item for item in parsed if isinstance(item, PreSignSlice))
    for item in slices:
        if item.kind is not PreSignKind.AD_HOC:
            continue
        entitlements = _invoke(
            runner,
            (
                CODESIGN,
                "--display",
                "--entitlements",
                "-",
                "--xml",
                "--arch",
                item.architecture,
                str(path),
            ),
        )
        _validate_zero_entitlements(
            entitlements,
            context=f"pre-sign {item.architecture} slice entitlement display",
        )
    return slices


def developer_id_leaf(
    identity_sha1: str, team_id: str, runner: CommandRunner
) -> str:
    """Resolve the exact keychain identity fingerprint to its Developer ID leaf."""

    _validate_identity_sha1(identity_sha1)
    _validate_team_id(team_id)
    result = _require_success(
        _invoke(runner, (SECURITY, "find-identity", "-v", "-p", "codesigning")),
        context="Developer ID identity lookup",
    )
    matches = [
        label
        for fingerprint, label in IDENTITY_LINE.findall(result.stdout)
        if fingerprint == identity_sha1
    ]
    if len(matches) != 1:
        raise SignatureValidationError(
            f"expected exactly one keychain identity for {identity_sha1}, found {len(matches)}"
        )
    leaf = matches[0]
    if not leaf.startswith("Developer ID Application: ") or not leaf.endswith(
        f" ({team_id})"
    ):
        raise SignatureValidationError(
            f"identity {identity_sha1} is not the expected Developer ID Application leaf"
        )
    return leaf


def _validate_designated_requirement(
    output: str, *, identifier: str, team_id: str, context: str
) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    requirement = " ".join(line for line in lines if not line.startswith("Executable="))
    normalized = re.sub(r"/\*\s*exists\s*\*/", "exists", requirement)
    normalized = re.sub(
        rf'=\s*"{re.escape(team_id)}"',
        f"= {team_id}",
        normalized,
    )
    normalized = " ".join(normalized.split())
    expected = " ".join(
        (
            f'designated => identifier "{identifier}" and anchor apple generic',
            "and certificate 1[field.1.2.840.113635.100.6.2.6] exists",
            "and certificate leaf[field.1.2.840.113635.100.6.1.13] exists",
            f"and certificate leaf[subject.OU] = {team_id}",
        )
    )
    if normalized != expected:
        raise SignatureValidationError(
            f"{context} has an unexpected designated requirement: {requirement!r}"
        )
    return requirement


def _validate_zero_entitlements(result: CommandResult, *, context: str) -> None:
    _require_success(result, context=context)
    if result.stdout.strip():
        raise SignatureValidationError(f"{context} contains unexpected entitlements")
    residual = [
        line
        for line in result.stderr.splitlines()
        if line.strip() and not line.startswith("Executable=")
    ]
    if residual:
        raise SignatureValidationError(
            f"{context} emitted unexpected entitlement output: {residual!r}"
        )


def _parse_signed_slice(
    binary: Path,
    architecture: str,
    expected_leaf: str | None,
    identifier: str,
    team_id: str,
    runner: CommandRunner,
) -> SignedSlice:
    context = f"signed {architecture} slice"
    display = _require_success(
        _display_signature(binary, architecture, runner), context=f"{context} display"
    )
    output = _combined_output(display)
    if _single_field(output, "Identifier", context=context) != identifier:
        raise SignatureValidationError(f"{context} has the wrong signing identifier")
    if _single_field(output, "TeamIdentifier", context=context) != team_id:
        raise SignatureValidationError(f"{context} has the wrong Developer Team ID")
    authorities = _field_values(output, "Authority")
    if not authorities:
        raise SignatureValidationError(f"{context} has the wrong Developer ID leaf")
    leaf = authorities[0]
    if (
        not leaf.startswith("Developer ID Application: ")
        or not leaf.endswith(f" ({team_id})")
        or (expected_leaf is not None and leaf != expected_leaf)
    ):
        raise SignatureValidationError(f"{context} has the wrong Developer ID leaf")
    developer_id_leaves = sum(
        authority.startswith("Developer ID Application: ")
        for authority in authorities
    )
    if developer_id_leaves != 1:
        raise SignatureValidationError(
            f"{context} has an ambiguous Developer ID authority chain"
        )
    flags = _code_directory_flags(output, context=context)
    if flags != frozenset({"runtime"}):
        raise SignatureValidationError(
            f"{context} must contain only the hardened runtime flag, found {sorted(flags)}"
        )
    timestamp = _single_field(output, "Timestamp", context=context)
    if timestamp.lower() in {"none", "not set"}:
        raise SignatureValidationError(f"{context} omitted the secure timestamp")
    cdhash = _single_field(output, "CDHash", context=context)
    if CDHASH.fullmatch(cdhash) is None:
        raise SignatureValidationError(f"{context} reported an invalid CDHash")

    requirements = _require_success(
        _invoke(
            runner,
            (
                CODESIGN,
                "--display",
                "--requirements",
                "-",
                "--arch",
                architecture,
                str(binary),
            ),
        ),
        context=f"{context} designated requirement display",
    )
    designated_requirement = _validate_designated_requirement(
        _combined_output(requirements),
        identifier=identifier,
        team_id=team_id,
        context=context,
    )
    entitlements = _invoke(
        runner,
        (
            CODESIGN,
            "--display",
            "--entitlements",
            "-",
            "--xml",
            "--arch",
            architecture,
            str(binary),
        ),
    )
    _validate_zero_entitlements(entitlements, context=f"{context} entitlement display")
    return SignedSlice(
        architecture=architecture,
        leaf_authority=leaf,
        cdhash=cdhash.lower(),
        timestamp=timestamp,
        designated_requirement=designated_requirement,
    )


def _verify_signed_with_leaf(
    path: Path,
    *,
    expected_architectures: Sequence[str],
    identity_sha1: str,
    identifier: str,
    team_id: str,
    expected_leaf: str | None,
    runner: CommandRunner,
) -> SignatureVerificationReceipt:
    """Verify an existing signature after its exact keychain leaf is resolved."""

    signed_sha256 = sha256_file(path)
    actual_architectures = architectures(path, runner)
    _require_expected_architectures(actual_architectures, expected_architectures)
    _require_success(
        _invoke(
            runner,
            (
                CODESIGN,
                "--verify",
                "--strict",
                "--all-architectures",
                "--verbose=4",
                str(path),
            ),
        ),
        context="strict all-architectures signature verification",
    )
    slices: list[SignedSlice] = []
    for architecture in actual_architectures:
        _require_success(
            _invoke(
                runner,
                (
                    CODESIGN,
                    "--verify",
                    "--strict",
                    "--arch",
                    architecture,
                    f'-R=certificate leaf = H"{identity_sha1}"',
                    str(path),
                ),
            ),
            context=f"signed {architecture} slice Developer ID leaf verification",
        )
        slices.append(
            _parse_signed_slice(
                path,
                architecture,
                expected_leaf,
                identifier,
                team_id,
                runner,
            )
        )
    if sha256_file(path) != signed_sha256:
        raise SignatureValidationError("signed binary changed during signature verification")
    return SignatureVerificationReceipt(
        signed_sha256=signed_sha256,
        identity_sha1=identity_sha1,
        identifier=identifier,
        team_id=team_id,
        architectures=actual_architectures,
        slices=tuple(slices),
    )


def verify_signed(
    binary: Path | str,
    *,
    expected_architectures: Sequence[str],
    identity_sha1: str,
    identifier: str,
    team_id: str,
    runner: CommandRunner,
) -> SignatureVerificationReceipt:
    """Read and prove an existing Developer ID signature without modifying it."""

    _validate_identity_sha1(identity_sha1)
    _validate_identifier(identifier)
    _validate_team_id(team_id)
    _expected_architecture_set(expected_architectures)
    path = _absolute_path(binary)
    initial_snapshot = validate_regular_single_link(path, label="signed macOS binary")
    receipt = _verify_signed_with_leaf(
        path,
        expected_architectures=expected_architectures,
        identity_sha1=identity_sha1,
        identifier=identifier,
        team_id=team_id,
        expected_leaf=None,
        runner=runner,
    )
    if validate_regular_single_link(path, label="signed macOS binary") != initial_snapshot:
        raise SignatureValidationError(
            "signed binary filesystem identity changed during verification"
        )
    return receipt


def sign_and_verify(
    binary: Path | str,
    *,
    expected_architectures: Sequence[str],
    identity_sha1: str,
    identifier: str,
    team_id: str,
    runner: CommandRunner,
) -> SigningReceipt:
    """Replace an unsigned/ad-hoc signature and prove the final signature per slice."""

    _validate_identity_sha1(identity_sha1)
    _validate_identifier(identifier)
    _validate_team_id(team_id)
    _expected_architecture_set(expected_architectures)
    path = _absolute_path(binary)
    input_snapshot = validate_regular_single_link(path, label="macOS signing input")
    input_sha256 = sha256_file(path)
    actual_architectures = architectures(path, runner)
    _require_expected_architectures(actual_architectures, expected_architectures)
    inspect_pre_signatures(path, actual_architectures, runner)
    expected_leaf = developer_id_leaf(identity_sha1, team_id, runner)
    pre_sign_snapshot = validate_regular_single_link(
        path,
        label="macOS signing input",
    )
    pre_sign_sha256 = sha256_file(path)
    if pre_sign_snapshot != input_snapshot or pre_sign_sha256 != input_sha256:
        raise SignatureValidationError(
            "macOS signing input changed during pre-sign verification"
        )

    sign_argv = (
        CODESIGN,
        "--force",
        "--sign",
        identity_sha1,
        "--identifier",
        identifier,
        "--options",
        "runtime",
        "--timestamp",
        str(path),
    )
    _require_success(_invoke(runner, sign_argv), context="Developer ID signing")
    validate_regular_single_link(path, label="signed macOS binary")
    verification = _verify_signed_with_leaf(
        path,
        expected_architectures=expected_architectures,
        identity_sha1=identity_sha1,
        identifier=identifier,
        team_id=team_id,
        expected_leaf=expected_leaf,
        runner=runner,
    )
    if verification.signed_sha256 == input_sha256:
        raise SignatureValidationError("Developer ID signing did not change the binary bytes")
    return SigningReceipt(
        input_sha256=input_sha256,
        signed_sha256=verification.signed_sha256,
        identity_sha1=identity_sha1,
        identifier=identifier,
        team_id=team_id,
        architectures=verification.architectures,
        slices=verification.slices,
    )


def _strict_json(text: str, *, context: str) -> dict[str, Any]:
    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise NotarizationValidationError(
                    f"{context} JSON repeats object key {key!r}"
                )
            result[key] = value
        return result

    def reject_nonstandard_constant(value: str) -> None:
        raise NotarizationValidationError(
            f"{context} JSON contains non-standard constant {value!r}"
        )

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_nonstandard_constant,
        )
    except NotarizationValidationError:
        raise
    except (json.JSONDecodeError, RecursionError) as error:
        raise NotarizationValidationError(f"{context} returned invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise NotarizationValidationError(f"{context} JSON root must be an object")
    return value


def _json_command(
    runner: CommandRunner,
    argv: tuple[str, ...],
    *,
    context: str,
) -> dict[str, Any]:
    result = _require_success(_invoke(runner, argv), context=context)
    if result.stderr.strip():
        raise CommandExecutionError(f"{context} wrote unexpected stderr: {result.stderr.strip()}")
    return _strict_json(result.stdout, context=context)


def _archive_command(
    archive: Path,
    expected_sha256: str,
    runner: CommandRunner,
    argv: tuple[str, ...],
    *,
    context: str,
) -> dict[str, Any]:
    expected = _validate_sha256(expected_sha256, context="expected archive SHA-256")
    if sha256_file(archive) != expected:
        raise NotarizationValidationError(f"{context} archive SHA-256 changed before command")
    try:
        response = _json_command(runner, argv, context=context)
    except Exception as error:
        if sha256_file(archive) != expected:
            raise NotarizationValidationError(
                f"{context} archive SHA-256 changed while the command failed"
            ) from error
        raise
    if sha256_file(archive) != expected:
        raise NotarizationValidationError(f"{context} archive SHA-256 changed after command")
    return response


def _response_job_id(response: dict[str, Any], field: str, *, context: str) -> str:
    value = response.get(field)
    if not isinstance(value, str):
        raise NotarizationValidationError(f"{context} omitted string field {field!r}")
    return _validate_job_id(value)


def _response_status(response: dict[str, Any], *, context: str) -> str:
    status = response.get("status")
    if status not in {"Accepted", "Invalid", "In Progress"}:
        raise NotarizationValidationError(
            f"{context} returned unsupported notarization status: {status!r}"
        )
    return status


def submit_notarization(
    archive: Path | str,
    *,
    keychain_profile: str,
    runner: CommandRunner,
) -> NotarySubmission:
    """Submit exact archive bytes without waiting; a process failure is outcome-unknown."""

    _validate_profile(keychain_profile)
    path = _absolute_path(archive)
    validate_regular_single_link(path, label="notarization archive")
    archive_sha256 = sha256_file(path)
    argv = (
        XCRUN,
        "notarytool",
        "submit",
        str(path),
        "--keychain-profile",
        keychain_profile,
        "--no-wait",
        "--no-progress",
        "--output-format",
        "json",
    )
    try:
        response = _archive_command(
            path,
            archive_sha256,
            runner,
            argv,
            context="notary submission",
        )
        job_id = _response_job_id(response, "id", context="notary submission")
    except (CommandExecutionError, NotarizationValidationError) as error:
        raise SubmissionOutcomeUnknown(
            "notary submission did not return a validated job ID; reconcile before retrying"
        ) from error
    return NotarySubmission(job_id=job_id, archive_sha256=archive_sha256)


def wait_for_notarization(
    archive: Path | str,
    submission: NotarySubmission,
    *,
    keychain_profile: str,
    timeout: str,
    runner: CommandRunner,
) -> NotaryStatus:
    _validate_profile(keychain_profile)
    if not isinstance(timeout, str) or TIMEOUT.fullmatch(timeout) is None:
        raise InputValidationError(f"invalid notary wait timeout: {timeout!r}")
    job_id = _validate_job_id(submission.job_id)
    path = _absolute_path(archive)
    response = _archive_command(
        path,
        submission.archive_sha256,
        runner,
        (
            XCRUN,
            "notarytool",
            "wait",
            job_id,
            "--keychain-profile",
            keychain_profile,
            "--timeout",
            timeout,
            "--no-progress",
            "--output-format",
            "json",
        ),
        context="notary wait",
    )
    returned_id = _response_job_id(response, "id", context="notary wait")
    if returned_id != job_id:
        raise NotarizationValidationError("notary wait returned a different job ID")
    return NotaryStatus(
        job_id=returned_id,
        status=_response_status(response, context="notary wait"),
    )


def notarization_info(
    archive: Path | str,
    submission: NotarySubmission,
    *,
    keychain_profile: str,
    runner: CommandRunner,
) -> NotaryStatus:
    _validate_profile(keychain_profile)
    job_id = _validate_job_id(submission.job_id)
    path = _absolute_path(archive)
    response = _archive_command(
        path,
        submission.archive_sha256,
        runner,
        (
            XCRUN,
            "notarytool",
            "info",
            job_id,
            "--keychain-profile",
            keychain_profile,
            "--no-progress",
            "--output-format",
            "json",
        ),
        context="notary info",
    )
    returned_id = _response_job_id(response, "id", context="notary info")
    if returned_id != job_id:
        raise NotarizationValidationError("notary info returned a different job ID")
    return NotaryStatus(
        job_id=returned_id,
        status=_response_status(response, context="notary info"),
    )


def notarization_log(
    archive: Path | str,
    submission: NotarySubmission,
    *,
    keychain_profile: str,
    runner: CommandRunner,
) -> NotaryLog:
    _validate_profile(keychain_profile)
    job_id = _validate_job_id(submission.job_id)
    path = _absolute_path(archive)
    response = _archive_command(
        path,
        submission.archive_sha256,
        runner,
        (
            XCRUN,
            "notarytool",
            "log",
            job_id,
            "--keychain-profile",
            keychain_profile,
        ),
        context="notary log",
    )
    returned_id = _response_job_id(response, "jobId", context="notary log")
    if returned_id != job_id:
        raise NotarizationValidationError("notary log returned a different job ID")
    status = _response_status(response, context="notary log")
    digest = response.get("sha256")
    if not isinstance(digest, str):
        raise NotarizationValidationError("notary log omitted string field 'sha256'")
    digest = _validate_sha256(digest, context="notary log archive SHA-256")
    if digest != submission.archive_sha256:
        raise NotarizationValidationError("notary log archive SHA-256 differs from submitted bytes")
    status_code = response.get("statusCode")
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise NotarizationValidationError("notary log statusCode must be an integer")
    if "issues" not in response:
        raise NotarizationValidationError("notary log omitted field 'issues'")
    issues = response["issues"]
    if issues is None:
        issue_count = 0
    elif isinstance(issues, list) and all(isinstance(issue, dict) for issue in issues):
        issue_count = len(issues)
    else:
        raise NotarizationValidationError("notary log issues must be null or an array of objects")
    return NotaryLog(
        job_id=returned_id,
        status=status,
        archive_sha256=digest,
        status_code=status_code,
        issue_count=issue_count,
    )


def verify_accepted_notarization(
    archive: Path | str,
    submission: NotarySubmission,
    *,
    keychain_profile: str,
    timeout: str,
    runner: CommandRunner,
) -> NotarizationReceipt:
    """Wait, independently query, and validate an Accepted issue-free notary job."""

    waited = wait_for_notarization(
        archive,
        submission,
        keychain_profile=keychain_profile,
        timeout=timeout,
        runner=runner,
    )
    info = notarization_info(
        archive,
        submission,
        keychain_profile=keychain_profile,
        runner=runner,
    )
    log = notarization_log(
        archive,
        submission,
        keychain_profile=keychain_profile,
        runner=runner,
    )
    if waited.status != "Accepted" or info.status != "Accepted" or log.status != "Accepted":
        raise NotarizationValidationError(
            "notary wait, info, and log must all report Accepted"
        )
    if log.status_code != 0 or log.issue_count != 0:
        raise NotarizationValidationError(
            "Accepted notary log must have statusCode 0 and no issues"
        )
    if sha256_file(archive) != submission.archive_sha256:
        raise NotarizationValidationError("notarized archive changed after validation")
    return NotarizationReceipt(
        job_id=submission.job_id,
        archive_sha256=submission.archive_sha256,
        status="Accepted",
    )


def check_notarization_ticket(binary: Path | str, runner: CommandRunner) -> None:
    """Require the online notarization ticket for an exact standalone binary."""

    path = _absolute_path(binary)
    validate_regular_single_link(path, label="notarized macOS binary")
    digest = sha256_file(path)
    _require_success(
        _invoke(
            runner,
            (
                CODESIGN,
                "--verify",
                "--verbose=4",
                "-R=notarized",
                "--check-notarization",
                str(path),
            ),
        ),
        context="online notarization ticket verification",
    )
    if sha256_file(path) != digest:
        raise SignatureValidationError(
            "standalone binary changed during notarization ticket verification"
        )
