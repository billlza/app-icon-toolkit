"""Exact remote annotated-tag validation for a GitHub release transaction."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from release_targets import validate_commit_sha, validate_release_tag


MAX_TAG_JSON_BYTES = 256 * 1024


class ReleaseTagError(RuntimeError):
    """The remote tag does not identify the expected annotated commit."""


@dataclass(frozen=True)
class RemoteTagBinding:
    tag: str
    tag_object_sha: str
    commit_sha: str


def remote_tag_object_sha(
    ref_payload: str,
    *,
    expected_tag: str,
    expected_local_tag_object_sha: str,
) -> str:
    """Validate an annotated remote tag ref and return its tag-object SHA."""

    try:
        expected_tag = validate_release_tag(expected_tag)
        expected_local_tag_object_sha = validate_commit_sha(
            expected_local_tag_object_sha
        )
    except RuntimeError as error:
        raise ReleaseTagError(str(error)) from error
    ref = _load_object(ref_payload, "remote tag ref")
    if _string(ref.get("ref"), "remote tag ref.ref") != f"refs/tags/{expected_tag}":
        raise ReleaseTagError("remote tag ref name differs from the release tag")
    ref_object = _object(ref.get("object"), "remote tag ref.object")
    if _string(ref_object.get("type"), "remote tag ref.object.type") != "tag":
        raise ReleaseTagError("remote release tag must be annotated, not lightweight")
    tag_object_sha = _string(ref_object.get("sha"), "remote tag ref.object.sha")
    try:
        validate_commit_sha(tag_object_sha)
    except RuntimeError as error:
        raise ReleaseTagError(str(error)) from error
    if tag_object_sha != expected_local_tag_object_sha:
        raise ReleaseTagError("local and remote annotated tag object SHAs differ")
    return tag_object_sha


def _load_object(payload: str, context: str) -> dict[str, Any]:
    if not isinstance(payload, str):
        raise ReleaseTagError(f"{context} must be text")
    try:
        size = len(payload.encode("utf-8", errors="strict"))
    except UnicodeError as error:
        raise ReleaseTagError(f"{context} is not valid UTF-8") from error
    if size <= 0 or size > MAX_TAG_JSON_BYTES:
        raise ReleaseTagError(f"{context} size {size} is outside the allowed range")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseTagError(f"{context} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except ReleaseTagError:
        raise
    except json.JSONDecodeError as error:
        raise ReleaseTagError(f"{context} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseTagError(f"{context} JSON root must be an object")
    return value


def _object(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseTagError(f"{context} must be an object")
    return value


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ReleaseTagError(f"{context} must be a bounded non-empty string")
    return value


def parse_remote_annotated_tag(
    ref_payload: str,
    tag_payload: str,
    *,
    expected_tag: str,
    expected_commit_sha: str,
    expected_local_tag_object_sha: str,
) -> RemoteTagBinding:
    """Require the remote tag ref, tag object, local tag object, and commit to agree."""

    try:
        expected_tag = validate_release_tag(expected_tag)
        expected_commit_sha = validate_commit_sha(expected_commit_sha)
    except RuntimeError as error:
        raise ReleaseTagError(str(error)) from error
    tag_object_sha = remote_tag_object_sha(
        ref_payload,
        expected_tag=expected_tag,
        expected_local_tag_object_sha=expected_local_tag_object_sha,
    )

    tag_object = _load_object(tag_payload, "remote annotated tag object")
    if _string(tag_object.get("sha"), "remote annotated tag.sha") != tag_object_sha:
        raise ReleaseTagError("remote annotated tag object SHA differs from its ref")
    if _string(tag_object.get("tag"), "remote annotated tag.tag") != expected_tag:
        raise ReleaseTagError("remote annotated tag object contains the wrong tag name")
    target = _object(tag_object.get("object"), "remote annotated tag.object")
    if _string(target.get("type"), "remote annotated tag.object.type") != "commit":
        raise ReleaseTagError("remote annotated tag must point directly to a commit")
    commit_sha = _string(target.get("sha"), "remote annotated tag.object.sha")
    try:
        validate_commit_sha(commit_sha)
    except RuntimeError as error:
        raise ReleaseTagError(str(error)) from error
    if commit_sha != expected_commit_sha:
        raise ReleaseTagError("remote annotated tag points to the wrong release commit")
    return RemoteTagBinding(
        tag=expected_tag,
        tag_object_sha=tag_object_sha,
        commit_sha=commit_sha,
    )
