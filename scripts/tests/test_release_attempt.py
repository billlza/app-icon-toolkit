from __future__ import annotations

from pathlib import Path
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_attempt


BINDING = release_attempt.ReleaseBinding(
    repository="example/app-icon-toolkit",
    tag="v1.2.3",
    head_sha="0123456789abcdef0123456789abcdef01234567",
    run_id=12345,
    run_attempt=2,
    workflow_database_id=67890,
)


class ReleaseAttemptTests(unittest.TestCase):
    def runtime_is_available(self) -> bool:
        if sys.platform == "darwin":
            return True
        with self.assertRaisesRegex(
            release_attempt.ReleaseAttemptError, "requires a macOS host"
        ):
            release_attempt.require_macos_host()
        return False

    def test_initialization_and_exact_resume_preserve_private_modes(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-attempt-parent-") as temporary:
            root = Path(temporary) / "attempt"
            initialized = release_attempt.initialize_or_resume(root, BINDING)
            self.assertEqual(initialized, root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((root / "binding.json").stat().st_mode),
                0o600,
            )
            self.assertEqual((root / "binding.json").stat().st_nlink, 1)
            self.assertEqual(
                release_attempt.initialize_or_resume(root, BINDING),
                root,
            )

            different = release_attempt.ReleaseBinding(
                **{**BINDING.__dict__, "run_attempt": 3}
            )
            with self.assertRaisesRegex(
                release_attempt.ReleaseAttemptError, "bound to"
            ):
                release_attempt.initialize_or_resume(root, different)

    def test_attempt_and_subdirectory_entries_are_parent_fsynced(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-attempt-durable-") as temporary:
            parent = Path(temporary)
            attempt = parent / "attempt"
            observed: list[Path] = []
            real_fsync_directory = release_attempt._fsync_private_directory

            def record(path: Path, *, context: str) -> None:
                self.assertTrue(context)
                observed.append(path)
                real_fsync_directory(path, context=context)

            with mock.patch.object(
                release_attempt,
                "_fsync_private_directory",
                side_effect=record,
            ):
                release_attempt.initialize_or_resume(attempt, BINDING)
                nested = release_attempt.private_subdirectory(attempt, "work")
            self.assertTrue(nested.is_dir())
            self.assertEqual(observed[0], parent)
            self.assertIn(attempt, observed)

    def test_attempt_container_must_itself_be_private(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-attempt-container-") as temporary:
            container = Path(temporary) / "insecure"
            container.mkdir(mode=0o755)
            with self.assertRaisesRegex(
                release_attempt.ReleaseAttemptError, "mode must be 0700"
            ):
                release_attempt.initialize_or_resume(container / "attempt", BINDING)
            self.assertFalse((container / "attempt").exists())

    def test_incomplete_or_insecure_attempt_is_never_adopted(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-attempt-parent-") as temporary:
            parent = Path(temporary)
            incomplete = parent / "incomplete"
            incomplete.mkdir(mode=0o700)
            with self.assertRaises(release_attempt.ReleaseAttemptError):
                release_attempt.initialize_or_resume(incomplete, BINDING)

            insecure = parent / "insecure"
            insecure.mkdir(mode=0o755)
            with self.assertRaisesRegex(
                release_attempt.ReleaseAttemptError, "mode must be 0700"
            ):
                release_attempt.initialize_or_resume(insecure, BINDING)

    def test_receipts_are_append_only_and_existing_bytes_remain_unchanged(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-receipt-test-") as temporary:
            root = Path(temporary)
            receipt = release_attempt.write_receipt_no_replace(
                root,
                "receipt.json",
                {"status": "first"},
            )
            original = receipt.read_bytes()
            with self.assertRaisesRegex(
                release_attempt.ReleaseAttemptError, "refusing to replace"
            ):
                release_attempt.write_receipt_no_replace(
                    root,
                    "receipt.json",
                    {"status": "second"},
                )
            self.assertEqual(receipt.read_bytes(), original)
            self.assertEqual(release_attempt.read_receipt(receipt), {"status": "first"})

    def test_attempt_lock_is_exclusive_and_rejects_symlink(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-lock-test-") as temporary:
            root = Path(temporary) / "attempt"
            release_attempt.initialize_or_resume(root, BINDING)
            with release_attempt.exclusive_attempt(root):
                with self.assertRaisesRegex(
                    release_attempt.ReleaseAttemptError, "another finalizer"
                ):
                    with release_attempt.exclusive_attempt(root):
                        self.fail("nested lock unexpectedly succeeded")

            lock = root / "attempt.lock"
            lock.unlink()
            external = Path(temporary) / "external"
            external.write_bytes(b"external")
            external.chmod(0o644)
            lock.symlink_to(external)
            with self.assertRaisesRegex(
                release_attempt.ReleaseAttemptError, "not a private owned file"
            ):
                with release_attempt.exclusive_attempt(root):
                    self.fail("symlink lock unexpectedly succeeded")
            self.assertEqual(external.read_bytes(), b"external")
            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o644)

    def test_attempt_lock_is_exclusive_across_processes(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-lock-process-test-") as temporary:
            root = Path(temporary) / "attempt"
            release_attempt.initialize_or_resume(root, BINDING)
            program = "\n".join(
                (
                    "import pathlib, sys",
                    "import release_attempt",
                    "try:",
                    "    with release_attempt.exclusive_attempt(pathlib.Path(sys.argv[1])):",
                    "        raise SystemExit(3)",
                    "except release_attempt.ReleaseAttemptError:",
                    "    raise SystemExit(0)",
                )
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(SCRIPTS_ROOT)
            with release_attempt.exclusive_attempt(root):
                completed = subprocess.run(
                    [sys.executable, "-c", program, str(root)],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    env=environment,
                    timeout=10,
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_receipt_reader_rejects_links_oversize_and_corrupt_json(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-receipt-invalid-test-") as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            symlink = root / "symlink.json"
            symlink.symlink_to(target)
            with self.assertRaises(release_attempt.ReleaseAttemptError):
                release_attempt.read_receipt(symlink)

            hardlink = root / "hardlink.json"
            os.link(target, hardlink)
            with self.assertRaises(release_attempt.ReleaseAttemptError):
                release_attempt.read_receipt(target)
            hardlink.unlink()

            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * (release_attempt.MAX_RECEIPT_BYTES + 1))
            with self.assertRaisesRegex(
                release_attempt.ReleaseAttemptError, "too large"
            ):
                release_attempt.read_receipt(oversized)

            for name, contents in (
                ("duplicate.json", b'{"key": 1, "key": 2}\n'),
                ("invalid.json", b"{\n"),
            ):
                path = root / name
                path.write_bytes(contents)
                with self.subTest(name=name), self.assertRaises(
                    release_attempt.ReleaseAttemptError
                ):
                    release_attempt.read_receipt(path)

    def test_indeterminate_receipt_publication_preserves_temporary_evidence(
        self,
    ) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-receipt-unknown-test-") as temporary:
            root = Path(temporary)
            with mock.patch.object(
                release_attempt,
                "publish_sibling_no_replace",
                side_effect=release_attempt.FilePublicationIndeterminate(
                    "injected unknown receipt publication"
                ),
            ):
                with self.assertRaises(
                    release_attempt.ReceiptPublicationIndeterminate
                ):
                    release_attempt.write_receipt_no_replace(
                        root,
                        "receipt.json",
                        {"status": "prepared"},
                    )
            self.assertFalse((root / "receipt.json").exists())
            preserved = list(root.glob(".receipt.json.*.tmp"))
            self.assertEqual(len(preserved), 1)
            self.assertEqual(
                release_attempt.read_receipt(preserved[0]),
                {"status": "prepared"},
            )

    def test_receipt_publication_fsyncs_directory_around_alias_cleanup(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-receipt-order-test-") as temporary:
            root = Path(temporary)
            events: list[str] = []
            real_fsync = os.fsync
            real_link = os.link
            real_unlink = os.unlink

            def record_fsync(descriptor: int) -> None:
                mode = os.fstat(descriptor).st_mode
                events.append("dir-fsync" if stat.S_ISDIR(mode) else "file-fsync")
                real_fsync(descriptor)

            def record_link(*args, **kwargs) -> None:
                events.append("link")
                real_link(*args, **kwargs)

            def record_unlink(path, *args, **kwargs) -> None:
                events.append("unlink")
                real_unlink(path, *args, **kwargs)

            with mock.patch.object(os, "fsync", side_effect=record_fsync), mock.patch.object(
                os,
                "link",
                side_effect=record_link,
            ), mock.patch.object(os, "unlink", side_effect=record_unlink):
                receipt = release_attempt.write_receipt_no_replace(
                    root,
                    "receipt.json",
                    {"status": "durable"},
                )

            publication = events.index("link")
            self.assertIn("file-fsync", events[:publication])
            self.assertEqual(
                events[publication : publication + 4],
                ["link", "dir-fsync", "unlink", "dir-fsync"],
            )
            self.assertEqual(receipt.stat().st_nlink, 1)

    def test_post_link_fsync_failure_preserves_and_reconciles_alias(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-receipt-reconcile-test-") as temporary:
            root = Path(temporary)
            with mock.patch.object(
                release_attempt,
                "_fsync_private_directory",
                side_effect=release_attempt.ReceiptPublicationIndeterminate(
                    "injected directory fsync failure"
                ),
            ):
                with self.assertRaisesRegex(
                    release_attempt.ReceiptPublicationIndeterminate,
                    "injected directory fsync failure",
                ):
                    release_attempt.write_receipt_no_replace(
                        root,
                        "receipt.json",
                        {"status": "linked"},
                    )

            destination = root / "receipt.json"
            aliases = list(root.glob(".receipt.json.*.tmp"))
            self.assertEqual(len(aliases), 1)
            self.assertEqual(destination.stat().st_nlink, 2)
            self.assertEqual(aliases[0].stat().st_ino, destination.stat().st_ino)

            self.assertEqual(
                release_attempt.read_receipt(destination),
                {"status": "linked"},
            )
            self.assertEqual(destination.stat().st_nlink, 1)
            self.assertEqual(list(root.glob(".receipt.json.*.tmp")), [])

    def test_post_link_unlink_failure_preserves_and_reconciles_alias(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-receipt-unlink-test-") as temporary:
            root = Path(temporary)
            real_unlink = os.unlink

            def fail_temporary_unlink(path, *args, **kwargs) -> None:
                if Path(path).name.startswith(".receipt.json."):
                    raise OSError("injected temporary unlink failure")
                real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                release_attempt.os,
                "unlink",
                side_effect=fail_temporary_unlink,
            ):
                with self.assertRaisesRegex(
                    release_attempt.ReceiptPublicationIndeterminate,
                    "temporary-alias cleanup is indeterminate",
                ):
                    release_attempt.write_receipt_no_replace(
                        root,
                        "receipt.json",
                        {"status": "linked"},
                    )

            destination = root / "receipt.json"
            self.assertEqual(destination.stat().st_nlink, 2)
            self.assertEqual(
                release_attempt.read_receipt(destination),
                {"status": "linked"},
            )
            self.assertEqual(destination.stat().st_nlink, 1)
            self.assertEqual(list(root.glob(".receipt.json.*.tmp")), [])

    def test_receipt_names_reject_control_and_path_syntax(self) -> None:
        if not self.runtime_is_available():
            return
        with tempfile.TemporaryDirectory(prefix="release-receipt-name-test-") as temporary:
            root = Path(temporary)
            for name in ("../receipt.json", "folder/receipt.json", "bad\\name.json", "bad\nname.json"):
                with self.subTest(name=name), self.assertRaisesRegex(
                    release_attempt.ReleaseAttemptError, "unsafe"
                ):
                    release_attempt.write_receipt_no_replace(root, name, {"ok": True})

    def test_binding_validation_rejects_malformed_values(self) -> None:
        invalid = (
            release_attempt.ReleaseBinding(
                **{**BINDING.__dict__, "repository": "owner/repo/extra"}
            ),
            release_attempt.ReleaseBinding(
                **{**BINDING.__dict__, "repository": "owner/repo\nother"}
            ),
            release_attempt.ReleaseBinding(
                **{**BINDING.__dict__, "tag": "v01.2.3"}
            ),
            release_attempt.ReleaseBinding(
                **{**BINDING.__dict__, "head_sha": "A" * 40}
            ),
            release_attempt.ReleaseBinding(
                **{**BINDING.__dict__, "run_attempt": 0}
            ),
        )
        for binding in invalid:
            with self.subTest(binding=binding):
                with self.assertRaises(release_attempt.ReleaseAttemptError):
                    release_attempt.validate_binding(binding)


if __name__ == "__main__":
    unittest.main()
