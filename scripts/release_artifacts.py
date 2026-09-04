"""Exact GitHub Actions artifact inventory validation for release finalization."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Sequence

from release_draft import WorkflowRun


MAX_ARTIFACT_JSON_BYTES = 2 * 1024 * 1024
MAX_ARTIFACTS = 64
MAX_ARTIFACT_ARCHIVE_BYTES = 512 * 1024 * 1024
_ARTIFACT_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


class ReleaseArtifactError(RuntimeError):
    """A workflow artifact inventory is not the exact expected candidate set."""


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: int
    name: str
    size_in_bytes: int
    archive_sha256: str
    created_at: str
    updated_at: str
    run_id: int
    head_sha: str
    head_branch: str
    repository_id: int
    head_repository_id: int


def _positive_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReleaseArtifactError(f"{context} must be a positive integer")
    return value


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ReleaseArtifactError(f"{context} must be a bounded non-empty string")
    return value


def _artifact_digest(value: object, context: str) -> str:
    digest = _string(value, context)
    match = _ARTIFACT_DIGEST.fullmatch(digest)
    if match is None:
        raise ReleaseArtifactError(f"{context} must be an exact SHA-256 digest")
    return match.group(1)


def _load_json(payload: str) -> dict[str, Any]:
    if not isinstance(payload, str):
        raise ReleaseArtifactError("artifact inventory must be text")
    try:
        size = len(payload.encode("utf-8", errors="strict"))
    except UnicodeError as error:
        raise ReleaseArtifactError("artifact inventory is not valid UTF-8") from error
    if size <= 0 or size > MAX_ARTIFACT_JSON_BYTES:
        raise ReleaseArtifactError(
            f"artifact inventory size {size} is outside the allowed range"
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseArtifactError(f"artifact inventory repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except ReleaseArtifactError:
        raise
    except json.JSONDecodeError as error:
        raise ReleaseArtifactError(f"artifact inventory is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseArtifactError("artifact inventory root must be an object")
    return value


def parse_artifact_inventory(
    payload: str,
    *,
    run: WorkflowRun,
    expected_names: Sequence[str],
) -> tuple[ArtifactRecord, ...]:
    """Bind the current attempt's exact artifacts to a validated workflow run.

    GitHub may retain artifacts from earlier attempts of the same workflow run.
    Such artifacts are accepted only when their names identify a lower attempt
    for a known target; they are never returned to the caller or downloaded.
    """

    expected = tuple(expected_names)
    if (
        not expected
        or len(expected) > MAX_ARTIFACTS
        or len(set(expected)) != len(expected)
    ):
        raise ReleaseArtifactError(
            "expected artifact allowlist is empty, duplicated, or too large"
        )
    attempt_suffix = f"-attempt-{run.attempt}"
    expected_bases: dict[str, str] = {}
    for name in expected:
        if not isinstance(name, str) or not name.endswith(attempt_suffix):
            raise ReleaseArtifactError(
                "expected artifact names must be bound to the workflow run attempt"
            )
        base = name[: -len(attempt_suffix)]
        if not base or base in expected_bases:
            raise ReleaseArtifactError("expected artifact bases are empty or duplicated")
        expected_bases[base] = name
    value = _load_json(payload)
    if set(value) != {"total_count", "artifacts"}:
        raise ReleaseArtifactError("artifact inventory has unexpected root fields")
    total_count = _positive_int(value["total_count"], "artifact total_count")
    raw_artifacts = value["artifacts"]
    if not isinstance(raw_artifacts, list):
        raise ReleaseArtifactError("artifact inventory artifacts must be an array")
    if total_count != len(raw_artifacts) or total_count > MAX_ARTIFACTS:
        raise ReleaseArtifactError("artifact total_count does not match the bounded array")

    all_records: list[ArtifactRecord] = []
    current_records: list[ArtifactRecord] = []
    for index, raw in enumerate(raw_artifacts):
        context = f"artifacts[{index}]"
        if not isinstance(raw, dict):
            raise ReleaseArtifactError(f"{context} must be an object")
        if raw.get("expired") is not False:
            raise ReleaseArtifactError(f"{context} is expired or omitted its expiry state")
        workflow = raw.get("workflow_run")
        if not isinstance(workflow, dict):
            raise ReleaseArtifactError(f"{context}.workflow_run must be an object")
        record = ArtifactRecord(
            artifact_id=_positive_int(raw.get("id"), f"{context}.id"),
            name=_string(raw.get("name"), f"{context}.name"),
            size_in_bytes=_positive_int(
                raw.get("size_in_bytes"), f"{context}.size_in_bytes"
            ),
            archive_sha256=_artifact_digest(
                raw.get("digest"), f"{context}.digest"
            ),
            created_at=_string(raw.get("created_at"), f"{context}.created_at"),
            updated_at=_string(raw.get("updated_at"), f"{context}.updated_at"),
            run_id=_positive_int(workflow.get("id"), f"{context}.workflow_run.id"),
            head_sha=_string(
                workflow.get("head_sha"), f"{context}.workflow_run.head_sha"
            ),
            head_branch=_string(
                workflow.get("head_branch"), f"{context}.workflow_run.head_branch"
            ),
            repository_id=_positive_int(
                workflow.get("repository_id"),
                f"{context}.workflow_run.repository_id",
            ),
            head_repository_id=_positive_int(
                workflow.get("head_repository_id"),
                f"{context}.workflow_run.head_repository_id",
            ),
        )
        if record.size_in_bytes > MAX_ARTIFACT_ARCHIVE_BYTES:
            raise ReleaseArtifactError(
                f"{context}.size_in_bytes exceeds the artifact archive limit"
            )
        mismatches = []
        if record.run_id != run.run_id:
            mismatches.append("run id")
        if record.head_sha != run.head_sha:
            mismatches.append("head SHA")
        if record.head_branch != run.tag:
            mismatches.append("tag")
        if record.repository_id != record.head_repository_id:
            mismatches.append("head repository")
        if mismatches:
            raise ReleaseArtifactError(
                f"{context} differs from the workflow binding: {', '.join(mismatches)}"
            )
        base, separator, attempt_text = record.name.rpartition("-attempt-")
        if (
            not separator
            or base not in expected_bases
            or not attempt_text.isascii()
            or not attempt_text.isdecimal()
            or attempt_text.startswith("0")
        ):
            raise ReleaseArtifactError(
                f"{context} has an artifact name outside the attempt-bound allowlist"
            )
        artifact_attempt = int(attempt_text)
        if artifact_attempt <= 0 or artifact_attempt > run.attempt:
            raise ReleaseArtifactError(
                f"{context} names invalid workflow attempt {artifact_attempt}"
            )
        all_records.append(record)
        if artifact_attempt == run.attempt:
            current_records.append(record)

    all_names = [record.name for record in all_records]
    all_identifiers = [record.artifact_id for record in all_records]
    if len(set(all_names)) != len(all_names) or len(set(all_identifiers)) != len(
        all_identifiers
    ):
        raise ReleaseArtifactError("artifact inventory contains duplicate names or IDs")

    names = [record.name for record in current_records]
    if set(names) != set(expected):
        raise ReleaseArtifactError(
            "current-attempt artifact name mismatch; "
            f"missing={sorted(set(expected) - set(names))}; "
            f"extra={sorted(set(names) - set(expected))}"
        )
    return tuple(sorted(current_records, key=lambda record: record.name))
