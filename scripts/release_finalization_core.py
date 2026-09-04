"""Shared finalization types, bounded command runners, and receipt primitives."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Literal, Sequence, cast
import uuid

import macos_signing
from release_attempt import ReleaseBinding, read_receipt, write_receipt_no_replace
from release_files import absolute_path
from release_targets import ReleaseContract, ReleaseTarget


MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 300
MACOS_FAMILIES = frozenset({"macos", "macos_universal2"})
FINALIZATION_PHASES = ("prepare", "notarize", "stage", "publish")
FinalizationPhase = Literal["prepare", "notarize", "stage", "publish"]


class FinalizationError(RuntimeError):
    """The local release finalizer cannot safely advance."""


class ExternalMutationOutcomeUnknown(FinalizationError):
    """An external mutation requires read-only reconciliation before retry."""


def validate_finalization_phase(value: str) -> FinalizationPhase:
    if value not in FINALIZATION_PHASES:
        expected = ", ".join(FINALIZATION_PHASES)
        raise FinalizationError(
            f"unsupported finalization stop phase {value!r}; expected one of: {expected}"
        )
    return cast(FinalizationPhase, value)


@dataclass(frozen=True)
class HostedValidationInput:
    workflow_id: int
    run_id: int
    run_attempt: int
    receipt_artifact_id: int


@dataclass(frozen=True)
class FinalizationOptions:
    plugin_root: Path
    repository: str
    binding: ReleaseBinding
    identity_sha1: str
    notary_profile: str
    attempt_root: Path
    stop_after: FinalizationPhase
    notary_timeout: str
    adopted_submissions: dict[str, str]
    reconcile_github_upload: bool
    reconcile_github_publish: bool
    hosted_validation: HostedValidationInput | None = None


def validate_attempt_root_location(plugin_root: Path, attempt_root: Path) -> Path:
    """Return an absolute attempt path that is outside the source checkout."""

    try:
        checkout = plugin_root.resolve(strict=True)
        candidate = attempt_root.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise FinalizationError(
            f"cannot resolve release attempt and checkout paths: {error}"
        ) from error
    if candidate == checkout or checkout in candidate.parents:
        raise FinalizationError(
            "release attempt root must be outside the source checkout"
        )
    return absolute_path(attempt_root)


def command_result(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            tuple(command),
            check=False,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise FinalizationError(f"failed to execute {command[0]!r}: {error}") from error
    for stream_name, value in (("stdout", result.stdout), ("stderr", result.stderr)):
        if len(value.encode("utf-8", errors="replace")) > MAX_COMMAND_OUTPUT_BYTES:
            raise FinalizationError(
                f"command {command[0]!r} {stream_name} exceeded the size limit"
            )
    return result


def required_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    allow_stdout: bool = True,
) -> str:
    result = command_result(command, cwd=cwd)
    if result.returncode != 0:
        diagnostic = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise FinalizationError(
            f"command failed with exit {result.returncode}: {' '.join(command)}: "
            f"{diagnostic[:1000]}"
        )
    if result.stderr.strip():
        raise FinalizationError(
            f"command wrote unexpected stderr: {' '.join(command)}: "
            f"{result.stderr.strip()[:1000]}"
        )
    if not allow_stdout and result.stdout.strip():
        raise FinalizationError(f"command wrote unexpected stdout: {' '.join(command)}")
    return result.stdout


def check_release_binary(
    plugin_root: Path,
    target: ReleaseTarget,
    binary: Path,
) -> None:
    required_command(
        (
            sys.executable,
            str(plugin_root / "scripts" / "check-release-binary.py"),
            "--plugin-root",
            str(plugin_root),
            "--target",
            target.id,
            "--binary",
            str(binary),
        ),
        cwd=plugin_root,
        allow_stdout=False,
    )


class GitHubRunner:
    """Shell-free runner compatible with release_draft's injected boundary."""

    def __call__(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return command_result(command)


class AuditedMacRunner:
    """Persist private intent/result receipts around every Apple command."""

    def __init__(self, directory: Path, timeout_seconds: float = 3600) -> None:
        self.directory = directory
        self.runner = macos_signing.SubprocessRunner(timeout_seconds=timeout_seconds)

    def run(self, argv: tuple[str, ...]) -> macos_signing.CommandResult:
        command_id = uuid.uuid4().hex
        write_receipt_no_replace(
            self.directory,
            f"command-{command_id}-intent.json",
            {"argv": list(argv)},
        )
        try:
            result = self.runner.run(argv)
        except macos_signing.MacSigningError as error:
            write_receipt_no_replace(
                self.directory,
                f"command-{command_id}-failure.json",
                {"argv": list(argv), "error": str(error)},
            )
            raise
        stdout_size = len(result.stdout.encode("utf-8", errors="replace"))
        stderr_size = len(result.stderr.encode("utf-8", errors="replace"))
        if stdout_size + stderr_size > macos_signing.MAX_COMMAND_OUTPUT_BYTES:
            payload: dict[str, Any] = {
                "argv": list(argv),
                "returncode": result.returncode,
                "stdout_bytes": stdout_size,
                "stderr_bytes": stderr_size,
                "output_omitted": True,
            }
        else:
            payload = {
                "argv": list(argv),
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        write_receipt_no_replace(
            self.directory,
            f"command-{command_id}-result.json",
            payload,
        )
        return result


def ensure_receipt(
    root: Path,
    name: str,
    payload: dict[str, Any],
    *,
    create_missing: bool = True,
) -> Path:
    """Publish one receipt or prove the existing receipt is byte-semantically equal."""

    try:
        normalized = json.loads(
            json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
            )
        )
    except (TypeError, ValueError) as error:
        raise FinalizationError(f"receipt payload is not strict JSON: {name}: {error}") from error
    if not isinstance(normalized, dict):
        raise FinalizationError(f"receipt payload root must be an object: {name}")
    path = root / name
    if os.path.lexists(path):
        observed = read_receipt(path)
        if observed != normalized:
            raise FinalizationError(f"existing receipt differs from expected state: {path}")
        return path
    if not create_missing:
        raise FinalizationError(f"required existing receipt is missing: {path}")
    return write_receipt_no_replace(root, name, normalized)


def parse_adopted_submissions(values: Sequence[str]) -> dict[str, str]:
    adopted: dict[str, str] = {}
    for value in values:
        target, separator, job_id = value.partition("=")
        if not separator or not target or not job_id or target in adopted:
            raise FinalizationError(
                "--adopt-submission values must be unique TARGET=NOTARY_UUID pairs"
            )
        try:
            canonical = str(uuid.UUID(job_id))
        except ValueError as error:
            raise FinalizationError(f"invalid adopted notarization UUID: {job_id!r}") from error
        if canonical != job_id.lower():
            raise FinalizationError(f"non-canonical adopted notarization UUID: {job_id!r}")
        adopted[target] = canonical
    return adopted


def archive_paths(
    directory: Path,
    contract: ReleaseContract,
    tag: str,
) -> dict[str, Path]:
    return {
        target.release_filename(tag): directory / target.release_filename(tag)
        for target in contract.targets
    }
