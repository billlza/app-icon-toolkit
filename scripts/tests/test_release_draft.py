from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_draft
import release_files


TAG = "v1.2.3"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
REPOSITORY = "example/app-icon-toolkit"
WORKFLOW_ID = 777
RUN_ID = 12345
RUN_ATTEMPT = 2
RELEASE_BODY = "# Release notes\n\nVerified changes.\n"


def run_json(**changes: object) -> str:
    value = {
        "databaseId": RUN_ID,
        "workflowDatabaseId": WORKFLOW_ID,
        "attempt": RUN_ATTEMPT,
        "headBranch": TAG,
        "headSha": HEAD_SHA,
        "workflowName": "Release",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    value.update(changes)
    return json.dumps(value)


def workflow_json(**changes: object) -> str:
    value = {
        "id": WORKFLOW_ID,
        "name": release_draft.EXPECTED_WORKFLOW,
        "path": release_draft.EXPECTED_WORKFLOW_PATH,
        "state": "active",
    }
    value.update(changes)
    return json.dumps(value)


def release_json(
    assets: list[dict[str, object]] | None = None,
    **changes: object,
) -> str:
    value = {
        "id": "R_kgDORelease",
        "databaseId": 67890,
        "tagName": TAG,
        "name": f"App Icon Toolkit {TAG}",
        "body": RELEASE_BODY,
        "isDraft": True,
        "isPrerelease": False,
        "assets": [] if assets is None else assets,
    }
    value.update(changes)
    return json.dumps(value)


def remote_asset(asset: release_draft.LocalAsset) -> dict[str, object]:
    return {
        "name": asset.name,
        "size": asset.size,
        "digest": f"sha256:{asset.sha256}",
        "state": "uploaded",
    }


class RecordingRunner:
    def __init__(self, return_codes: list[int] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.return_codes = list(return_codes or [])

    def __call__(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        return_code = self.return_codes.pop(0) if self.return_codes else 0
        return subprocess.CompletedProcess(
            args=command,
            returncode=return_code,
            stdout="",
            stderr="failure" if return_code else "",
        )


class RaisingRunner:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        raise self.error


class ReleaseDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = release_draft.parse_workflow_run(
            run_json(),
            expected_workflow_id=WORKFLOW_ID,
            expected_run_id=RUN_ID,
            expected_attempt=RUN_ATTEMPT,
            expected_tag=TAG,
            expected_head_sha=HEAD_SHA,
        )

    def plan(
        self,
        release: release_draft.ReleaseSnapshot,
        assets: tuple[release_draft.LocalAsset, ...],
    ) -> release_draft.DraftUploadPlan:
        return release_draft.plan_draft_uploads(
            REPOSITORY,
            self.run,
            release,
            assets,
            expected_body=RELEASE_BODY,
        )

    def test_repository_slug_is_strict_and_reusable(self) -> None:
        self.assertEqual(release_draft.validate_repository(REPOSITORY), REPOSITORY)
        for invalid in ("owner", "https://github.com/owner/repo", "../owner/repo"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(release_draft.ReleaseDraftError):
                    release_draft.validate_repository(invalid)

    def test_canonical_workflow_identity_is_strictly_bound(self) -> None:
        workflow = release_draft.parse_workflow_identity(
            workflow_json(),
            expected_workflow_id=WORKFLOW_ID,
            expected_name=release_draft.EXPECTED_WORKFLOW,
            expected_path=release_draft.EXPECTED_WORKFLOW_PATH,
        )
        self.assertEqual(workflow.workflow_id, WORKFLOW_ID)

        cases = (
            ("id", WORKFLOW_ID + 1, "ID"),
            ("name", "Other", "name"),
            ("path", ".github/workflows/other.yml", "path"),
            ("state", "disabled_manually", "not active"),
        )
        for field, replacement, message in cases:
            with self.subTest(field=field), self.assertRaisesRegex(
                release_draft.ReleaseDraftError,
                message,
            ):
                release_draft.parse_workflow_identity(
                    workflow_json(**{field: replacement}),
                    expected_workflow_id=WORKFLOW_ID,
                    expected_name=release_draft.EXPECTED_WORKFLOW,
                    expected_path=release_draft.EXPECTED_WORKFLOW_PATH,
                )

        duplicate = workflow_json().replace(
            f'"id": {WORKFLOW_ID}',
            f'"id": {WORKFLOW_ID}, "id": {WORKFLOW_ID}',
            1,
        )
        with self.assertRaisesRegex(release_draft.ReleaseDraftError, "repeats JSON key"):
            release_draft.parse_workflow_identity(
                duplicate,
                expected_workflow_id=WORKFLOW_ID,
                expected_name=release_draft.EXPECTED_WORKFLOW,
                expected_path=release_draft.EXPECTED_WORKFLOW_PATH,
            )

    def test_publication_command_is_explicitly_github_com_bound(self) -> None:
        verified = release_draft.VerifiedDraft(
            repository=REPOSITORY,
            run=self.run,
            release_id="R_kgDORelease",
            release_database_id=67890,
            expected_body=RELEASE_BODY,
            assets=(),
        )
        self.assertEqual(
            release_draft.publish_command(verified)[:4],
            ("gh", "api", "--hostname", "github.com"),
        )

    def make_assets(
        self, root: Path
    ) -> tuple[release_draft.LocalAsset, ...]:
        first = root / "app-icon-toolkit-v1.2.3-aarch64-apple-darwin.zip"
        second = root / "app-icon-toolkit-v1.2.3-x86_64-apple-darwin.zip"
        first.write_bytes(b"arm64 signed archive")
        second.write_bytes(b"x86_64 signed archive")
        archives = release_draft.snapshot_local_assets(
            {first.name: first, second.name: second},
            expected_names=(second.name, first.name),
        )
        checksum = release_draft.generate_sha256sums(
            archives, root / release_draft.CHECKSUM_ASSET_NAME
        )
        self.assertEqual(list(root.glob(".SHA256SUMS.*.tmp")), [])
        return tuple(sorted((*archives, checksum), key=lambda asset: asset.name))

    def test_workflow_run_rejects_wrong_commit_and_release_contract_fields(self) -> None:
        cases = (
            ({"workflowDatabaseId": None}, "workflowDatabaseId"),
            ({"workflowDatabaseId": WORKFLOW_ID + 1}, "workflowDatabaseId"),
            ({"databaseId": RUN_ID + 1}, "databaseId"),
            ({"attempt": None}, "attempt"),
            ({"attempt": RUN_ATTEMPT + 1}, "attempt"),
            ({"headSha": "f" * 40}, "headSha"),
            ({"headBranch": "v1.2.4"}, "tag"),
            ({"workflowName": "Other"}, "workflowName"),
            ({"event": "workflow_dispatch"}, "event"),
            ({"conclusion": "failure"}, "conclusion"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(release_draft.ReleaseDraftError, message):
                    release_draft.parse_workflow_run(
                        run_json(**changes),
                        expected_workflow_id=WORKFLOW_ID,
                        expected_run_id=RUN_ID,
                        expected_attempt=RUN_ATTEMPT,
                        expected_tag=TAG,
                        expected_head_sha=HEAD_SHA,
                    )

        with self.assertRaisesRegex(
            release_draft.ReleaseDraftError, "stable semantic version"
        ):
            release_draft.parse_workflow_run(
                run_json(),
                expected_workflow_id=WORKFLOW_ID,
                expected_run_id=RUN_ID,
                expected_attempt=RUN_ATTEMPT,
                expected_tag="v01.2.3",
                expected_head_sha=HEAD_SHA,
            )

    def test_workflow_and_release_json_reject_duplicate_security_fields(self) -> None:
        duplicate_run = run_json().replace(
            f'"databaseId": {RUN_ID}',
            f'"databaseId": {RUN_ID}, "databaseId": {RUN_ID}',
            1,
        )
        with self.assertRaisesRegex(
            release_draft.ReleaseDraftError,
            "repeats JSON key 'databaseId'",
        ):
            release_draft.parse_workflow_run(
                duplicate_run,
                expected_workflow_id=WORKFLOW_ID,
                expected_run_id=RUN_ID,
                expected_attempt=RUN_ATTEMPT,
                expected_tag=TAG,
                expected_head_sha=HEAD_SHA,
            )

        duplicate_release = release_json().replace(
            '"isDraft": true',
            '"isDraft": true, "isDraft": true',
            1,
        )
        with self.assertRaisesRegex(
            release_draft.ReleaseDraftError,
            "repeats JSON key 'isDraft'",
        ):
            release_draft.parse_release(duplicate_release, expected_tag=TAG)

    def test_non_draft_release_is_never_resumed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            release = release_draft.parse_release(
                release_json(isDraft=False), expected_tag=TAG
            )
            with self.assertRaisesRegex(
                release_draft.ReleaseDraftError, "non-draft"
            ):
                self.plan(release, assets)

    def test_wrong_release_name_or_prerelease_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            for changes, message in (
                ({"name": "Wrong release"}, "name"),
                ({"body": "Wrong body"}, "body"),
                ({"isPrerelease": True}, "prerelease"),
            ):
                with self.subTest(changes=changes):
                    release = release_draft.parse_release(
                        release_json(**changes), expected_tag=TAG
                    )
                    with self.assertRaisesRegex(
                        release_draft.ReleaseDraftError, message
                    ):
                        self.plan(release, assets)

    def test_extra_remote_asset_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            unexpected = {
                "name": "unexpected.zip",
                "size": 4,
                "digest": f"sha256:{'a' * 64}",
                "state": "uploaded",
            }
            release = release_draft.parse_release(
                release_json([unexpected]), expected_tag=TAG
            )
            with self.assertRaisesRegex(
                release_draft.ReleaseDraftError, "unexpected assets"
            ):
                self.plan(release, assets)

    def test_remote_digest_mismatch_is_not_safe_to_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            mismatched = remote_asset(assets[0])
            mismatched["digest"] = f"sha256:{'0' * 64}"
            release = release_draft.parse_release(
                release_json([mismatched]), expected_tag=TAG
            )
            with self.assertRaisesRegex(
                release_draft.ReleaseDraftError, "digest mismatch"
            ):
                self.plan(release, assets)

    def test_remote_size_mismatch_is_not_safe_to_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            mismatched = remote_asset(assets[0])
            mismatched["size"] = assets[0].size + 1
            release = release_draft.parse_release(
                release_json([mismatched]), expected_tag=TAG
            )
            with self.assertRaisesRegex(
                release_draft.ReleaseDraftError, "size mismatch"
            ):
                self.plan(release, assets)

    def test_hashing_detects_ctime_only_concurrent_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            asset = Path(temporary) / "candidate.tar.gz"
            asset.write_bytes(b"same-size bytes")
            metadata = asset.stat()

            def observed(ctime_ns: int) -> SimpleNamespace:
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_nlink=metadata.st_nlink,
                    st_dev=metadata.st_dev,
                    st_ino=metadata.st_ino,
                    st_size=metadata.st_size,
                    st_mtime_ns=metadata.st_mtime_ns,
                    st_ctime_ns=ctime_ns,
                )

            with mock.patch.object(
                release_files.os,
                "fstat",
                side_effect=(
                    observed(metadata.st_ctime_ns),
                    observed(metadata.st_ctime_ns + 1),
                ),
            ):
                with self.assertRaisesRegex(
                    release_draft.ReleaseDraftError, "changed while it was being read"
                ):
                    release_draft._snapshot_path(asset.name, asset)

    def test_checksum_failures_never_publish_a_partial_final_file(self) -> None:
        for failure in ("zero-write", "partial-write", "fsync"):
            with self.subTest(failure=failure):
                with tempfile.TemporaryDirectory(
                    prefix="release-draft-test-"
                ) as temporary:
                    root = Path(temporary)
                    archive = root / "candidate.zip"
                    archive.write_bytes(b"signed archive")
                    assets = release_draft.snapshot_local_assets(
                        {archive.name: archive}, expected_names=(archive.name,)
                    )
                    checksum = root / release_draft.CHECKSUM_ASSET_NAME
                    real_write = release_draft.os.write
                    writes = 0

                    def partial_then_fail(descriptor: int, data: bytes) -> int:
                        nonlocal writes
                        writes += 1
                        if writes == 1:
                            return real_write(descriptor, data[:5])
                        raise OSError("injected disk failure")

                    if failure == "zero-write":
                        patcher = mock.patch.object(
                            release_draft.os, "write", return_value=0
                        )
                    elif failure == "partial-write":
                        patcher = mock.patch.object(
                            release_draft.os, "write", side_effect=partial_then_fail
                        )
                    else:
                        patcher = mock.patch.object(
                            release_draft.os,
                            "fsync",
                            side_effect=OSError("injected fsync failure"),
                        )
                    with patcher:
                        with self.assertRaises(release_draft.ReleaseDraftError):
                            release_draft.generate_sha256sums(assets, checksum)
                    self.assertFalse(checksum.exists())
                    self.assertEqual(list(root.glob(".SHA256SUMS.*.tmp")), [])

    def test_existing_checksum_is_unchanged_and_temporary_is_removed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            archive = root / "candidate.zip"
            archive.write_bytes(b"signed archive")
            assets = release_draft.snapshot_local_assets(
                {archive.name: archive}, expected_names=(archive.name,)
            )
            checksum = root / release_draft.CHECKSUM_ASSET_NAME
            checksum.write_bytes(b"existing checksum")

            with self.assertRaisesRegex(
                release_draft.ReleaseDraftError, "refusing to replace"
            ):
                release_draft.generate_sha256sums(assets, checksum)

            self.assertEqual(checksum.read_bytes(), b"existing checksum")
            self.assertEqual(list(root.glob(".SHA256SUMS.*.tmp")), [])

    def test_indeterminate_checksum_publication_preserves_temporary_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            archive = root / "candidate.zip"
            archive.write_bytes(b"signed archive")
            assets = release_draft.snapshot_local_assets(
                {archive.name: archive}, expected_names=(archive.name,)
            )
            checksum = root / release_draft.CHECKSUM_ASSET_NAME

            with mock.patch.object(
                release_draft,
                "publish_sibling_no_replace",
                side_effect=release_files.FilePublicationIndeterminate(
                    "injected uncertain link result"
                ),
            ):
                with self.assertRaises(release_draft.MutationOutcomeUnknown):
                    release_draft.generate_sha256sums(assets, checksum)

            self.assertFalse(checksum.exists())
            self.assertEqual(len(list(root.glob(".SHA256SUMS.*.tmp"))), 1)

    def test_partial_upload_stops_and_cannot_authorize_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            empty = release_draft.parse_release(release_json(), expected_tag=TAG)
            plan = self.plan(empty, assets)
            runner = RecordingRunner([0, 1])

            with self.assertRaises(release_draft.MutationOutcomeUnknown):
                release_draft.run_uploads(plan, runner)

            self.assertEqual(len(runner.calls), 2)
            self.assertTrue(
                all(
                    call[0:4]
                    == ("gh", "api", "--hostname", "github.com")
                    for call in runner.calls
                )
            )
            self.assertTrue(
                all(
                    call[6].startswith("https://uploads.github.com/")
                    and "api.uploads.github.com" not in call[6]
                    and TAG not in call[6].split("?", maxsplit=1)[0]
                    for call in runner.calls
                )
            )
            first_uploaded_name = Path(runner.calls[0][10]).name
            by_name = {asset.name: asset for asset in assets}
            partial = release_draft.parse_release(
                release_json([remote_asset(by_name[first_uploaded_name])]),
                expected_tag=TAG,
            )
            with self.assertRaisesRegex(
                release_draft.ReleaseDraftError, "missing assets"
            ):
                release_draft.verify_complete_draft(plan, partial)
            self.assertFalse(
                any("--draft=false" in argument for call in runner.calls for argument in call)
            )

    def test_runner_exceptions_make_upload_outcome_unknown_after_one_attempt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            plan = self.plan(
                release_draft.parse_release(release_json(), expected_tag=TAG),
                assets,
            )
            for error in (
                RuntimeError("wrapped finalizer runner failure"),
                UnicodeError("invalid command output"),
                subprocess.TimeoutExpired(cmd="gh", timeout=300),
            ):
                with self.subTest(error=type(error).__name__):
                    runner = RaisingRunner(error)
                    with self.assertRaisesRegex(
                        release_draft.MutationOutcomeUnknown,
                        "reconcile before retry",
                    ):
                        release_draft.run_uploads(plan, runner)
                    self.assertEqual(len(runner.calls), 1)

    def test_successful_flow_is_sorted_verified_and_published_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            checksum_text = (root / release_draft.CHECKSUM_ASSET_NAME).read_text(
                encoding="ascii"
            )
            checksum_names = [
                line.split("  ", maxsplit=1)[1]
                for line in checksum_text.splitlines()
            ]
            self.assertEqual(checksum_names, sorted(checksum_names))

            empty = release_draft.parse_release(release_json(), expected_tag=TAG)
            plan = self.plan(empty, assets)
            runner = RecordingRunner()
            release_draft.run_uploads(plan, runner)

            upload_names = [Path(command[10]).name for command in runner.calls]
            archive_names = sorted(
                asset.name
                for asset in assets
                if asset.name != release_draft.CHECKSUM_ASSET_NAME
            )
            expected_upload_names = [
                *archive_names,
                release_draft.CHECKSUM_ASSET_NAME,
            ]
            self.assertEqual(
                upload_names, expected_upload_names
            )
            by_name = {asset.name: asset for asset in assets}
            self.assertEqual(
                runner.calls,
                [
                    (
                        "gh",
                        "api",
                        "--hostname",
                        "github.com",
                        "--method",
                        "POST",
                        (
                            "https://uploads.github.com/repos/example/"
                            "app-icon-toolkit/releases/67890/assets?name="
                            f"{name}"
                        ),
                        "--header",
                        "Content-Type: application/octet-stream",
                        "--input",
                        str(by_name[name].path),
                        "--silent",
                    )
                    for name in expected_upload_names
                ],
            )
            self.assertTrue(all("--clobber" not in command for command in runner.calls))
            self.assertTrue(
                all(
                    "https://uploads.github.com/repos/example/app-icon-toolkit/"
                    "releases/67890/assets?name="
                    in command[6]
                    for command in runner.calls
                )
            )
            self.assertTrue(
                all("api.uploads.github.com" not in command[6] for command in runner.calls)
            )
            self.assertTrue(all(TAG not in command for command in runner.calls))

            complete_value = json.loads(release_json([remote_asset(asset) for asset in assets]))
            complete_value["assets"] = list(reversed(complete_value["assets"]))
            complete_json = json.dumps(complete_value)
            complete = release_draft.parse_release(complete_json, expected_tag=TAG)
            verified = release_draft.verify_complete_draft(plan, complete)

            snapshots = iter(
                (
                    complete_json,
                    release_json(
                        [remote_asset(asset) for asset in assets], isDraft=False
                    ),
                )
            )
            receipt = release_draft.publish_verified_draft(
                verified, runner, lambda: next(snapshots)
            )

            self.assertFalse(receipt.reconciled_after_unknown_mutation)
            self.assertEqual(runner.calls[-1], release_draft.publish_command(verified))
            self.assertEqual(
                sum("draft=false" in command for command in runner.calls), 1
            )
            publish = runner.calls[-1]
            self.assertIn("repos/example/app-icon-toolkit/releases/67890", publish)
            self.assertNotIn(TAG, publish)

    def test_unknown_publish_is_only_reconciled_and_never_retried(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            complete_json = release_json([remote_asset(asset) for asset in assets])
            plan = self.plan(
                release_draft.parse_release(complete_json, expected_tag=TAG),
                assets,
            )
            verified = release_draft.verify_complete_draft(
                plan, release_draft.parse_release(complete_json, expected_tag=TAG)
            )
            runner = RecordingRunner([1])
            snapshots = iter(
                (
                    complete_json,
                    release_json(
                        [remote_asset(asset) for asset in assets], isDraft=False
                    ),
                )
            )

            receipt = release_draft.publish_verified_draft(
                verified, runner, lambda: next(snapshots)
            )

            self.assertTrue(receipt.reconciled_after_unknown_mutation)
            self.assertEqual(runner.calls, [release_draft.publish_command(verified)])

    def test_pre_mutation_policy_failure_prevents_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-policy-gate-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            complete_json = release_json([remote_asset(asset) for asset in assets])
            complete = release_draft.parse_release(complete_json, expected_tag=TAG)
            plan = self.plan(complete, assets)
            verified = release_draft.verify_complete_draft(plan, complete)
            runner = RecordingRunner()
            checks = 0

            def reject_publication() -> None:
                nonlocal checks
                checks += 1
                raise RuntimeError("immutable releases are disabled")

            with self.assertRaisesRegex(RuntimeError, "immutable releases are disabled"):
                release_draft.publish_verified_draft(
                    verified,
                    runner,
                    lambda: complete_json,
                    before_mutation=reject_publication,
                )

            self.assertEqual(checks, 1)
            self.assertEqual(runner.calls, [])

    def test_runner_exceptions_are_reconciled_after_one_publish_attempt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            complete_json = release_json([remote_asset(asset) for asset in assets])
            plan = self.plan(
                release_draft.parse_release(complete_json, expected_tag=TAG),
                assets,
            )
            verified = release_draft.verify_complete_draft(
                plan,
                release_draft.parse_release(complete_json, expected_tag=TAG),
            )
            public_json = release_json(
                [remote_asset(asset) for asset in assets], isDraft=False
            )
            for error in (
                RuntimeError("wrapped finalizer runner failure"),
                UnicodeError("invalid command output"),
                subprocess.TimeoutExpired(cmd="gh", timeout=300),
            ):
                with self.subTest(error=type(error).__name__):
                    runner = RaisingRunner(error)
                    snapshots = iter((complete_json, public_json))
                    reads = 0

                    def read_release() -> str:
                        nonlocal reads
                        reads += 1
                        return next(snapshots)

                    receipt = release_draft.publish_verified_draft(
                        verified,
                        runner,
                        read_release,
                    )
                    self.assertTrue(receipt.reconciled_after_unknown_mutation)
                    self.assertEqual(
                        runner.calls,
                        [release_draft.publish_command(verified)],
                    )
                    self.assertEqual(reads, 2)

    def test_same_tag_replacement_cannot_redirect_publication_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-draft-test-") as temporary:
            root = Path(temporary)
            assets = self.make_assets(root)
            remote = [remote_asset(asset) for asset in assets]
            original_json = release_json(remote)
            plan = self.plan(
                release_draft.parse_release(original_json, expected_tag=TAG),
                assets,
            )
            verified = release_draft.verify_complete_draft(
                plan,
                release_draft.parse_release(original_json, expected_tag=TAG),
            )
            replacement_json = release_json(
                remote,
                id="R_replacement",
                databaseId=99999,
                isDraft=False,
            )
            snapshots = iter((original_json, replacement_json))
            runner = RecordingRunner([1])

            with self.assertRaises(release_draft.PublicationOutcomeUnknown):
                release_draft.publish_verified_draft(
                    verified, runner, lambda: next(snapshots)
                )

            self.assertEqual(len(runner.calls), 1)
            command = runner.calls[0]
            self.assertIn("repos/example/app-icon-toolkit/releases/67890", command)
            self.assertNotIn("repos/example/app-icon-toolkit/releases/99999", command)
            self.assertNotIn(TAG, command)


if __name__ == "__main__":
    unittest.main()
