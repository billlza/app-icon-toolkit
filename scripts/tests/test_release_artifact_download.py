from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
import zlib


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_artifact_download
import release_artifacts
import release_files


REPOSITORY = "example/app-icon-toolkit"
EXPECTED_NAME = "app-icon-toolkit-v1.2.3-test.zip"


def artifact_record(
    artifact_id: int,
    name: str,
    payload: bytes,
) -> release_artifacts.ArtifactRecord:
    return release_artifacts.ArtifactRecord(
        artifact_id=artifact_id,
        name=name,
        size_in_bytes=len(payload),
        archive_sha256=hashlib.sha256(payload).hexdigest(),
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        run_id=123,
        head_sha="0123456789abcdef0123456789abcdef01234567",
        head_branch="v1.2.3",
        repository_id=99,
        head_repository_id=99,
    )


def outer_zip(
    path: Path,
    members: tuple[tuple[zipfile.ZipInfo | str, bytes], ...],
) -> bytes:
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            archive.writestr(name, payload)
    return path.read_bytes()


class FakeDownloader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, int, Path, int, str]] = []

    def download(
        self,
        repository: str,
        artifact_id: int,
        destination: Path,
        *,
        expected_size: int,
        label: str,
    ) -> None:
        self.calls.append(
            (repository, artifact_id, destination, expected_size, label)
        )
        destination.write_bytes(self.payload)
        destination.chmod(0o600)


class ArtifactDownloadPlatformTests(unittest.TestCase):
    def test_non_posix_download_fails_before_creating_a_file_or_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-platform-test-") as temporary:
            destination = Path(temporary) / "artifact.partial"
            with mock.patch.object(
                release_artifact_download.os,
                "name",
                "nt",
            ), mock.patch.object(
                release_artifact_download.subprocess,
                "Popen",
            ) as popen, self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "require POSIX file-size limits",
            ):
                release_artifact_download.download_command_to_file(
                    ("download",),
                    destination,
                    expected_size=3,
                    label="unsupported platform artifact",
                    timeout_seconds=10,
                )

            popen.assert_not_called()
            self.assertFalse(destination.exists())


