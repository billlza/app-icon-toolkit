"""Stable, bounded release-note loading shared by staging and finalization."""

from __future__ import annotations

from pathlib import Path

from release_files import ReleaseFileError, open_stable_regular_file


MAX_NOTES_BYTES = 256 * 1024


class ReleaseNotesError(RuntimeError):
    """Release notes do not satisfy the stable text-file contract."""


def read_release_notes(path: Path) -> str:
    """Read stable UTF-8 release notes through one bounded descriptor."""

    chunks: list[bytes] = []
    size = 0
    try:
        with open_stable_regular_file(
            path,
            label="release notes",
            require_single_link=True,
        ) as (source, snapshot):
            if snapshot.size > MAX_NOTES_BYTES:
                raise ReleaseNotesError("release notes exceed the size limit")
            while True:
                chunk = source.read(min(64 * 1024, MAX_NOTES_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_NOTES_BYTES:
                    raise ReleaseNotesError("release notes exceed the size limit")
                chunks.append(chunk)
    except ReleaseFileError as error:
        raise ReleaseNotesError(f"cannot read stable release notes: {error}") from error
    try:
        text = b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ReleaseNotesError("release notes are not valid UTF-8") from error
    if not text:
        raise ReleaseNotesError("release notes must not be empty")
    return text
