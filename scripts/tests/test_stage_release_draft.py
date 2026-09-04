from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from release_test_support import create_symlink_or_skip, stage_release_draft
import release_notes


REPOSITORY = "example/app-icon-toolkit"
TAG = "v1.2.3"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
NOTES = "# Release notes\n\nVerified changes.\n"


def completed(
    returncode: int,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=("gh",),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def release_json(**changes: object) -> str:
    value = {
        "id": "R_release",
        "databaseId": 789,
        "tagName": TAG,
        "name": f"App Icon Toolkit {TAG}",
        "body": NOTES,
        "isDraft": True,
        "isPrerelease": False,
        "assets": [],
    }
    value.update(changes)
    return json.dumps(value)


class ScriptedRunner:
    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(
        self,
        command: tuple[str, ...],
        stdin_text: str | None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, stdin_text))
        if not self.results:
            raise AssertionError("unexpected command")
        return self.results.pop(0)


class DraftStagingTests(unittest.TestCase):
    def test_existing_exact_empty_draft_is_reused_without_mutation(self) -> None:
        runner = ScriptedRunner([completed(0, stdout=release_json())])
        receipt = stage_release_draft.stage_release_draft(
            REPOSITORY,
            TAG,
            HEAD_SHA,
            NOTES,
            runner,
        )
        self.assertTrue(receipt.reused_existing_draft)
        self.assertFalse(receipt.reconciled_after_unknown_mutation)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0][0:3], ("gh", "release", "view"))
        self.assertIsNone(runner.calls[0][1])

    def test_missing_draft_is_created_once_and_reconciled(self) -> None:
        runner = ScriptedRunner(
            [
                completed(1, stderr="release not found\n"),
                completed(0, stdout="https://example.invalid/draft\n"),
                completed(0, stdout=release_json()),
            ]
        )
        receipt = stage_release_draft.stage_release_draft(
            REPOSITORY,
            TAG,
            HEAD_SHA,
            NOTES,
            runner,
        )
        self.assertFalse(receipt.reused_existing_draft)
        self.assertFalse(receipt.reconciled_after_unknown_mutation)
        self.assertEqual(len(runner.calls), 3)
        create, stdin_text = runner.calls[1]
        self.assertEqual(create[0:3], ("gh", "release", "create"))
        self.assertIn("--draft", create)
        self.assertIn("--verify-tag", create)
        self.assertNotIn("--latest", create)
        self.assertEqual(stdin_text, NOTES)

    def test_failed_create_is_read_only_reconciled_without_retry(self) -> None:
        runner = ScriptedRunner(
            [
                completed(1, stderr="release not found\n"),
                completed(1, stderr="connection closed after send"),
                completed(0, stdout=release_json()),
            ]
        )
        receipt = stage_release_draft.stage_release_draft(
            REPOSITORY,
            TAG,
            HEAD_SHA,
            NOTES,
            runner,
        )
        self.assertTrue(receipt.reconciled_after_unknown_mutation)
        self.assertEqual(
            sum(call[0][0:3] == ("gh", "release", "create") for call in runner.calls),
            1,
        )

    def test_unreconciled_create_stays_unknown_and_is_never_retried(self) -> None:
        runner = ScriptedRunner(
            [
                completed(1, stderr="release not found\n"),
                completed(1, stderr="connection closed after send"),
                completed(1, stderr="release not found\n"),
            ]
        )
        with self.assertRaisesRegex(
            stage_release_draft.DraftStagingOutcomeUnknown,
            "inspect the tag release",
        ):
            stage_release_draft.stage_release_draft(
                REPOSITORY,
                TAG,
                HEAD_SHA,
                NOTES,
                runner,
            )
        self.assertEqual(
            sum(call[0][0:3] == ("gh", "release", "create") for call in runner.calls),
            1,
        )

    def test_wrong_or_nonempty_existing_release_is_never_modified(self) -> None:
        cases = (
            (release_json(body="wrong"), "body"),
            (release_json(isDraft=False), "unpublished"),
            (release_json(isPrerelease=True), "unpublished"),
            (
                release_json(
                    assets=[
                        {
                            "name": "unexpected.zip",
                            "size": 1,
                            "digest": f"sha256:{'0' * 64}",
                            "state": "uploaded",
                        }
                    ]
                ),
                "empty",
            ),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                runner = ScriptedRunner([completed(0, stdout=payload)])
                with self.assertRaisesRegex(
                    stage_release_draft.DraftStagingError,
                    message,
                ):
                    stage_release_draft.stage_release_draft(
                        REPOSITORY,
                        TAG,
                        HEAD_SHA,
                        NOTES,
                        runner,
                    )
                self.assertEqual(len(runner.calls), 1)

    def test_lookup_failure_is_unknown_and_does_not_create(self) -> None:
        runner = ScriptedRunner([completed(1, stderr="network unavailable")])
        with self.assertRaises(stage_release_draft.DraftStagingOutcomeUnknown):
            stage_release_draft.stage_release_draft(
                REPOSITORY,
                TAG,
                HEAD_SHA,
                NOTES,
                runner,
            )
        self.assertEqual(len(runner.calls), 1)

    def test_release_notes_reader_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-notes-test-") as temporary:
            root = Path(temporary)
            notes = root / "notes"
            notes.write_text(NOTES, encoding="utf-8")
            symlink = root / "symlink"
            create_symlink_or_skip(self, symlink, notes)
            with self.assertRaises(release_notes.ReleaseNotesError):
                release_notes.read_release_notes(symlink)

    def test_release_notes_reader_accepts_utf8_and_rejects_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-notes-test-") as temporary:
            root = Path(temporary)
            notes = root / "notes"
            notes.write_bytes(NOTES.encode("utf-8"))
            self.assertEqual(release_notes.read_release_notes(notes), NOTES)

            invalid = root / "invalid"
            invalid.write_bytes(b"\xff")
            with self.assertRaisesRegex(
                release_notes.ReleaseNotesError,
                "UTF-8",
            ):
                release_notes.read_release_notes(invalid)


if __name__ == "__main__":
    unittest.main()
