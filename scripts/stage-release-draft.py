#!/usr/bin/env python3
"""Create or reconcile the exact empty GitHub draft for a release run."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Protocol

from release_draft import (
    MAX_COMMAND_OUTPUT_BYTES,
    RELEASE_VIEW_FIELDS,
    ReleaseDraftError,
    ReleaseSnapshot,
    parse_release,
)
from release_notes import MAX_NOTES_BYTES, ReleaseNotesError, read_release_notes
from release_targets import (
    validate_commit_sha,
    validate_release_tag,
    validate_repository,
)


COMMAND_TIMEOUT_SECONDS = 120


class DraftStagingError(RuntimeError):
    """The release draft is known not to satisfy the staging contract."""


class DraftStagingOutcomeUnknown(DraftStagingError):
    """Draft creation crossed the mutation boundary without reconciliation."""


class CommandRunner(Protocol):
    def __call__(
        self,
        command: tuple[str, ...],
        stdin_text: str | None,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class DraftStagingReceipt:
    repository: str
    tag: str
    head_sha: str
    release_id: str
    release_database_id: int
    reused_existing_draft: bool
    reconciled_after_unknown_mutation: bool


def subprocess_runner(
    command: tuple[str, ...], stdin_text: str | None
) -> subprocess.CompletedProcess[str]:
    """Run one shell-free GitHub CLI command with bounded waiting."""

    return subprocess.run(
        command,
        check=False,
        stdin=subprocess.DEVNULL if stdin_text is None else None,
        input=stdin_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


def _validate_command_output(
    result: subprocess.CompletedProcess[str], *, context: str
) -> None:
    if isinstance(result.returncode, bool) or not isinstance(result.returncode, int):
        raise DraftStagingOutcomeUnknown(f"{context} returned an invalid exit status")
    if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
        raise DraftStagingOutcomeUnknown(f"{context} returned non-text output")
    if (
        len(result.stdout.encode("utf-8", errors="replace")) > MAX_COMMAND_OUTPUT_BYTES
        or len(result.stderr.encode("utf-8", errors="replace"))
        > MAX_COMMAND_OUTPUT_BYTES
    ):
        raise DraftStagingOutcomeUnknown(f"{context} output exceeded the size limit")


def _invoke(
    runner: CommandRunner,
    command: tuple[str, ...],
    *,
    stdin_text: str | None,
    context: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(command, stdin_text)
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise DraftStagingOutcomeUnknown(
            f"{context} did not return a trustworthy result"
        ) from error
    _validate_command_output(result, context=context)
    return result


def release_view_command(repository: str, tag: str) -> tuple[str, ...]:
    return (
        "gh",
        "release",
        "view",
        tag,
        "--repo",
        repository,
        "--json",
        RELEASE_VIEW_FIELDS,
    )


def read_release(
    repository: str,
    tag: str,
    runner: CommandRunner,
) -> ReleaseSnapshot | None:
    """Read the exact release, distinguishing a known absence from uncertainty."""

    result = _invoke(
        runner,
        release_view_command(repository, tag),
        stdin_text=None,
        context="draft release lookup",
    )
    if result.returncode == 0:
        if result.stderr.strip():
            raise DraftStagingOutcomeUnknown(
                f"draft release lookup wrote unexpected stderr: {result.stderr.strip()}"
            )
        try:
            return parse_release(result.stdout, expected_tag=tag)
        except ReleaseDraftError as error:
            raise DraftStagingError(str(error)) from error
    if (
        result.returncode == 1
        and not result.stdout.strip()
        and result.stderr.strip() == "release not found"
    ):
        return None
    diagnostic = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
    raise DraftStagingOutcomeUnknown(
        f"draft release lookup failed with exit {result.returncode}: {diagnostic[:512]}"
    )


def validate_empty_draft(
    release: ReleaseSnapshot,
    *,
    tag: str,
    notes: str,
) -> None:
    """Accept only the exact empty stable draft created by this workflow."""

    expected_name = f"App Icon Toolkit {tag}"
    if release.tag != tag:
        raise DraftStagingError("draft tag differs from the requested release tag")
    if release.name != expected_name:
        raise DraftStagingError("draft release name differs from the release contract")
    if release.body != notes:
        raise DraftStagingError("draft release body differs from the tagged release notes")
    if not release.is_draft or release.is_prerelease:
        raise DraftStagingError("release must be a stable unpublished draft")
    if release.assets:
        raise DraftStagingError("staging requires an empty draft release")


def create_command(
    repository: str,
    tag: str,
    head_sha: str,
) -> tuple[str, ...]:
    return (
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        repository,
        "--draft",
        "--verify-tag",
        "--target",
        head_sha,
        "--title",
        f"App Icon Toolkit {tag}",
        "--notes-file",
        "-",
    )


def stage_release_draft(
    repository: str,
    tag: str,
    head_sha: str,
    notes: str,
    runner: CommandRunner = subprocess_runner,
) -> DraftStagingReceipt:
    """Create once or reconcile an existing exact draft without modifying it."""

    try:
        repository = validate_repository(repository)
        tag = validate_release_tag(tag)
        head_sha = validate_commit_sha(head_sha)
    except RuntimeError as error:
        raise DraftStagingError(str(error)) from error
    try:
        notes_size = len(notes.encode("utf-8", errors="strict"))
    except (AttributeError, UnicodeError) as error:
        raise DraftStagingError("release notes are not valid UTF-8 text") from error
    if not notes or notes_size > MAX_NOTES_BYTES:
        raise DraftStagingError("release notes size is outside the allowed range")

    existing = read_release(repository, tag, runner)
    if existing is not None:
        validate_empty_draft(existing, tag=tag, notes=notes)
        return DraftStagingReceipt(
            repository=repository,
            tag=tag,
            head_sha=head_sha,
            release_id=existing.release_id,
            release_database_id=existing.database_id,
            reused_existing_draft=True,
            reconciled_after_unknown_mutation=False,
        )

    mutation_unknown = False
    try:
        created = _invoke(
            runner,
            create_command(repository, tag, head_sha),
            stdin_text=notes,
            context="draft release creation",
        )
        if created.returncode != 0:
            mutation_unknown = True
    except DraftStagingOutcomeUnknown:
        mutation_unknown = True

    try:
        observed = read_release(repository, tag, runner)
        if observed is None:
            raise DraftStagingOutcomeUnknown(
                "draft creation could not be reconciled to an existing release"
            )
        validate_empty_draft(observed, tag=tag, notes=notes)
    except DraftStagingError as error:
        raise DraftStagingOutcomeUnknown(
            "draft creation outcome is unknown; inspect the tag release before retrying"
        ) from error

    return DraftStagingReceipt(
        repository=repository,
        tag=tag,
        head_sha=head_sha,
        release_id=observed.release_id,
        release_database_id=observed.database_id,
        reused_existing_draft=False,
        reconciled_after_unknown_mutation=mutation_unknown,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--notes-file", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        receipt = stage_release_draft(
            arguments.repository,
            arguments.tag,
            arguments.head_sha,
            read_release_notes(arguments.notes_file),
        )
    except (DraftStagingError, ReleaseNotesError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(asdict(receipt), separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
