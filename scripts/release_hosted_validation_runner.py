"""I/O and native execution boundaries for hosted signed-draft validation."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Mapping, Sequence

import macos_signing
import release_artifact_download
import release_draft
import release_files
import release_hosted_validation as hosted
from release_package import expected_archive_members, safe_extract_archive
from release_targets import ReleaseContract


COMMAND_TIMEOUT_SECONDS = 300
MAX_TEXT_FILE_BYTES = 4 * 1024 * 1024
GITHUB_TOKEN_ENVIRONMENT = ("GH_TOKEN", "GITHUB_TOKEN")


class HostedValidationRunnerError(RuntimeError):
    """Hosted validation I/O could not produce trustworthy evidence."""


def read_stable_text(path: Path, *, label: str) -> str:
    chunks: list[bytes] = []
    size = 0
    try:
        with release_files.open_stable_regular_file(
            path,
            label=label,
            require_single_link=True,
        ) as (source, snapshot):
            if snapshot.size > MAX_TEXT_FILE_BYTES:
                raise HostedValidationRunnerError(f"{label} exceeds the size limit")
            while True:
                chunk = source.read(min(64 * 1024, MAX_TEXT_FILE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_TEXT_FILE_BYTES:
                    raise HostedValidationRunnerError(f"{label} exceeds the size limit")
    except release_files.ReleaseFileError as error:
        raise HostedValidationRunnerError(f"cannot read stable {label}: {error}") from error
    try:
        return b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise HostedValidationRunnerError(f"{label} is not valid UTF-8") from error


def write_json_no_replace(
    path: Path,
    value: Mapping[str, object],
    *,
    mode: int,
) -> None:
    try:
        payload = hosted.canonical_json(value).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise HostedValidationRunnerError(
            f"JSON output is not serializable: {error}"
        ) from error
    if len(payload) > MAX_TEXT_FILE_BYTES:
        raise HostedValidationRunnerError("JSON output exceeds the size limit")
    try:
        with path.open("xb") as output:
            if hasattr(os, "fchmod"):
                os.fchmod(output.fileno(), mode)
            offset = 0
            while offset < len(payload):
                written = output.write(payload[offset:])
                if written is None or written <= 0:
                    raise HostedValidationRunnerError(
                        "JSON output write made no progress"
                    )
                offset += written
            output.flush()
            os.fsync(output.fileno())
        if not hasattr(os, "fchmod"):
            path.chmod(mode)
    except FileExistsError as error:
        raise HostedValidationRunnerError(
            f"refusing to replace JSON output: {path}"
        ) from error
    except HostedValidationRunnerError:
        raise
    except OSError as error:
        raise HostedValidationRunnerError(
            f"cannot write JSON output {path}: {error}"
        ) from error


def append_github_output(path: Path, name: str, value: str) -> None:
    if not name.isidentifier() or "\n" in value or "\r" in value:
        raise HostedValidationRunnerError("unsafe GitHub output value")
    try:
        with path.open("a", encoding="utf-8", errors="strict") as output:
            output.write(f"{name}={value}\n")
            output.flush()
            os.fsync(output.fileno())
    except OSError as error:
        raise HostedValidationRunnerError(
            f"cannot append GitHub output: {error}"
        ) from error


def run_required(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> None:
    try:
        completed = subprocess.run(
            tuple(command),
            check=False,
            cwd=cwd,
            env=None if environment is None else dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise HostedValidationRunnerError(
            f"failed to execute validation command {command[0]!r}: {error}"
        ) from error
    stdout_size = len(completed.stdout.encode("utf-8", errors="replace"))
    stderr_size = len(completed.stderr.encode("utf-8", errors="replace"))
    if stdout_size + stderr_size > MAX_TEXT_FILE_BYTES:
        raise HostedValidationRunnerError(
            f"validation command output exceeded the limit: {command[0]}"
        )
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise HostedValidationRunnerError(
            f"validation command failed with exit {completed.returncode}: "
            f"{command[0]}: {diagnostic[:1000]}"
        )
    if completed.stdout.strip() or completed.stderr.strip():
        raise HostedValidationRunnerError(
            f"validation command wrote unexpected output: {command[0]}"
        )


def load_plan(path: Path, contract: ReleaseContract) -> hosted.HostedValidationPlan:
    return hosted.parse_plan(
        read_stable_text(path, label="hosted validation plan"),
        contract=contract,
    )


def find_spec(
    plan: hosted.HostedValidationPlan,
    validation_id: str,
) -> hosted.ValidationSpec:
    matches = [spec for spec in plan.validations if spec.validation_id == validation_id]
    if len(matches) != 1:
        raise HostedValidationRunnerError(
            f"validation ID is not unique in the exact plan: {validation_id!r}"
        )
    return matches[0]


def build_plan(
    *,
    repository: str,
    source_workflow_id: int,
    source_run_id: int,
    source_run_attempt: int,
    source_run_json: str,
    validation_workflow_id: int,
    validation_run_id: int,
    validation_run_attempt: int,
    tag: str,
    head_sha: str,
    release_id: str,
    release_database_id: int,
    release_json: str,
    release_notes: str,
    identity_sha1: str,
    contract: ReleaseContract,
) -> hosted.HostedValidationPlan:
    try:
        source_run = release_draft.parse_workflow_run(
            source_run_json,
            expected_workflow_id=source_workflow_id,
            expected_run_id=source_run_id,
            expected_attempt=source_run_attempt,
            expected_tag=tag,
            expected_head_sha=head_sha,
        )
    except release_draft.ReleaseDraftError as error:
        raise HostedValidationRunnerError(str(error)) from error
    release = hosted.parse_draft_release(release_json, expected_tag=tag)
    if (
        release.release_id != release_id
        or release.release_database_id != release_database_id
    ):
        raise HostedValidationRunnerError(
            "live draft release identity differs from the dispatched validation input"
        )
    validation_run = release_draft.WorkflowRun(
        workflow_id=validation_workflow_id,
        run_id=validation_run_id,
        attempt=validation_run_attempt,
        tag=tag,
        head_sha=head_sha,
    )
    return hosted.create_plan(
        repository=repository,
        source_run=source_run,
        validation_run=validation_run,
        release=release,
        release_notes=release_notes,
        identity_sha1=identity_sha1,
        contract=contract,
    )


def resolve_gh() -> Path:
    executable = shutil.which("gh")
    if executable is None or not Path(executable).is_absolute():
        raise HostedValidationRunnerError(
            "gh must resolve to an absolute executable path"
        )
    try:
        resolved = Path(executable).resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError) as error:
        raise HostedValidationRunnerError(f"cannot resolve gh executable: {error}") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise HostedValidationRunnerError("gh is not an executable regular file")
    return resolved


def _require_empty_private_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise HostedValidationRunnerError(
            f"cannot create validation download directory: {error}"
        ) from error
    try:
        metadata = os.lstat(directory)
        entries = tuple(directory.iterdir())
    except OSError as error:
        raise HostedValidationRunnerError(
            f"cannot inspect validation download directory: {error}"
        ) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or entries
    ):
        raise HostedValidationRunnerError(
            "validation download directory must be an empty ordinary 0700 directory"
        )


def download_validation_asset(
    plan: hosted.HostedValidationPlan,
    spec: hosted.ValidationSpec,
    output_directory: Path,
) -> Path:
    if find_spec(plan, spec.validation_id) != spec:
        raise HostedValidationRunnerError("download job differs from the exact plan")
    _require_empty_private_directory(output_directory)
    destination = output_directory / spec.archive_name
    command = (
        str(resolve_gh()),
        "api",
        "--hostname",
        "github.com",
        "-H",
        "Accept: application/octet-stream",
        f"repos/{plan.repository}/releases/assets/{spec.archive_asset_id}",
    )
    try:
        release_artifact_download.download_command_to_file(
            command,
            destination,
            expected_size=spec.archive_size,
            label=f"signed draft asset {spec.archive_name}",
            timeout_seconds=COMMAND_TIMEOUT_SECONDS,
        )
        digest = release_files.sha256_file(
            destination,
            label=f"signed draft asset {spec.archive_name}",
            require_single_link=True,
        )
    except (
        release_artifact_download.ArtifactDownloadError,
        release_files.ReleaseFileError,
    ) as error:
        raise HostedValidationRunnerError(str(error)) from error
    if digest != spec.archive_sha256:
        raise HostedValidationRunnerError(
            f"downloaded signed draft asset digest differs: {spec.archive_name}"
        )
    return destination


def require_no_github_token(environment: Mapping[str, str]) -> None:
    present = sorted(name for name in GITHUB_TOKEN_ENVIRONMENT if environment.get(name))
    if present:
        raise HostedValidationRunnerError(
            f"candidate validation refuses GitHub token environment variables: {present}"
        )


def candidate_environment() -> dict[str, str]:
    """Return the minimal environment inherited by the untrusted MCP process."""

    return {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }


def runtime_architecture() -> str:
    value = platform.machine().lower()
    normalized = {"aarch64": "arm64", "amd64": "x86_64"}.get(value, value)
    if normalized not in hosted.ARCHITECTURES:
        raise HostedValidationRunnerError(
            f"unsupported hosted macOS architecture: {value}"
        )
    return normalized


def validate_target(
    *,
    plugin_root: Path,
    plan: hosted.HostedValidationPlan,
    spec: hosted.ValidationSpec,
    archive: Path,
    contract: ReleaseContract,
    environment: Mapping[str, str] = os.environ,
) -> hosted.ValidationResult:
    require_no_github_token(environment)
    if find_spec(plan, spec.validation_id) != spec:
        raise HostedValidationRunnerError("validation job differs from the exact plan")
    if sys.platform != "darwin":
        raise HostedValidationRunnerError("signed draft target validation requires macOS")
    if runtime_architecture() != spec.runtime_architecture:
        raise HostedValidationRunnerError(
            "hosted runner architecture differs from the validation plan"
        )
    try:
        target = contract.target(spec.target_id)
    except RuntimeError as error:
        raise HostedValidationRunnerError(str(error)) from error
    if (
        target.release_filename(plan.release.tag) != spec.archive_name
        or target.archive_format != spec.archive_format
        or target.binary_name != spec.binary_name
        or target.macos_architectures() != spec.expected_architectures
    ):
        raise HostedValidationRunnerError(
            "validation job differs from the shared release target contract"
        )
    archive = Path(os.path.abspath(archive))
    if archive.name != spec.archive_name:
        raise HostedValidationRunnerError(
            "signed draft archive basename differs from the plan"
        )
    try:
        archive_snapshot = release_files.inspect_regular_file(
            archive,
            label=f"signed draft archive {spec.archive_name}",
            require_single_link=True,
        )
        archive_sha256 = release_files.sha256_file(
            archive,
            label=f"signed draft archive {spec.archive_name}",
            require_single_link=True,
        )
    except release_files.ReleaseFileError as error:
        raise HostedValidationRunnerError(str(error)) from error
    if (
        archive_snapshot.size != spec.archive_size
        or archive_sha256 != spec.archive_sha256
    ):
        raise HostedValidationRunnerError("signed draft archive bytes differ from the plan")

    with tempfile.TemporaryDirectory(prefix="signed-draft-validation-") as temporary:
        extraction_root = Path(temporary)
        try:
            package = safe_extract_archive(
                archive,
                spec.archive_format,
                extraction_root,
                expected_archive_members(spec.binary_name),
            )
        except (OSError, RuntimeError) as error:
            raise HostedValidationRunnerError(
                f"cannot safely extract signed draft archive: {error}"
            ) from error
        binary = package / "bin" / spec.binary_name
        run_required(
            (
                sys.executable,
                str(plugin_root / "scripts" / "check-release-binary.py"),
                "--plugin-root",
                str(plugin_root),
                "--target",
                spec.target_id,
                "--binary",
                str(binary),
            ),
            cwd=plugin_root,
        )
        runner = macos_signing.SubprocessRunner(
            timeout_seconds=COMMAND_TIMEOUT_SECONDS
        )
        try:
            signature = macos_signing.verify_signed(
                binary,
                expected_architectures=spec.expected_architectures,
                identity_sha1=plan.identity_sha1,
                identifier=contract.macos_signing.code_identifier,
                team_id=contract.macos_signing.team_id,
                runner=runner,
            )
            macos_signing.check_notarization_ticket(binary, runner)
        except macos_signing.MacSigningError as error:
            raise HostedValidationRunnerError(str(error)) from error
        run_required(
            (
                sys.executable,
                str(plugin_root / "scripts" / "smoke-installed-plugin.py"),
                str(package),
            ),
            cwd=plugin_root,
            environment=candidate_environment(),
        )
        try:
            final_binary_sha256 = release_files.sha256_file(
                binary,
                label=f"smoke-tested signed binary {spec.target_id}",
                require_single_link=True,
            )
            final_archive_sha256 = release_files.sha256_file(
                archive,
                label=f"smoke-tested signed archive {spec.archive_name}",
                require_single_link=True,
            )
        except release_files.ReleaseFileError as error:
            raise HostedValidationRunnerError(str(error)) from error
        if final_binary_sha256 != signature.signed_sha256:
            raise HostedValidationRunnerError(
                "candidate binary changed during MCP smoke test"
            )
        if final_archive_sha256 != spec.archive_sha256:
            raise HostedValidationRunnerError(
                "signed archive changed during hosted validation"
            )
        return hosted.ValidationResult(
            **asdict(spec),
            binary_sha256=signature.signed_sha256,
            identity_sha1=signature.identity_sha1,
            identifier=signature.identifier,
            team_id=signature.team_id,
            architectures=signature.architectures,
            signature_valid=True,
            notarization_ticket_valid=True,
            mcp_smoke_valid=True,
        )


def load_exact_results(
    directory: Path,
    plan: hosted.HostedValidationPlan,
) -> tuple[hosted.ValidationResult, ...]:
    expected = {f"{spec.validation_id}.json" for spec in plan.validations}
    try:
        entries = tuple(directory.iterdir())
    except OSError as error:
        raise HostedValidationRunnerError(
            f"cannot inspect validation results: {error}"
        ) from error
    actual = {entry.name for entry in entries}
    if actual != expected:
        raise HostedValidationRunnerError(
            "hosted validation result file set is not exact; "
            f"missing={sorted(expected - actual)}; extra={sorted(actual - expected)}"
        )
    results = []
    for name in sorted(expected):
        results.append(
            hosted.parse_validation_result(
                read_stable_text(
                    directory / name,
                    label=f"hosted validation result {name}",
                )
            )
        )
    return tuple(results)
