from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_files


class ReleaseFileTests(unittest.TestCase):
    @staticmethod
    def metadata_with_changed_ctime(metadata: os.stat_result) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_size=metadata.st_size,
            st_mtime_ns=metadata.st_mtime_ns,
            st_ctime_ns=metadata.st_ctime_ns + 1,
            st_nlink=metadata.st_nlink,
        )

    def test_hash_rejects_ctime_change_even_when_size_and_mtime_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-hash-test-") as temporary:
            source = Path(temporary) / "source"
            source.write_bytes(b"stable bytes")
            metadata = os.stat(source)
            changed = self.metadata_with_changed_ctime(metadata)

            with mock.patch.object(
                release_files.os,
                "fstat",
                side_effect=(metadata, changed),
            ):
                with self.assertRaisesRegex(release_files.ReleaseFileError, "changed"):
                    release_files.sha256_file(source)

    @unittest.skipIf(
        os.name == "nt",
        "Windows rejects renaming this open file before a path swap can occur",
    )
    def test_stable_open_rejects_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-swap-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            moved = root / "moved"
            source.write_bytes(b"original")

            with self.assertRaisesRegex(release_files.ReleaseFileError, "changed"):
                with release_files.open_stable_regular_file(
                    source,
                    label="test source",
                    require_single_link=True,
                ) as (opened, _snapshot):
                    self.assertEqual(opened.read(), b"original")
                    source.rename(moved)
                    source.write_bytes(b"replacement")

    def test_stable_open_preserves_a_consumer_error_when_the_file_is_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-consumer-test-") as temporary:
            source = Path(temporary) / "source"
            source.write_bytes(b"original")
            sentinel = ValueError("consumer failed")

            with self.assertRaises(ValueError) as raised:
                with release_files.open_stable_regular_file(
                    source,
                    label="test source",
                    require_single_link=True,
                ):
                    raise sentinel

            self.assertIs(raised.exception, sentinel)

    @unittest.skipIf(
        os.name == "nt",
        "Windows rejects renaming this open file before a path swap can occur",
    )
    def test_stable_open_prioritizes_mutation_and_retains_consumer_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-consumer-swap-") as temporary:
            root = Path(temporary)
            source = root / "source"
            moved = root / "moved"
            source.write_bytes(b"original")
            sentinel = ValueError("consumer failed")

            with self.assertRaisesRegex(
                release_files.ReleaseFileError,
                "path changed",
            ) as raised:
                with release_files.open_stable_regular_file(
                    source,
                    label="test source",
                    require_single_link=True,
                ):
                    source.rename(moved)
                    source.write_bytes(b"replacement")
                    raise sentinel

            self.assertIs(raised.exception.__cause__, sentinel)

    @unittest.skipUnless(
        os.name == "nt",
        "Windows-specific open-file replacement semantics",
    )
    def test_windows_open_file_rejects_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-swap-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            moved = root / "moved"
            source.write_bytes(b"original")

            with release_files.open_stable_regular_file(
                source,
                label="test source",
                require_single_link=True,
            ) as (opened, _snapshot):
                self.assertEqual(opened.read(), b"original")
                with self.assertRaises(PermissionError):
                    source.rename(moved)
                self.assertTrue(source.exists())
                self.assertFalse(moved.exists())
            self.assertEqual(source.read_bytes(), b"original")

    def test_copy_is_exclusive_and_removes_a_failed_partial_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-copy-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.write_bytes(b"payload")

            release_files.copy_regular_file(
                source,
                destination,
                mode=0o644,
                label="test source",
            )
            self.assertEqual(destination.read_bytes(), b"payload")
            with self.assertRaises(FileExistsError):
                release_files.copy_regular_file(
                    source,
                    destination,
                    mode=0o644,
                    label="test source",
                )

            failed = root / "failed"
            with mock.patch.object(
                release_files.os,
                "fsync",
                side_effect=OSError("injected fsync failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected fsync failure"):
                    release_files.copy_regular_file(
                        source,
                        failed,
                        mode=0o644,
                        label="test source",
                    )
            self.assertFalse(failed.exists())

    def test_copy_rejects_changed_or_oversized_source_before_destination_creation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-bound-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.write_bytes(b"a")
            authorized = release_files.inspect_regular_file(
                source,
                label="test source",
                require_single_link=True,
            )
            source.write_bytes(b"expanded")

            with self.assertRaisesRegex(
                release_files.ReleaseFileError,
                "changed after it was authorized",
            ):
                release_files.copy_regular_file(
                    source,
                    destination,
                    mode=0o644,
                    label="test source",
                    expected_source=authorized,
                    maximum_bytes=4,
                )
            self.assertFalse(destination.exists())

            with self.assertRaisesRegex(
                release_files.ReleaseFileError,
                "exceeds its 4-byte copy limit",
            ):
                release_files.copy_regular_file(
                    source,
                    destination,
                    mode=0o644,
                    label="test source",
                    maximum_bytes=4,
                )
            self.assertFalse(destination.exists())

            source.write_bytes(b"same")
            authorized = release_files.inspect_regular_file(
                source,
                label="test source",
                require_single_link=True,
            )
            source.rename(root / "original-source")
            source.write_bytes(b"same")
            with self.assertRaisesRegex(
                release_files.ReleaseFileError,
                "changed after it was authorized",
            ):
                release_files.copy_regular_file(
                    source,
                    destination,
                    mode=0o644,
                    label="test source",
                    expected_source=authorized,
                    maximum_bytes=4,
                )
            self.assertFalse(destination.exists())

    def test_hard_linked_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-link-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            alias = root / "alias"
            source.write_bytes(b"payload")
            os.link(source, alias)

            with self.assertRaisesRegex(release_files.ReleaseFileError, "hard link"):
                release_files.sha256_file(source)

    def test_no_replace_publication_reconciles_success_and_late_error(self) -> None:
        for late_error in (False, True):
            with self.subTest(late_error=late_error):
                with tempfile.TemporaryDirectory(
                    prefix="release-file-publish-test-"
                ) as temporary:
                    root = Path(temporary)
                    source = root / "candidate"
                    destination = root / "published"
                    source.write_bytes(b"candidate")
                    real_link = os.link

                    def link_then_maybe_fail(*args, **kwargs):
                        real_link(*args, **kwargs)
                        if late_error:
                            raise OSError("injected late error")

                    with mock.patch.object(
                        release_files.os,
                        "link",
                        side_effect=link_then_maybe_fail,
                    ):
                        release_files.publish_sibling_no_replace(
                            source,
                            destination,
                            label="test artifact",
                        )

                    self.assertTrue(os.path.samefile(source, destination))
                    self.assertEqual(destination.read_bytes(), b"candidate")

    def test_no_replace_publication_classifies_collision_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-result-test-") as temporary:
            root = Path(temporary)
            source = root / "candidate"
            destination = root / "published"
            source.write_bytes(b"candidate")
            destination.write_bytes(b"existing")

            with self.assertRaises(release_files.FilePublicationCollision):
                release_files.publish_sibling_no_replace(
                    source,
                    destination,
                    label="test artifact",
                )
            self.assertEqual(destination.read_bytes(), b"existing")

            destination.unlink()
            with mock.patch.object(
                release_files.os,
                "link",
                side_effect=OSError("injected ambiguous failure"),
            ):
                with self.assertRaises(release_files.FilePublicationIndeterminate):
                    release_files.publish_sibling_no_replace(
                        source,
                        destination,
                        label="test artifact",
                    )
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_post_link_hash_failure_is_indeterminate_and_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-post-link-test-") as temporary:
            root = Path(temporary)
            source = root / "candidate"
            destination = root / "published"
            source.write_bytes(b"candidate")
            real_sha256_file = release_files.sha256_file
            calls = 0

            def fail_second_hash(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise release_files.ReleaseFileError(
                        "injected post-link hash failure"
                    )
                return real_sha256_file(*args, **kwargs)

            with mock.patch.object(
                release_files,
                "sha256_file",
                side_effect=fail_second_hash,
            ):
                with self.assertRaisesRegex(
                    release_files.FilePublicationIndeterminate,
                    "injected post-link hash failure",
                ):
                    release_files.publish_sibling_no_replace(
                        source,
                        destination,
                        label="test artifact",
                    )

            self.assertTrue(source.exists())
            self.assertTrue(destination.exists())
            self.assertTrue(os.path.samefile(source, destination))

    def test_concurrent_no_replace_publication_has_one_winner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-race-test-") as temporary:
            root = Path(temporary)
            sources = (root / "first", root / "second")
            sources[0].write_bytes(b"first")
            sources[1].write_bytes(b"second")
            destination = root / "published"
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            outcome_lock = threading.Lock()

            def publish(source: Path) -> None:
                barrier.wait()
                try:
                    release_files.publish_sibling_no_replace(
                        source,
                        destination,
                        label="test artifact",
                    )
                    outcome = "published"
                except release_files.FilePublicationCollision:
                    outcome = "collision"
                with outcome_lock:
                    outcomes.append(outcome)

            threads = [threading.Thread(target=publish, args=(source,)) for source in sources]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

            self.assertCountEqual(outcomes, ["published", "collision"])
            self.assertIn(destination.read_bytes(), {b"first", b"second"})


if __name__ == "__main__":
    unittest.main()
