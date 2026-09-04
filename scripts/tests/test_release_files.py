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

    def test_exact_regular_file_set_accepts_only_the_named_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-set-") as temporary:
            directory = Path(temporary)
            (directory / "one.zip").write_bytes(b"one")
            (directory / "two.tar.gz").write_bytes(b"two")

            release_files.verify_exact_regular_file_set(
                directory,
                ("one.zip", "two.tar.gz"),
                label="test asset set",
            )

            (directory / "unexpected").write_bytes(b"extra")
            with self.assertRaisesRegex(
                release_files.ReleaseFileError,
                "extra=.*unexpected",
            ):
                release_files.verify_exact_regular_file_set(
                    directory,
                    ("one.zip", "two.tar.gz"),
                    label="test asset set",
                )

    def test_exact_regular_file_set_uses_named_path_link_count(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-set-links-") as temporary:
            directory = Path(temporary)
            path = directory / "one.zip"
            path.write_bytes(b"one")
            metadata = os.lstat(path)
            entry = mock.Mock()
            entry.name = path.name
            entry.stat.return_value = SimpleNamespace(
                st_mode=metadata.st_mode,
                st_size=metadata.st_size,
                st_nlink=0,
            )
            scanned = mock.MagicMock()
            scanned.__enter__.return_value = iter((entry,))

            with mock.patch.object(
                release_files.os,
                "scandir",
                return_value=scanned,
            ):
                release_files.verify_exact_regular_file_set(
                    directory,
                    (path.name,),
                    label="test asset set",
                )

            entry.stat.assert_called_once_with(follow_symlinks=False)

    def test_windows_style_directory_metadata_cannot_hide_a_hardlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-set-hardlink-") as temporary:
            directory = Path(temporary)
            source = directory / "source.zip"
            source.write_bytes(b"source")
            candidate = directory / "candidate.zip"
            os.link(source, candidate)
            entries = []
            for path in (source, candidate):
                metadata = os.lstat(path)
                entry = mock.Mock()
                entry.name = path.name
                entry.stat.return_value = SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_size=metadata.st_size,
                    st_nlink=0,
                )
                entries.append(entry)
            scanned = mock.MagicMock()
            scanned.__enter__.return_value = iter(entries)

            with mock.patch.object(
                release_files,
                "_WINDOWS",
                True,
            ), mock.patch.object(
                release_files.os,
                "scandir",
                return_value=scanned,
            ), self.assertRaisesRegex(
                release_files.ReleaseFileError,
                "non-empty regular non-symlink single-link",
            ) as raised:
                release_files.verify_exact_regular_file_set(
                    directory,
                    (source.name, candidate.name),
                    label="test asset set",
                )

            self.assertIsInstance(
                raised.exception.__cause__,
                release_files.ReleaseFileError,
            )
            self.assertIn("found 2", str(raised.exception.__cause__))

    def test_exact_regular_file_set_rejects_directory_replacement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-set-swap-") as temporary:
            root = Path(temporary)
            directory = root / "assets"
            directory.mkdir()
            (directory / "one.zip").write_bytes(b"original")
            moved = root / "moved-assets"
            real_scandir = os.scandir
            swapped = False

            def swap_after_open(path):
                nonlocal swapped
                scanned = real_scandir(path)
                if not swapped:
                    swapped = True
                    directory.rename(moved)
                    directory.mkdir()
                    (directory / "one.zip").write_bytes(b"replacement")
                return scanned

            with mock.patch.object(
                release_files.os,
                "scandir",
                side_effect=swap_after_open,
            ), self.assertRaisesRegex(
                release_files.ReleaseFileError,
                "directory changed while it was scanned",
            ):
                release_files.verify_exact_regular_file_set(
                    directory,
                    ("one.zip",),
                    label="test asset set",
                )

            self.assertTrue((moved / "one.zip").is_file())
            self.assertEqual((directory / "one.zip").read_bytes(), b"replacement")

    def test_exact_regular_file_set_rejects_missing_and_unsafe_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-set-missing-") as temporary:
            directory = Path(temporary)
            (directory / "one.zip").write_bytes(b"one")

            with self.assertRaisesRegex(release_files.ReleaseFileError, "missing=.*two"):
                release_files.verify_exact_regular_file_set(
                    directory,
                    ("one.zip", "two.zip"),
                    label="test asset set",
                )
            for unsafe in ("../one.zip", "nested/one.zip", "nested\\one.zip"):
                with self.subTest(unsafe=unsafe), self.assertRaisesRegex(
                    release_files.ReleaseFileError,
                    "unsafe expected name",
                ):
                    release_files.verify_exact_regular_file_set(
                        directory,
                        (unsafe,),
                        label="test asset set",
                    )

    def test_exact_regular_file_set_rejects_non_regular_or_linked_entries(self) -> None:
        mutations = ("empty", "directory", "hardlink", "symlink")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix="release-file-set-invalid-"
            ) as temporary:
                directory = Path(temporary)
                source = directory / "source.zip"
                source.write_bytes(b"source")
                candidate = directory / "candidate.zip"
                if mutation == "empty":
                    candidate.write_bytes(b"")
                elif mutation == "directory":
                    candidate.mkdir()
                elif mutation == "hardlink":
                    os.link(source, candidate)
                else:
                    try:
                        candidate.symlink_to(source.name)
                    except OSError as error:
                        self.skipTest(f"symlink creation is unavailable: {error}")

                with self.assertRaisesRegex(
                    release_files.ReleaseFileError,
                    "non-empty regular non-symlink single-link",
                ):
                    release_files.verify_exact_regular_file_set(
                        directory,
                        ("source.zip", "candidate.zip"),
                        label="test asset set",
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

    def test_windows_stable_open_compares_path_and_handle_snapshots_separately(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-windows-stat-") as temporary:
            source = Path(temporary) / "source"
            source.write_bytes(b"payload")
            named = os.lstat(source)
            opened = mock.Mock(
                st_mode=named.st_mode,
                st_ino=named.st_ino + 1,
                st_dev=named.st_dev + 1,
                st_nlink=named.st_nlink,
                st_size=named.st_size,
                st_mtime_ns=named.st_mtime_ns,
                st_ctime_ns=named.st_ctime_ns,
            )

            with mock.patch.object(
                release_files,
                "_WINDOWS",
                True,
            ), mock.patch.object(
                release_files.os,
                "fstat",
                return_value=opened,
            ):
                with release_files.open_stable_regular_file(
                    source,
                    label="test source",
                    require_single_link=True,
                ) as (input_file, snapshot):
                    self.assertEqual(input_file.read(), b"payload")
                    self.assertEqual(
                        snapshot,
                        release_files.FileSnapshot.from_stat(named),
                    )

    def test_windows_stable_open_requires_cross_channel_size_agreement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-file-windows-size-") as temporary:
            source = Path(temporary) / "source"
            source.write_bytes(b"payload")
            named = os.lstat(source)
            opened = mock.Mock(
                st_mode=named.st_mode,
                st_ino=named.st_ino + 1,
                st_dev=named.st_dev + 1,
                st_nlink=named.st_nlink,
                st_size=named.st_size + 1,
                st_mtime_ns=named.st_mtime_ns,
                st_ctime_ns=named.st_ctime_ns,
            )

            with mock.patch.object(
                release_files,
                "_WINDOWS",
                True,
            ), mock.patch.object(
                release_files.os,
                "fstat",
                return_value=opened,
            ):
                with self.assertRaisesRegex(
                    release_files.ReleaseFileError,
                    "size changed",
                ):
                    with release_files.open_stable_regular_file(
                        source,
                        label="test source",
                        require_single_link=True,
                    ):
                        self.fail("mismatched size must fail before yielding")

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