@unittest.skipUnless(
    os.name == "posix",
    "artifact download integration requires POSIX resource and mode semantics",
)
class ArtifactDownloadTests(unittest.TestCase):
    def test_binary_download_is_direct_size_limited_and_fsynced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-binary-download-") as temporary:
            root = Path(temporary)
            destination = root / "artifact.partial"
            release_artifact_download.download_command_to_file(
                (sys.executable, "-c", "import os; os.write(1, b'zip')"),
                destination,
                expected_size=3,
                label="test artifact",
                timeout_seconds=10,
            )
            self.assertEqual(destination.read_bytes(), b"zip")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

            oversized = root / "oversized.partial"
            with self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "artifact download",
            ):
                release_artifact_download.download_command_to_file(
                    (
                        sys.executable,
                        "-c",
                        (
                            "import os; data=b'too large'; offset=0; "
                            "\nwhile offset < len(data): "
                            "offset += os.write(1, data[offset:])"
                        ),
                    ),
                    oversized,
                    expected_size=3,
                    label="oversized artifact",
                    timeout_seconds=10,
                )
            self.assertLessEqual(oversized.stat().st_size, 3)

            invalid_utf8 = root / "invalid-utf8.partial"
            with self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "diagnostics are not valid UTF-8",
            ):
                release_artifact_download.download_command_to_file(
                    (
                        sys.executable,
                        "-c",
                        "import os; os.write(1,b'zip'); os.write(2,b'\\xff')",
                    ),
                    invalid_utf8,
                    expected_size=3,
                    label="invalid diagnostic artifact",
                    timeout_seconds=10,
                )

            timed_out = root / "timed-out.partial"
            with self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "timed out",
            ):
                release_artifact_download.download_command_to_file(
                    (
                        sys.executable,
                        "-c",
                        "import time; time.sleep(2)",
                    ),
                    timed_out,
                    expected_size=3,
                    label="timed out artifact",
                    timeout_seconds=1,
                )

    def test_binary_download_wraps_local_io_failure_and_reaps_child(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-download-io-") as temporary:
            root = Path(temporary)
            with mock.patch.object(
                Path,
                "open",
                side_effect=OSError("injected open failure"),
            ), self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "local artifact download I/O failed.*injected open failure",
            ):
                release_artifact_download.download_command_to_file(
                    ("/usr/bin/false",),
                    root / "open-failure.partial",
                    expected_size=3,
                    label="open failure artifact",
                    timeout_seconds=10,
                )

            class CompletedProcess:
                def __init__(self) -> None:
                    self.stderr = io.BytesIO(b"")
                    self.returncode: int | None = None
                    self.wait_count = 0
                    self.kill_count = 0

                def wait(self, timeout: int | None = None) -> int:
                    del timeout
                    self.wait_count += 1
                    self.returncode = 0
                    return 0

                def poll(self) -> int | None:
                    return self.returncode

                def kill(self) -> None:
                    self.kill_count += 1
                    self.returncode = -9

            process = CompletedProcess()
            fsync_failure = root / "fsync-failure.partial"
            with mock.patch.object(
                release_artifact_download.subprocess,
                "Popen",
                return_value=process,
            ), mock.patch.object(
                release_artifact_download.os,
                "fsync",
                side_effect=OSError("injected fsync failure"),
            ), self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "local artifact download I/O failed.*injected fsync failure",
            ):
                release_artifact_download.download_command_to_file(
                    ("/usr/bin/false",),
                    fsync_failure,
                    expected_size=3,
                    label="fsync failure artifact",
                    timeout_seconds=10,
                )
            self.assertEqual(process.wait_count, 1)
            self.assertEqual(process.kill_count, 0)
            self.assertEqual(process.returncode, 0)
            self.assertTrue(process.stderr.closed)
            self.assertTrue(fsync_failure.exists())

    def test_github_runner_uses_absolute_binary_hostname_and_numeric_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-gh-command-") as temporary:
            root = Path(temporary)
            executable = root / "gh"
            executable.write_bytes(b"#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            runner = release_artifact_download.GitHubArtifactZipDownloader(
                executable=executable,
                timeout_seconds=30,
            )
            destination = root / "artifact.partial"
            with mock.patch.object(
                release_artifact_download,
                "download_command_to_file",
            ) as download:
                runner.download(
                    REPOSITORY,
                    987,
                    destination,
                    expected_size=123,
                    label="numeric artifact",
                )
            download.assert_called_once_with(
                (
                    str(executable.resolve()),
                    "api",
                    "--hostname",
                    "github.com",
                    "repos/example/app-icon-toolkit/actions/artifacts/987/zip",
                ),
                destination,
                expected_size=123,
                label="numeric artifact",
                timeout_seconds=30,
            )
            with self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "absolute executable",
            ):
                release_artifact_download.GitHubArtifactZipDownloader(
                    executable=Path("gh")
                )

    def test_numeric_artifact_download_is_digest_bound_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-id-digest-") as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir(mode=0o700)
            source = root / "source.zip"
            payload = outer_zip(source, ((EXPECTED_NAME, b"public archive"),))
            record = artifact_record(
                987,
                "app-icon-toolkit-test-attempt-1",
                payload,
            )
            downloader = FakeDownloader(payload)

            first = release_artifact_download.obtain_artifact_zip(
                REPOSITORY,
                record,
                cache,
                downloader,
            )
            resumed = release_artifact_download.obtain_artifact_zip(
                REPOSITORY,
                record,
                cache,
                downloader,
            )
            self.assertEqual(first, resumed)
            self.assertEqual(first.read_bytes(), payload)
            self.assertEqual(len(downloader.calls), 1)
            repository, artifact_id, temporary_path, size, label = downloader.calls[0]
            self.assertEqual((repository, artifact_id, size), (REPOSITORY, 987, len(payload)))
            self.assertIn("GitHub artifact", label)
            self.assertRegex(
                temporary_path.name,
                r"^\.artifact-987\.[0-9a-f]{32}\.partial$",
            )
            self.assertNotIn(record.name, os.fspath(temporary_path))

            corrupt_cache = root / "corrupt-cache"
            corrupt_cache.mkdir(mode=0o700)
            wrong_digest = replace(record, artifact_id=988, archive_sha256="f" * 64)
            with self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "SHA-256 differs from API metadata",
            ):
                release_artifact_download.obtain_artifact_zip(
                    REPOSITORY,
                    wrong_digest,
                    corrupt_cache,
                    FakeDownloader(payload),
                )
            self.assertEqual(list(corrupt_cache.iterdir()), [])

            class FailedDownloader:
                def download(
                    self,
                    repository: str,
                    artifact_id: int,
                    destination: Path,
                    *,
                    expected_size: int,
                    label: str,
                ) -> None:
                    del repository, artifact_id, expected_size, label
                    destination.write_bytes(b"known failed response")
                    destination.chmod(0o600)
                    raise release_artifact_download.ArtifactDownloadError(
                        "injected GET failure"
                    )

            failed_cache = root / "failed-cache"
            failed_cache.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "injected GET failure",
            ):
                release_artifact_download.obtain_artifact_zip(
                    REPOSITORY,
                    replace(record, artifact_id=989),
                    failed_cache,
                    FailedDownloader(),
                )
            self.assertEqual(list(failed_cache.iterdir()), [])

    def test_sigkill_residue_is_reconciled_without_poisoning_attempt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-sigkill-resume-") as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir(mode=0o700)
            source = root / "source.zip"
            payload = outer_zip(source, ((EXPECTED_NAME, b"public archive"),))
            record = artifact_record(
                777,
                "app-icon-toolkit-test-attempt-1",
                payload,
            )
            incomplete = cache / f".artifact-777.{'a' * 32}.partial"
            incomplete.write_bytes(payload[:10])
            incomplete.chmod(0o600)
            downloader = FakeDownloader(payload)

            result = release_artifact_download.obtain_artifact_zip(
                REPOSITORY,
                record,
                cache,
                downloader,
            )
            self.assertEqual(result.read_bytes(), payload)
            self.assertFalse(incomplete.exists())
            self.assertEqual(len(downloader.calls), 1)

            resumed_cache = root / "resumed-cache"
            resumed_cache.mkdir(mode=0o700)
            complete = resumed_cache / f".artifact-777.{'b' * 32}.partial"
            complete.write_bytes(payload)
            complete.chmod(0o600)
            no_download = FakeDownloader(b"wrong")
            resumed = release_artifact_download.obtain_artifact_zip(
                REPOSITORY,
                record,
                resumed_cache,
                no_download,
            )
            self.assertEqual(resumed.read_bytes(), payload)
            self.assertEqual(no_download.calls, [])
            self.assertFalse(complete.exists())

    def test_indeterminate_publication_preserves_reconcilable_temporary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-indeterminate-") as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir(mode=0o700)
            source = root / "source.zip"
            payload = outer_zip(source, ((EXPECTED_NAME, b"public archive"),))
            record = artifact_record(
                778,
                "app-icon-toolkit-test-attempt-1",
                payload,
            )
            with mock.patch.object(
                release_artifact_download,
                "publish_sibling_no_replace",
                side_effect=release_files.FilePublicationIndeterminate(
                    "injected indeterminate publication"
                ),
            ), self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "injected indeterminate publication",
            ):
                release_artifact_download.obtain_artifact_zip(
                    REPOSITORY,
                    record,
                    cache,
                    FakeDownloader(payload),
                )
            partials = tuple(cache.glob(".artifact-778.*.partial"))
            self.assertEqual(len(partials), 1)
            self.assertEqual(partials[0].read_bytes(), payload)

            resumed = release_artifact_download.obtain_artifact_zip(
                REPOSITORY,
                record,
                cache,
                FakeDownloader(b"wrong"),
            )
            self.assertEqual(resumed.read_bytes(), payload)
            self.assertEqual(tuple(cache.glob("*.partial")), ())

    def test_unique_member_is_published_and_link_alias_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-member-") as temporary:
            root = Path(temporary)
            outputs = root / "outputs"
            outputs.mkdir(mode=0o700)
            outer = root / "artifact.zip"
            outer_zip(outer, ((EXPECTED_NAME, b"public archive bytes"),))

            destination = release_artifact_download.extract_public_archive(
                outer,
                EXPECTED_NAME,
                321,
                outputs,
            )
            self.assertEqual(destination.read_bytes(), b"public archive bytes")
            self.assertEqual(destination.stat().st_nlink, 1)
            self.assertEqual(
                release_artifact_download.extract_public_archive(
                    outer,
                    EXPECTED_NAME,
                    321,
                    outputs,
                ),
                destination,
            )

            recovered_outputs = root / "recovered-outputs"
            recovered_outputs.mkdir(mode=0o700)
            partial = recovered_outputs / f".{EXPECTED_NAME}.artifact-321.{'c' * 32}.partial"
            final = recovered_outputs / EXPECTED_NAME
            partial.write_bytes(b"public archive bytes")
            partial.chmod(0o600)
            os.link(partial, final)
            recovered = release_artifact_download.extract_public_archive(
                outer,
                EXPECTED_NAME,
                321,
                recovered_outputs,
            )
            self.assertEqual(recovered, final)
            self.assertFalse(partial.exists())
            self.assertEqual(final.stat().st_nlink, 1)

    def test_outer_zip_rejects_contamination_links_encryption_and_oversize(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-contamination-") as temporary:
            root = Path(temporary)
            symlink = zipfile.ZipInfo(EXPECTED_NAME)
            symlink.create_system = 3
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            cases: tuple[
                tuple[str, tuple[tuple[zipfile.ZipInfo | str, bytes], ...], str],
                ...,
            ] = (
                (
                    "traversal",
                    (("../../app-icon-toolkit-v1.2.3-test.zip", b"bad"),),
                    "exactly the expected",
                ),
                (
                    "extra",
                    ((EXPECTED_NAME, b"good"), ("extra.txt", b"bad")),
                    "one non-ZIP64",
                ),
                (
                    "symlink",
                    ((symlink, b"target"),),
                    "not an ordinary file",
                ),
            )
            for case_name, members, message in cases:
                with self.subTest(case=case_name):
                    outer = root / f"{case_name}.zip"
                    outer_zip(outer, members)
                    outputs = root / f"{case_name}-outputs"
                    outputs.mkdir(mode=0o700)
                    with self.assertRaisesRegex(
                        release_artifact_download.ArtifactDownloadError,
                        message,
                    ):
                        release_artifact_download.extract_public_archive(
                            outer,
                            EXPECTED_NAME,
                            123,
                            outputs,
                        )
                    self.assertEqual(list(outputs.iterdir()), [])

            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(
                duplicate,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr(EXPECTED_NAME, b"first")
                archive.NameToInfo.pop(EXPECTED_NAME)
                archive.writestr(EXPECTED_NAME, b"second")
            duplicate_outputs = root / "duplicate-outputs"
            duplicate_outputs.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "one non-ZIP64",
            ):
                release_artifact_download.extract_public_archive(
                    duplicate,
                    EXPECTED_NAME,
                    124,
                    duplicate_outputs,
                )

            encrypted = root / "encrypted.zip"
            outer_zip(encrypted, ((EXPECTED_NAME, b"encrypted marker"),))
            encrypted_bytes = bytearray(encrypted.read_bytes())
            for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
                header = encrypted_bytes.index(signature)
                flags = int.from_bytes(
                    encrypted_bytes[header + flag_offset : header + flag_offset + 2],
                    "little",
                )
                encrypted_bytes[header + flag_offset : header + flag_offset + 2] = (
                    flags | 0x1
                ).to_bytes(2, "little")
            encrypted.write_bytes(encrypted_bytes)
            encrypted_outputs = root / "encrypted-outputs"
            encrypted_outputs.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "member is encrypted",
            ):
                release_artifact_download.extract_public_archive(
                    encrypted,
                    EXPECTED_NAME,
                    125,
                    encrypted_outputs,
                )

            oversized = root / "oversized.zip"
            outer_zip(oversized, ((EXPECTED_NAME, b"1234"),))
            oversized_outputs = root / "oversized-outputs"
            oversized_outputs.mkdir(mode=0o700)
            with mock.patch.object(release_artifact_download, "MAX_ARCHIVE_BYTES", 3):
                with self.assertRaisesRegex(
                    release_artifact_download.ArtifactDownloadError,
                    "size is outside the limit",
                ):
                    release_artifact_download.extract_public_archive(
                        oversized,
                        EXPECTED_NAME,
                        126,
                        oversized_outputs,
                    )

    def test_outer_zip_scans_actual_records_before_standard_parser(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-central-count-") as temporary:
            root = Path(temporary)
            outer = root / "forged-count.zip"
            members = [(EXPECTED_NAME, b"public archive")]
            members.extend(
                (f"hidden-{index}.txt", b"metadata") for index in range(128)
            )
            outer_zip(outer, tuple(members))
            forged = bytearray(outer.read_bytes())
            eocd_offset = forged.rfind(b"PK\x05\x06")
            self.assertGreaterEqual(eocd_offset, 0)
            forged[eocd_offset + 8 : eocd_offset + 10] = (1).to_bytes(2, "little")
            forged[eocd_offset + 10 : eocd_offset + 12] = (1).to_bytes(2, "little")
            outer.write_bytes(forged)
            outputs = root / "outputs"
            outputs.mkdir(mode=0o700)

            with mock.patch.object(
                release_artifact_download.zipfile,
                "ZipFile",
            ) as standard_parser:
                with self.assertRaisesRegex(
                    release_artifact_download.ArtifactDownloadError,
                    "unexpected number of records",
                ):
                    release_artifact_download.extract_public_archive(
                        outer,
                        EXPECTED_NAME,
                        127,
                        outputs,
                    )
            standard_parser.assert_not_called()
            self.assertEqual(list(outputs.iterdir()), [])

    def test_outer_zip_rejects_lzma_before_standard_parser(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-method-bound-") as temporary:
            root = Path(temporary)
            outer = root / "lzma.zip"
            information = zipfile.ZipInfo(EXPECTED_NAME)
            information.compress_type = zipfile.ZIP_LZMA
            outer_zip(outer, ((information, b"\0" * (2 * 1024 * 1024)),))
            outputs = root / "outputs"
            outputs.mkdir(mode=0o700)

            with mock.patch.object(
                release_artifact_download.zipfile,
                "ZipFile",
            ) as standard_parser:
                with self.assertRaisesRegex(
                    release_artifact_download.ArtifactDownloadError,
                    "unsupported compression method",
                ):
                    release_artifact_download.extract_public_archive(
                        outer,
                        EXPECTED_NAME,
                        128,
                        outputs,
                    )
            standard_parser.assert_not_called()
            self.assertEqual(list(outputs.iterdir()), [])

    def test_outer_zip_rejects_bzip2_before_standard_parser(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-bzip2-bound-") as temporary:
            root = Path(temporary)
            outer = root / "bzip2.zip"
            information = zipfile.ZipInfo(EXPECTED_NAME)
            information.compress_type = zipfile.ZIP_BZIP2
            outer_zip(outer, ((information, b"public archive"),))
            outputs = root / "outputs"
            outputs.mkdir(mode=0o700)

            with mock.patch.object(
                release_artifact_download.zipfile,
                "ZipFile",
            ) as standard_parser:
                with self.assertRaisesRegex(
                    release_artifact_download.ArtifactDownloadError,
                    "unsupported compression method",
                ):
                    release_artifact_download.extract_public_archive(
                        outer,
                        EXPECTED_NAME,
                        129,
                        outputs,
                    )
            standard_parser.assert_not_called()
            self.assertEqual(list(outputs.iterdir()), [])

    def test_crc_failure_is_known_and_partial_output_is_removed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-crc-") as temporary:
            root = Path(temporary)
            payload = b"unique-public-archive-payload"
            outer = root / "artifact.zip"
            with zipfile.ZipFile(outer, mode="w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr(EXPECTED_NAME, payload)
            corrupted = bytearray(outer.read_bytes())
            payload_offset = corrupted.index(payload)
            corrupted[payload_offset] ^= 0x01
            outer.write_bytes(corrupted)
            outputs = root / "outputs"
            outputs.mkdir(mode=0o700)

            with self.assertRaisesRegex(
                release_artifact_download.ArtifactDownloadError,
                "Bad CRC-32",
            ):
                release_artifact_download.extract_public_archive(
                    outer,
                    EXPECTED_NAME,
                    200,
                    outputs,
                )
            self.assertEqual(list(outputs.iterdir()), [])

    def test_deflate_failure_is_typed_cleaned_and_retriable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-deflate-") as temporary:
            root = Path(temporary)
            outer = root / "artifact.zip"
            outer_zip(outer, ((EXPECTED_NAME, b"public archive" * 4096),))
            corrupted = bytearray(outer.read_bytes())
            local_header = corrupted.index(b"PK\x03\x04")
            name_size = int.from_bytes(
                corrupted[local_header + 26 : local_header + 28],
                "little",
            )
            extra_size = int.from_bytes(
                corrupted[local_header + 28 : local_header + 30],
                "little",
            )
            payload_offset = local_header + 30 + name_size + extra_size
            corrupted[payload_offset] ^= 0xFF
            outer.write_bytes(corrupted)
            outputs = root / "outputs"
            outputs.mkdir(mode=0o700)

            for _attempt in range(2):
                with self.assertRaises(
                    release_artifact_download.ArtifactDownloadError
                ) as raised:
                    release_artifact_download.extract_public_archive(
                        outer,
                        EXPECTED_NAME,
                        202,
                        outputs,
                    )
                self.assertIsInstance(raised.exception.__cause__, zlib.error)
                self.assertEqual(list(outputs.iterdir()), [])

    def test_distinct_stale_partial_does_not_poison_verified_destination(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-stale-partial-") as temporary:
            root = Path(temporary)
            outer = root / "artifact.zip"
            outer_zip(outer, ((EXPECTED_NAME, b"public archive"),))
            outputs = root / "outputs"
            outputs.mkdir(mode=0o700)
            destination = outputs / EXPECTED_NAME
            destination.write_bytes(b"public archive")
            destination.chmod(0o600)
            partial = outputs / f".{EXPECTED_NAME}.artifact-201.{'d' * 32}.partial"
            partial.write_bytes(b"incomplete")
            partial.chmod(0o600)

            result = release_artifact_download.extract_public_archive(
                outer,
                EXPECTED_NAME,
                201,
                outputs,
            )
            self.assertEqual(result, destination)
            self.assertFalse(partial.exists())
            self.assertEqual(destination.read_bytes(), b"public archive")

    def test_archive_name_and_id_are_validated_before_output_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-path-validation-") as temporary:
            root = Path(temporary)
            outer = root / "artifact.zip"
            outer_zip(outer, ((EXPECTED_NAME, b"public archive"),))
            outputs = root / "outputs"
            outputs.mkdir(mode=0o700)

            for name, artifact_id in (("../escape.zip", 1), (EXPECTED_NAME, -1)):
                with self.subTest(name=name, artifact_id=artifact_id):
                    with self.assertRaises(
                        release_artifact_download.ArtifactDownloadError
                    ):
                        release_artifact_download.extract_public_archive(
                            outer,
                            name,
                            artifact_id,
                            outputs,
                        )
            self.assertEqual(list(outputs.iterdir()), [])
            self.assertFalse((root / "escape.zip").exists())


if __name__ == "__main__":
    unittest.main()
