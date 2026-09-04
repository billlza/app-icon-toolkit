from __future__ import annotations

import gzip
import io
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile
import zlib

from release_test_support import (
    create_symlink_or_skip,
    prepare_release_package,
    release_package,
    release_targets,
    release_zip_preflight,
)


class ReleaseArchiveRoundTripTests(unittest.TestCase):
    @staticmethod
    def write_tar_member(
        archive: tarfile.TarFile,
        name: str,
        data: bytes,
        kind: bytes = tarfile.REGTYPE,
        linkname: str = "",
    ) -> None:
        information = tarfile.TarInfo(name)
        information.type = kind
        information.mode = 0o644
        information.linkname = linkname
        information.size = len(data) if kind == tarfile.REGTYPE else 0
        archive.addfile(information, io.BytesIO(data) if data else None)

    @staticmethod
    def zip_member(name: str, mode: int = 0o644) -> zipfile.ZipInfo:
        information = zipfile.ZipInfo(name)
        information.create_system = 3
        information.external_attr = mode << 16
        information.compress_type = zipfile.ZIP_DEFLATED
        return information

    def test_static_release_input_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-input-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"source")
            symlink = root / "symlink"
            create_symlink_or_skip(self, symlink, source)
            with self.assertRaisesRegex(RuntimeError, "ordinary non-symlink"):
                prepare_release_package.validate_static_input(symlink)

    def test_static_release_input_rejects_empty_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-input-test-") as temporary:
            root = Path(temporary)
            empty = root / "empty"
            empty.write_bytes(b"")
            with self.assertRaisesRegex(RuntimeError, "non-empty"):
                prepare_release_package.validate_static_input(empty)

    @staticmethod
    def create_minimal_plugin(root: Path) -> tuple[Path, Path]:
        plugin = root / "plugin"
        for relative in prepare_release_package.STATIC_PATHS:
            source = plugin / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"{relative.as_posix()}\n", encoding="utf-8")
        binary = root / "app-icon-toolkit-mcp"
        binary.write_bytes(b"test-binary")
        binary.chmod(0o755)
        return plugin, binary

    def test_failed_smoke_never_publishes_or_leaves_archive_temporary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-atomic-test-") as temporary:
            root = Path(temporary)
            plugin, binary = self.create_minimal_plugin(root)
            output = root / "dist"
            output.mkdir()
            target = release_targets.load_contract(
                release_targets.CONTRACT_PATH
            ).target("aarch64-apple-darwin")

            with mock.patch.object(
                prepare_release_package,
                "verify_archive",
                side_effect=RuntimeError("smoke failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "smoke failed"):
                    prepare_release_package.prepare_release_archive(
                        plugin,
                        binary,
                        target,
                        "v1.2.3",
                        output,
                        1_700_000_000,
                        prepare_release_package.ArchiveVerificationMode.RUNTIME_SMOKE,
                    )

            destination = output / target.release_filename("v1.2.3")
            self.assertFalse(destination.exists())
            self.assertEqual(list(output.iterdir()), [])

    def test_failed_archive_write_never_publishes_or_leaves_temporary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-write-test-") as temporary:
            root = Path(temporary)
            plugin, binary = self.create_minimal_plugin(root)
            output = root / "dist"
            output.mkdir()
            target = release_targets.load_contract(
                release_targets.CONTRACT_PATH
            ).target("aarch64-apple-darwin")

            def fail_after_write(_package, raw_output, _epoch, _archive_format):
                raw_output.write(b"partial")
                raise RuntimeError("injected archive failure")

            with mock.patch.object(
                prepare_release_package,
                "_write_archive",
                side_effect=fail_after_write,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected archive failure"):
                    prepare_release_package.prepare_release_archive(
                        plugin,
                        binary,
                        target,
                        "v1.2.3",
                        output,
                        1_700_000_000,
                        prepare_release_package.ArchiveVerificationMode.RUNTIME_SMOKE,
                    )

            self.assertEqual(list(output.iterdir()), [])

    def test_package_inputs_are_bounded_before_any_copy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-input-bound-") as temporary:
            root = Path(temporary)
            plugin, binary = self.create_minimal_plugin(root)
            package = root / "package"
            target = release_targets.load_contract(
                release_targets.CONTRACT_PATH
            ).target("aarch64-apple-darwin")

            with mock.patch.object(
                prepare_release_package,
                "MAX_MEMBER_BYTES",
                1,
            ):
                with self.assertRaisesRegex(RuntimeError, "member exceeds"):
                    prepare_release_package.copy_package(
                        plugin,
                        package,
                        binary,
                        target,
                    )

            self.assertFalse(package.exists())

    def test_package_copy_rejects_source_growth_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-input-race-") as temporary:
            root = Path(temporary)
            plugin, binary = self.create_minimal_plugin(root)
            package = root / "package"
            target = release_targets.load_contract(
                release_targets.CONTRACT_PATH
            ).target("aarch64-apple-darwin")
            original_copy = prepare_release_package.copy_regular_file
            grew_source = False

            def grow_before_copy(source, destination, **kwargs):
                nonlocal grew_source
                if not grew_source:
                    Path(source).write_bytes(b"x" * 1_025)
                    grew_source = True
                return original_copy(source, destination, **kwargs)

            with mock.patch.object(
                prepare_release_package,
                "MAX_MEMBER_BYTES",
                1_024,
            ), mock.patch.object(
                prepare_release_package,
                "copy_regular_file",
                side_effect=grow_before_copy,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "changed after it was authorized",
                ):
                    prepare_release_package.copy_package(
                        plugin,
                        package,
                        binary,
                        target,
                    )

            self.assertTrue(grew_source)
            self.assertFalse(any(path.is_file() for path in package.rglob("*")))

    def test_successful_prepare_publishes_only_the_verified_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-success-test-") as temporary:
            root = Path(temporary)
            plugin, binary = self.create_minimal_plugin(root)
            output = root / "dist"
            output.mkdir()
            target = release_targets.load_contract(
                release_targets.CONTRACT_PATH
            ).target("aarch64-apple-darwin")

            with mock.patch.object(
                prepare_release_package.subprocess,
                "run",
            ) as run:
                destination = prepare_release_package.prepare_release_archive(
                    plugin,
                    binary,
                    target,
                    "v1.2.3",
                    output,
                    1_700_000_000,
                    prepare_release_package.ArchiveVerificationMode.RUNTIME_SMOKE,
                )

            run.assert_called_once()
            self.assertEqual(list(output.iterdir()), [destination])
            self.assertGreater(destination.stat().st_size, 0)
            self.assertEqual(destination.stat().st_nlink, 1)

    def test_archive_publication_never_replaces_an_existing_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-collision-test-") as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            destination = root / "destination"
            candidate.write_bytes(b"candidate")
            destination.write_bytes(b"existing")

            with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                prepare_release_package.publish_archive_no_replace(
                    candidate, destination
                )

            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertEqual(candidate.read_bytes(), b"candidate")

    def test_indeterminate_publication_preserves_the_complete_temporary_archive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="release-unknown-test-") as temporary:
            root = Path(temporary)
            plugin, binary = self.create_minimal_plugin(root)
            output = root / "dist"
            output.mkdir()
            target = release_targets.load_contract(
                release_targets.CONTRACT_PATH
            ).target("aarch64-apple-darwin")

            with mock.patch.object(
                prepare_release_package.subprocess,
                "run",
            ), mock.patch.object(
                prepare_release_package,
                "publish_archive_no_replace",
                side_effect=prepare_release_package.FilePublicationIndeterminate(
                    "injected unknown publication"
                ),
            ):
                with self.assertRaises(
                    prepare_release_package.FilePublicationIndeterminate
                ):
                    prepare_release_package.prepare_release_archive(
                        plugin,
                        binary,
                        target,
                        "v1.2.3",
                        output,
                        1_700_000_000,
                        prepare_release_package.ArchiveVerificationMode.RUNTIME_SMOKE,
                    )

            final = output / target.release_filename("v1.2.3")
            preserved = list(output.glob(f".{final.name}.*.tmp"))
            self.assertFalse(final.exists())
            self.assertEqual(len(preserved), 1)
            self.assertGreater(preserved[0].stat().st_size, 0)

    def test_unsafe_release_tag_is_rejected_before_packaging(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-tag-test-") as temporary:
            root = Path(temporary)
            plugin, binary = self.create_minimal_plugin(root)
            output = root / "dist"
            output.mkdir()
            target = release_targets.load_contract(
                release_targets.CONTRACT_PATH
            ).target("aarch64-apple-darwin")

            invalid_tags = (
                "../v1.2.3",
                "v1.2",
                "v01.2.3",
                "v1.02.3",
                "v1.2.03",
                "v1.2.3-rc.1",
                "v1.2.3\nasset",
                "v1.2.3 *",
                "v1:2:3",
            )
            for tag in invalid_tags:
                with self.subTest(tag=tag):
                    with self.assertRaisesRegex(
                        RuntimeError, "stable semantic version"
                    ):
                        prepare_release_package.prepare_release_archive(
                            plugin,
                            binary,
                            target,
                            tag,
                            output,
                            1_700_000_000,
                            prepare_release_package.ArchiveVerificationMode.RUNTIME_SMOKE,
                        )
            self.assertEqual(list(output.iterdir()), [])

    def test_every_archive_format_is_extracted_verified_and_smoked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-round-trip-test-") as temporary:
            root = Path(temporary)
            package = root / "source-package"
            (package / "bin").mkdir(parents=True)
            (package / ".mcp.json").write_text("{}\n", encoding="utf-8")
            (package / "bin" / "app-icon-toolkit-mcp").write_bytes(b"binary")

            for archive_format in ("tar.gz", "zip"):
                with self.subTest(archive_format=archive_format):
                    archive = root / f"candidate.{archive_format}"
                    if archive_format == "tar.gz":
                        prepare_release_package.write_tar_gz(package, archive, 1_700_000_000)
                    else:
                        prepare_release_package.write_zip(package, archive, 1_700_000_000)

                    def inspect_smoke(command, *, check):
                        self.assertTrue(check)
                        extracted = Path(command[-1])
                        self.assertEqual(
                            (extracted / ".mcp.json").read_text(encoding="utf-8"),
                            "{}\n",
                        )
                        self.assertEqual(
                            (extracted / "bin" / "app-icon-toolkit-mcp").read_bytes(),
                            b"binary",
                        )

                    with mock.patch.object(
                        prepare_release_package.subprocess,
                        "run",
                        side_effect=inspect_smoke,
                    ) as run:
                        prepare_release_package.verify_archive(
                            Path("/plugin"),
                            package,
                            archive,
                            archive_format,
                            prepare_release_package.ArchiveVerificationMode.RUNTIME_SMOKE,
                        )
                    run.assert_called_once()

    def test_static_only_archive_verification_never_executes_candidate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-static-only-test-") as temporary:
            root = Path(temporary)
            package = root / "source-package"
            (package / "bin").mkdir(parents=True)
            (package / ".mcp.json").write_text("{}\n", encoding="utf-8")
            (package / "bin" / "app-icon-toolkit-mcp").write_bytes(b"binary")
            archive = root / "candidate.zip"
            prepare_release_package.write_zip(package, archive, 1_700_000_000)

            with mock.patch.object(
                prepare_release_package.subprocess,
                "run",
            ) as run:
                prepare_release_package.verify_archive(
                    Path("/plugin"),
                    package,
                    archive,
                    "zip",
                    prepare_release_package.ArchiveVerificationMode.STATIC_ONLY,
                )

            run.assert_not_called()

    def test_archive_verification_rejects_untyped_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-mode-test-") as temporary:
            root = Path(temporary)
            package = root / "source-package"
            (package / "bin").mkdir(parents=True)
            (package / "bin" / "app-icon-toolkit-mcp").write_bytes(b"binary")
            archive = root / "candidate.zip"
            prepare_release_package.write_zip(package, archive, 1_700_000_000)

            with self.assertRaisesRegex(RuntimeError, "ArchiveVerificationMode"):
                prepare_release_package.verify_archive(
                    Path("/plugin"),
                    package,
                    archive,
                    "zip",
                    "static-only",
                )

    def test_archive_writers_are_byte_deterministic_for_the_same_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-determinism-test-") as temporary:
            root = Path(temporary)
            package = root / "source-package"
            (package / "bin").mkdir(parents=True)
            (package / "README.md").write_bytes(b"documentation\n")
            (package / "bin" / "app-icon-toolkit-mcp").write_bytes(b"binary")

            for archive_format, writer in (
                ("tar.gz", prepare_release_package.write_tar_gz),
                ("zip", prepare_release_package.write_zip),
            ):
                with self.subTest(archive_format=archive_format):
                    first = root / f"first.{archive_format}"
                    second = root / f"second.{archive_format}"
                    writer(package, first, 1_700_000_000)
                    writer(package, second, 1_700_000_000)
                    self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_tar_writer_emits_only_ordinary_ustar_headers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-tar-format-test-") as temporary:
            root = Path(temporary)
            package = root / "source-package"
            (package / "bin").mkdir(parents=True)
            (package / "README.md").write_bytes(b"documentation\n")
            (package / "bin" / "app-icon-toolkit-mcp").write_bytes(b"binary")
            archive_path = root / "candidate.tar.gz"
            prepare_release_package.write_tar_gz(
                package,
                archive_path,
                1_700_000_000,
            )

            member_types: list[bytes] = []
            with gzip.open(archive_path, mode="rb") as archive:
                while True:
                    header = archive.read(tarfile.BLOCKSIZE)
                    self.assertEqual(len(header), tarfile.BLOCKSIZE)
                    if header == tarfile.NUL * tarfile.BLOCKSIZE:
                        break
                    self.assertEqual(header[257:263], b"ustar\0")
                    information = tarfile.TarInfo.frombuf(
                        header,
                        "utf-8",
                        "strict",
                    )
                    member_types.append(information.type)
                    padded_size = (
                        (information.size + tarfile.BLOCKSIZE - 1)
                        // tarfile.BLOCKSIZE
                    ) * tarfile.BLOCKSIZE
                    archive.seek(padded_size, io.SEEK_CUR)
            self.assertEqual(
                member_types,
                [tarfile.REGTYPE, tarfile.REGTYPE],
            )

    def test_zip_writer_records_explicit_unix_regular_file_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-zip-metadata-test-") as temporary:
            root = Path(temporary)
            package = root / "source-package"
            (package / "bin").mkdir(parents=True)
            (package / "README.md").write_bytes(b"documentation\n")
            (package / "bin" / "app-icon-toolkit-mcp").write_bytes(b"binary")
            archive_path = root / "candidate.zip"

            prepare_release_package.write_zip(
                package,
                archive_path,
                1_700_000_000,
            )

            with zipfile.ZipFile(archive_path, mode="r") as archive:
                members = {member.filename: member for member in archive.infolist()}
            self.assertEqual(
                set(members),
                {
                    "app-icon-toolkit/README.md",
                    "app-icon-toolkit/bin/app-icon-toolkit-mcp",
                },
            )
            for name, expected_mode in (
                ("app-icon-toolkit/README.md", 0o644),
                ("app-icon-toolkit/bin/app-icon-toolkit-mcp", 0o755),
            ):
                member = members[name]
                unix_mode = member.external_attr >> 16
                self.assertEqual(member.create_system, 3)
                self.assertEqual(stat.S_IFMT(unix_mode), stat.S_IFREG)
                self.assertEqual(stat.S_IMODE(unix_mode), expected_mode)

    def test_archive_writers_preflight_bounds_and_zip_streams_members(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-writer-bounds-") as temporary:
            root = Path(temporary)
            package = root / "source-package"
            package.mkdir()
            source = package / "README.md"
            source.write_bytes(b"1234")

            streamed_zip = root / "streamed.zip"
            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("ZIP writer must stream members"),
            ):
                prepare_release_package.write_zip(
                    package,
                    streamed_zip,
                    1_700_000_000,
                )
            with zipfile.ZipFile(streamed_zip, mode="r") as archive:
                self.assertEqual(
                    archive.read("app-icon-toolkit/README.md"),
                    b"1234",
                )

            for archive_format, writer in (
                ("zip", prepare_release_package.write_zip),
                ("tar.gz", prepare_release_package.write_tar_gz),
            ):
                with self.subTest(archive_format=archive_format):
                    output = root / f"oversized.{archive_format}"
                    with mock.patch.object(
                        prepare_release_package,
                        "MAX_MEMBER_BYTES",
                        3,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "member exceeds"):
                            writer(package, output, 1_700_000_000)
                    self.assertFalse(output.exists())

                    exact = root / f"exact.{archive_format}"
                    with mock.patch.object(
                        prepare_release_package,
                        "MAX_MEMBER_BYTES",
                        4,
                    ), mock.patch.object(
                        prepare_release_package,
                        "MAX_TOTAL_EXTRACTED_BYTES",
                        4,
                    ):
                        writer(package, exact, 1_700_000_000)
                    self.assertGreater(exact.stat().st_size, 0)

            bounded_output = root / "bounded.zip"
            with mock.patch.object(
                prepare_release_package,
                "MAX_ARCHIVE_BYTES",
                32,
            ):
                with self.assertRaisesRegex(RuntimeError, "output limit"):
                    prepare_release_package.write_zip(
                        package,
                        bounded_output,
                        1_700_000_000,
                    )
            self.assertFalse(bounded_output.exists())

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS ditto")
    def test_macos_ditto_preserves_zip_executable_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-ditto-test-") as temporary:
            root = Path(temporary)
            package = root / "source-package"
            (package / "bin").mkdir(parents=True)
            (package / "README.md").write_bytes(b"documentation\n")
            (package / "bin" / "app-icon-toolkit-mcp").write_bytes(b"binary")
            archive_path = root / "candidate.zip"
            extraction = root / "extracted"
            prepare_release_package.write_zip(
                package,
                archive_path,
                1_700_000_000,
            )

            completed = subprocess.run(
                ("/usr/bin/ditto", "-x", "-k", str(archive_path), str(extraction)),
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=30,
            )
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr, "")

            extracted = extraction / "app-icon-toolkit"
            self.assertEqual(
                stat.S_IMODE((extracted / "README.md").stat().st_mode),
                0o644,
            )
            self.assertEqual(
                stat.S_IMODE(
                    (extracted / "bin" / "app-icon-toolkit-mcp").stat().st_mode
                ),
                0o755,
            )

    def test_extracted_package_rejects_extra_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-layout-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            extracted = root / "extracted" / "app-icon-toolkit"
            source.mkdir()
            extracted.mkdir(parents=True)
            (source / "README.md").write_text("source", encoding="utf-8")
            (extracted / "README.md").write_text("source", encoding="utf-8")
            (extracted / "unexpected").write_text("extra", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "extra=.*unexpected"):
                prepare_release_package.validate_extracted_package(
                    root / "extracted", source
                )

    def test_tar_rejects_traversal_links_duplicates_and_oversize_before_writes(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="unsafe-tar-test-") as temporary:
            root = Path(temporary)
            cases = (
                ("traversal", [(expected[0], b"safe", tarfile.REGTYPE, ""), ("../escape", b"bad", tarfile.REGTYPE, "")]),
                ("symlink", [(expected[0], b"", tarfile.SYMTYPE, "../../escape")]),
                ("hardlink", [(expected[0], b"", tarfile.LNKTYPE, "../../escape")]),
                ("duplicate", [(expected[0], b"one", tarfile.REGTYPE, ""), (expected[0], b"two", tarfile.REGTYPE, "")]),
            )
            for label, members in cases:
                with self.subTest(label=label):
                    archive_path = root / f"{label}.tar.gz"
                    with tarfile.open(archive_path, mode="w:gz") as archive:
                        for name, data, kind, linkname in members:
                            self.write_tar_member(archive, name, data, kind, linkname)
                    extraction = root / f"extract-{label}"
                    extraction.mkdir()
                    with self.assertRaises(RuntimeError):
                        release_package.safe_extract_archive(
                            archive_path, "tar.gz", extraction, expected
                        )
                    self.assertEqual(list(extraction.iterdir()), [])
                    self.assertFalse((root / "escape").exists())

            oversized = root / "oversized.tar.gz"
            with tarfile.open(oversized, mode="w:gz") as archive:
                self.write_tar_member(archive, expected[0], b"two")
            oversized_output = root / "extract-oversized"
            oversized_output.mkdir()
            with mock.patch.object(release_package, "MAX_MEMBER_BYTES", 1):
                with self.assertRaisesRegex(RuntimeError, "invalid size"):
                    release_package.safe_extract_archive(
                        oversized, "tar.gz", oversized_output, expected
                    )
            self.assertEqual(list(oversized_output.iterdir()), [])

    def test_tar_rejects_oversize_before_standard_parser(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="tar-bomb-bound-test-") as temporary:
            root = Path(temporary)
            archive_path = root / "candidate.tar.gz"
            information = tarfile.TarInfo(expected[0])
            information.mode = 0o644
            information.size = release_package.MAX_MEMBER_BYTES + 1
            with gzip.GzipFile(
                filename=str(archive_path),
                mode="wb",
                mtime=0,
            ) as archive:
                archive.write(information.tobuf(format=tarfile.USTAR_FORMAT))
            extraction = root / "extracted"
            extraction.mkdir()

            with mock.patch.object(
                release_package.tarfile,
                "open",
            ) as standard_parser:
                with self.assertRaisesRegex(RuntimeError, "invalid size"):
                    release_package.safe_extract_archive(
                        archive_path, "tar.gz", extraction, expected
                    )
            standard_parser.assert_not_called()
            self.assertEqual(list(extraction.iterdir()), [])

    def test_tar_rejects_extended_metadata_before_reading_its_payload(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="tar-metadata-bound-test-") as temporary:
            root = Path(temporary)
            cases = (
                ("pax", tarfile.PAX_FORMAT, expected[0], True),
                (
                    "gnu-longname",
                    tarfile.GNU_FORMAT,
                    f"app-icon-toolkit/{'long-name-' * 20}",
                    False,
                ),
            )
            for label, archive_format, member_name, add_pax_metadata in cases:
                with self.subTest(label=label):
                    archive_path = root / f"{label}.tar.gz"
                    with tarfile.open(
                        archive_path,
                        mode="w:gz",
                        format=archive_format,
                    ) as archive:
                        information = tarfile.TarInfo(member_name)
                        information.mode = 0o644
                        information.size = 4
                        if add_pax_metadata:
                            information.pax_headers = {
                                "comment": "x" * (4 * 1024 * 1024)
                            }
                        archive.addfile(information, io.BytesIO(b"data"))

                    extraction = root / f"extract-{label}"
                    extraction.mkdir()
                    read_requests: list[int] = []
                    original_read = release_package.gzip.GzipFile.read

                    def observed_read(stream, size=-1):
                        read_requests.append(size)
                        return original_read(stream, size)

                    with mock.patch.object(
                        release_package.tarfile,
                        "open",
                    ) as standard_parser, mock.patch.object(
                        release_package.gzip.GzipFile,
                        "read",
                        new=observed_read,
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "extended metadata",
                        ):
                            release_package.safe_extract_archive(
                                archive_path,
                                "tar.gz",
                                extraction,
                                expected,
                            )
                    standard_parser.assert_not_called()
                    self.assertEqual(read_requests, [tarfile.BLOCKSIZE])
                    self.assertEqual(list(extraction.iterdir()), [])

    def test_archives_reject_special_permission_bits_before_standard_parser(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="archive-mode-test-") as temporary:
            root = Path(temporary)
            tar_path = root / "special-mode.tar.gz"
            with tarfile.open(
                tar_path,
                mode="w:gz",
                format=tarfile.USTAR_FORMAT,
            ) as archive:
                information = tarfile.TarInfo(expected[0])
                information.mode = stat.S_ISUID | 0o644
                information.size = 4
                archive.addfile(information, io.BytesIO(b"data"))
            tar_output = root / "tar-output"
            tar_output.mkdir()
            with mock.patch.object(
                release_package.tarfile,
                "open",
            ) as standard_tar_parser:
                with self.assertRaisesRegex(RuntimeError, "mode changed"):
                    release_package.safe_extract_archive(
                        tar_path,
                        "tar.gz",
                        tar_output,
                        expected,
                    )
            standard_tar_parser.assert_not_called()
            self.assertEqual(list(tar_output.iterdir()), [])

            zip_path = root / "special-mode.zip"
            with zipfile.ZipFile(zip_path, mode="w") as archive:
                archive.writestr(
                    self.zip_member(
                        expected[0],
                        stat.S_IFREG | stat.S_ISUID | 0o644,
                    ),
                    b"data",
                )
            zip_output = root / "zip-output"
            zip_output.mkdir()
            with mock.patch.object(
                release_package.zipfile,
                "ZipFile",
            ) as standard_zip_parser:
                with self.assertRaisesRegex(RuntimeError, "mode changed"):
                    release_package.safe_extract_archive(
                        zip_path,
                        "zip",
                        zip_output,
                        expected,
                    )
            standard_zip_parser.assert_not_called()
            self.assertEqual(list(zip_output.iterdir()), [])

    def test_archives_reject_setgid_and_sticky_permission_bits(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="archive-extra-mode-test-") as temporary:
            root = Path(temporary)
            for label, special_mode in (
                ("setgid", stat.S_ISGID),
                ("sticky", stat.S_ISVTX),
            ):
                for archive_format in ("tar.gz", "zip"):
                    with self.subTest(label=label, archive_format=archive_format):
                        archive_path = root / f"{label}.{archive_format}"
                        if archive_format == "tar.gz":
                            with tarfile.open(
                                archive_path,
                                mode="w:gz",
                                format=tarfile.USTAR_FORMAT,
                            ) as archive:
                                information = tarfile.TarInfo(expected[0])
                                information.mode = special_mode | 0o644
                                information.size = 4
                                archive.addfile(information, io.BytesIO(b"data"))
                        else:
                            with zipfile.ZipFile(archive_path, mode="w") as archive:
                                archive.writestr(
                                    self.zip_member(
                                        expected[0],
                                        stat.S_IFREG | special_mode | 0o644,
                                    ),
                                    b"data",
                                )
                        extraction = root / f"{label}-{archive_format}-output"
                        extraction.mkdir()
                        with self.assertRaisesRegex(RuntimeError, "mode changed"):
                            release_package.safe_extract_archive(
                                archive_path,
                                archive_format,
                                extraction,
                                expected,
                            )
                        self.assertEqual(list(extraction.iterdir()), [])

    def test_zip_crc_failure_rolls_back_allowlisted_outputs_and_is_retriable(
        self,
    ) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="archive-crc-rollback-") as temporary:
            root = Path(temporary)
            archive_path = root / "corrupt.zip"
            payload = b"release archive payload" * (128 * 1024)
            with zipfile.ZipFile(
                archive_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr(
                    self.zip_member(
                        expected[0],
                        stat.S_IFREG | 0o644,
                    ),
                    payload,
                )
            corrupted = bytearray(archive_path.read_bytes())
            local_header = corrupted.index(b"PK\x03\x04")
            central_header = corrupted.index(b"PK\x01\x02")
            corrupted[local_header + 14] ^= 0x01
            corrupted[central_header + 16] ^= 0x01
            archive_path.write_bytes(corrupted)
            extraction = root / "extracted"
            extraction.mkdir()

            for _attempt in range(2):
                with self.assertRaises(
                    release_package.ReleasePackageError
                ) as raised:
                    release_package.safe_extract_archive(
                        archive_path,
                        "zip",
                        extraction,
                        expected,
                    )
                self.assertIsInstance(raised.exception.__cause__, zipfile.BadZipFile)
                self.assertEqual(list(extraction.iterdir()), [])

    def test_incomplete_extraction_cleanup_rejects_unknown_or_linked_entries(
        self,
    ) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="archive-cleanup-boundary-") as temporary:
            root = Path(temporary)
            for label in ("unknown", "hardlink", "symlink"):
                with self.subTest(label=label):
                    extraction = root / label
                    package = extraction / "app-icon-toolkit"
                    package.mkdir(parents=True)
                    expected_file = package / "README.md"
                    expected_file.write_bytes(b"partial")
                    if label == "unknown":
                        unsafe = package / "unexpected"
                        unsafe.write_bytes(b"preserve")
                    elif label == "hardlink":
                        unsafe = root / f"{label}-alias"
                        os.link(expected_file, unsafe)
                    else:
                        unsafe = package / "linked"
                        create_symlink_or_skip(self, unsafe, expected_file)

                    with self.assertRaises(release_package.ReleasePackageError):
                        release_package.recover_incomplete_extraction(
                            extraction,
                            expected,
                        )
                    self.assertTrue(expected_file.exists())
                    self.assertTrue(unsafe.exists() or unsafe.is_symlink())

    @unittest.skipUnless(
        release_package._DIRECTORY_FD_SUPPORTED,
        "requires directory-relative filesystem operations",
    )
    def test_extraction_root_swap_never_redirects_member_writes(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="archive-root-swap-") as temporary:
            root = Path(temporary)
            archive_path = root / "candidate.zip"
            with zipfile.ZipFile(archive_path, mode="w") as archive:
                archive.writestr(
                    self.zip_member(expected[0], stat.S_IFREG | 0o644),
                    b"archive payload",
                )
            extraction = root / "extracted"
            parked = root / "parked"
            replacement = root / "replacement"
            extraction.mkdir()
            replacement.mkdir()
            (replacement / "marker").write_bytes(b"preserve")
            original_copy = release_package._copy_member
            swapped = False

            def swap_root_before_copy(*args, **kwargs):
                nonlocal swapped
                if not swapped:
                    extraction.rename(parked)
                    replacement.rename(extraction)
                    swapped = True
                return original_copy(*args, **kwargs)

            with mock.patch.object(
                release_package,
                "_copy_member",
                side_effect=swap_root_before_copy,
            ):
                with self.assertRaises(
                    release_package.ReleasePackageCleanupError
                ):
                    release_package.safe_extract_archive(
                        archive_path,
                        "zip",
                        extraction,
                        expected,
                    )

            self.assertTrue(swapped)
            self.assertEqual((extraction / "marker").read_bytes(), b"preserve")
            self.assertFalse((extraction / expected[0]).exists())
            self.assertEqual(list(parked.iterdir()), [])

    @unittest.skipUnless(
        release_package._DIRECTORY_FD_SUPPORTED,
        "requires directory-relative filesystem operations",
    )
    def test_cleanup_root_swap_never_unlinks_replacement_entry(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="archive-cleanup-swap-") as temporary:
            root = Path(temporary)
            extraction = root / "extracted"
            original_file = extraction / expected[0]
            original_file.parent.mkdir(parents=True)
            original_file.write_bytes(b"original partial")
            parked = root / "parked"
            replacement = root / "replacement"
            replacement_file = replacement / expected[0]
            replacement_file.parent.mkdir(parents=True)
            replacement_file.write_bytes(b"replacement must survive")
            real_unlink = release_package.os.unlink
            swapped = False

            def swap_root_before_unlink(path, *args, **kwargs):
                nonlocal swapped
                if not swapped and kwargs.get("dir_fd") is not None:
                    extraction.rename(parked)
                    replacement.rename(extraction)
                    swapped = True
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                release_package.os,
                "unlink",
                side_effect=swap_root_before_unlink,
            ):
                with self.assertRaisesRegex(
                    release_package.ReleasePackageError,
                    "root path changed",
                ):
                    release_package.recover_incomplete_extraction(
                        extraction,
                        expected,
                    )

            self.assertTrue(swapped)
            self.assertEqual(
                (extraction / expected[0]).read_bytes(),
                b"replacement must survive",
            )
            self.assertEqual(list(parked.iterdir()), [])

    def test_extraction_member_replacement_fails_without_deleting_replacement(
        self,
    ) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="archive-member-swap-") as temporary:
            root = Path(temporary)
            archive_path = root / "candidate.zip"
            with zipfile.ZipFile(archive_path, mode="w") as archive:
                archive.writestr(
                    self.zip_member(expected[0], stat.S_IFREG | 0o644),
                    b"archive payload",
                )
            extraction = root / "extracted"
            extraction.mkdir()
            member = extraction / expected[0]
            parked = root / "parked-member"
            real_fsync = release_package.os.fsync
            swapped = False

            def swap_member_before_fsync(descriptor):
                nonlocal swapped
                if not swapped:
                    member.rename(parked)
                    member.write_bytes(b"replacement must survive")
                    swapped = True
                return real_fsync(descriptor)

            with mock.patch.object(
                release_package.os,
                "fsync",
                side_effect=swap_member_before_fsync,
            ):
                with self.assertRaises(
                    release_package.ReleasePackageCleanupError
                ) as raised:
                    release_package.safe_extract_archive(
                        archive_path,
                        "zip",
                        extraction,
                        expected,
                    )

            self.assertTrue(swapped)
            self.assertIn("path changed", str(raised.exception.primary))
            self.assertIn("not created by this operation", str(raised.exception.cleanup))
            self.assertEqual(parked.read_bytes(), b"archive payload")
            self.assertEqual(member.read_bytes(), b"replacement must survive")

    def test_cleanup_error_retains_primary_and_cleanup_failures(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="archive-cleanup-error-") as temporary:
            root = Path(temporary)
            archive_path = root / "archive.bin"
            archive_path.write_bytes(b"not an archive")
            extraction = root / "extracted"
            extraction.mkdir()
            cleanup_error = release_package.ReleasePackageError(
                "injected cleanup failure"
            )

            with mock.patch.object(
                release_package,
                "_recover_incomplete_extraction",
                side_effect=cleanup_error,
            ):
                with self.assertRaises(
                    release_package.ReleasePackageCleanupError
                ) as raised:
                    release_package.safe_extract_archive(
                        archive_path,
                        "unsupported",
                        extraction,
                        expected,
                    )

            self.assertIsInstance(
                raised.exception.primary,
                release_package.ReleasePackageError,
            )
            self.assertIs(raised.exception.cleanup, cleanup_error)
            self.assertIs(raised.exception.__cause__, raised.exception.primary)

    def test_member_count_name_and_total_size_bounds_have_both_edges(self) -> None:
        maximum_members = tuple(
            f"app-icon-toolkit/member-{index}"
            for index in range(release_package.MAX_MEMBERS)
        )
        release_package._validate_expected_members(maximum_members)
        with self.assertRaisesRegex(RuntimeError, "invalid size"):
            release_package._validate_expected_members(
                (*maximum_members, "app-icon-toolkit/one-too-many")
            )

        prefix = "app-icon-toolkit/"
        exact_name = prefix + "x" * (
            release_package.MAX_MEMBER_NAME_BYTES - len(prefix.encode("utf-8"))
        )
        release_package._validate_expected_members((exact_name,))
        with self.assertRaisesRegex(RuntimeError, "unsafe expected"):
            release_package._validate_expected_members((exact_name + "x",))

        expected = (
            "app-icon-toolkit/README.md",
            "app-icon-toolkit/LICENSE",
        )
        for archive_format in ("tar.gz", "zip"):
            with self.subTest(archive_format=archive_format):
                with tempfile.TemporaryDirectory(
                    prefix="archive-total-bound-"
                ) as temporary:
                    root = Path(temporary)
                    archive_path = root / f"candidate.{archive_format}"
                    if archive_format == "tar.gz":
                        with tarfile.open(
                            archive_path,
                            mode="w:gz",
                            format=tarfile.USTAR_FORMAT,
                        ) as archive:
                            for name in expected:
                                self.write_tar_member(archive, name, b"1234")
                    else:
                        with zipfile.ZipFile(archive_path, mode="w") as archive:
                            for name in expected:
                                archive.writestr(
                                    self.zip_member(
                                        name,
                                        stat.S_IFREG | 0o644,
                                    ),
                                    b"1234",
                                )

                    rejected = root / "rejected"
                    rejected.mkdir()
                    with mock.patch.object(
                        release_package,
                        "MAX_TOTAL_EXTRACTED_BYTES",
                        7,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "expands beyond"):
                            release_package.safe_extract_archive(
                                archive_path,
                                archive_format,
                                rejected,
                                expected,
                            )
                    self.assertEqual(list(rejected.iterdir()), [])

                    accepted = root / "accepted"
                    accepted.mkdir()
                    with mock.patch.object(
                        release_package,
                        "MAX_TOTAL_EXTRACTED_BYTES",
                        8,
                    ):
                        release_package.safe_extract_archive(
                            archive_path,
                            archive_format,
                            accepted,
                            expected,
                        )
                    self.assertEqual(
                        sum(
                            path.stat().st_size
                            for path in accepted.rglob("*")
                            if path.is_file()
                        ),
                        8,
                    )

    def test_tar_extracts_validated_members_in_archive_order(self) -> None:
        expected = tuple(
            f"app-icon-toolkit/member-{index}.txt" for index in range(8)
        )
        archive_order = tuple(reversed(expected))
        with tempfile.TemporaryDirectory(prefix="tar-order-test-") as temporary:
            root = Path(temporary)
            archive_path = root / "reverse-order.tar.gz"
            with tarfile.open(
                archive_path,
                mode="w:gz",
                format=tarfile.USTAR_FORMAT,
            ) as archive:
                for index, name in enumerate(archive_order):
                    self.write_tar_member(
                        archive,
                        name,
                        bytes([index]) * (64 * 1024),
                    )
            extraction = root / "extracted"
            extraction.mkdir()
            extracted_order: list[str] = []
            original_extractfile = release_package.tarfile.TarFile.extractfile

            def observed_extractfile(archive, member):
                extracted_order.append(member.name)
                return original_extractfile(archive, member)

            with mock.patch.object(
                release_package.tarfile.TarFile,
                "extractfile",
                new=observed_extractfile,
            ):
                release_package.safe_extract_archive(
                    archive_path,
                    "tar.gz",
                    extraction,
                    expected,
                )

            self.assertEqual(tuple(extracted_order), archive_order)
            self.assertEqual(
                {
                    path.relative_to(extraction).as_posix()
                    for path in extraction.rglob("*")
                    if path.is_file()
                },
                set(expected),
            )

    def test_zip_rejects_traversal_and_symlink_before_writes(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="unsafe-zip-test-") as temporary:
            root = Path(temporary)

            traversal = root / "traversal.zip"
            with zipfile.ZipFile(traversal, mode="w") as archive:
                archive.writestr(self.zip_member(expected[0]), b"safe")
                archive.writestr(self.zip_member("../escape"), b"bad")
            traversal_output = root / "extract-traversal"
            traversal_output.mkdir()
            with self.assertRaisesRegex(RuntimeError, "expected member count"):
                release_package.safe_extract_archive(
                    traversal, "zip", traversal_output, expected
                )
            self.assertEqual(list(traversal_output.iterdir()), [])
            self.assertFalse((root / "escape").exists())

            symlink = root / "symlink.zip"
            with zipfile.ZipFile(symlink, mode="w") as archive:
                archive.writestr(
                    self.zip_member(expected[0], stat.S_IFLNK | 0o777),
                    b"../../escape",
                )
            symlink_output = root / "extract-symlink"
            symlink_output.mkdir()
            with self.assertRaisesRegex(RuntimeError, "not an ordinary file"):
                release_package.safe_extract_archive(
                    symlink, "zip", symlink_output, expected
                )
            self.assertEqual(list(symlink_output.iterdir()), [])

            missing_type = root / "missing-type.zip"
            with zipfile.ZipFile(missing_type, mode="w") as archive:
                archive.writestr(self.zip_member(expected[0], 0o644), b"data")
            missing_type_output = root / "extract-missing-type"
            missing_type_output.mkdir()
            with self.assertRaisesRegex(RuntimeError, "not an ordinary file"):
                release_package.safe_extract_archive(
                    missing_type,
                    "zip",
                    missing_type_output,
                    expected,
                )
            self.assertEqual(list(missing_type_output.iterdir()), [])

    def test_zip_rejects_declared_member_count_before_standard_parser(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="zip-member-bound-test-") as temporary:
            root = Path(temporary)
            archive_path = root / "many-members.zip"
            with zipfile.ZipFile(archive_path, mode="w") as archive:
                for index in range(release_package.MAX_MEMBERS + 1):
                    archive.writestr(
                        self.zip_member(
                            f"app-icon-toolkit/member-{index}",
                            stat.S_IFREG | 0o644,
                        ),
                        b"data",
                    )
            extraction = root / "extracted"
            extraction.mkdir()

            with mock.patch.object(
                release_package.zipfile,
                "ZipFile",
            ) as standard_parser:
                with self.assertRaisesRegex(RuntimeError, "member count"):
                    release_package.safe_extract_archive(
                        archive_path,
                        "zip",
                        extraction,
                        expected,
                    )
            standard_parser.assert_not_called()
            self.assertEqual(list(extraction.iterdir()), [])

    def test_zip_scans_actual_central_directory_before_standard_parser(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="zip-count-mismatch-test-") as temporary:
            root = Path(temporary)
            archive_path = root / "forged-count.zip"
            with zipfile.ZipFile(archive_path, mode="w") as archive:
                archive.writestr(
                    self.zip_member(expected[0], stat.S_IFREG | 0o644),
                    b"first",
                )
                archive.writestr(
                    self.zip_member(
                        "app-icon-toolkit/unexpected",
                        stat.S_IFREG | 0o644,
                    ),
                    b"second",
                )
            forged = bytearray(archive_path.read_bytes())
            eocd_offset = len(forged) - release_zip_preflight.ZIP_EOCD.size
            struct.pack_into("<H", forged, eocd_offset + 8, 1)
            struct.pack_into("<H", forged, eocd_offset + 10, 1)
            archive_path.write_bytes(forged)
            extraction = root / "extracted"
            extraction.mkdir()

            with mock.patch.object(
                release_package.zipfile,
                "ZipFile",
            ) as standard_parser:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "unexpected number of records",
                ):
                    release_package.safe_extract_archive(
                        archive_path,
                        "zip",
                        extraction,
                        expected,
                    )
            standard_parser.assert_not_called()
            self.assertEqual(list(extraction.iterdir()), [])

    def test_zip_rejects_central_metadata_limit_before_standard_parser(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="zip-metadata-bound-test-") as temporary:
            root = Path(temporary)
            archive_path = root / "metadata.zip"
            with zipfile.ZipFile(archive_path, mode="w") as archive:
                archive.writestr(
                    self.zip_member(expected[0], stat.S_IFREG | 0o644),
                    b"data",
                )
            extraction = root / "extracted"
            extraction.mkdir()

            with mock.patch.object(
                release_package,
                "MAX_ZIP_CENTRAL_DIRECTORY_BYTES",
                1,
            ), mock.patch.object(
                release_package.zipfile,
                "ZipFile",
            ) as standard_parser:
                with self.assertRaisesRegex(RuntimeError, "metadata size limit"):
                    release_package.safe_extract_archive(
                        archive_path,
                        "zip",
                        extraction,
                        expected,
                    )
            standard_parser.assert_not_called()
            self.assertEqual(list(extraction.iterdir()), [])

    def test_zip_rejects_lzma_amplification_before_standard_parser(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="zip-method-bound-test-") as temporary:
            root = Path(temporary)
            archive_path = root / "lzma.zip"
            information = self.zip_member(
                expected[0],
                stat.S_IFREG | 0o644,
            )
            information.compress_type = zipfile.ZIP_LZMA
            expanded = b"\0" * (2 * 1024 * 1024)
            with zipfile.ZipFile(archive_path, mode="w") as archive:
                archive.writestr(information, expanded)

            forged = bytearray(archive_path.read_bytes())
            local_header = forged.index(b"PK\x03\x04")
            central_header = forged.index(b"PK\x01\x02")
            short_payload = b"\0" * 4
            short_crc = zlib.crc32(short_payload)
            forged[local_header + 14 : local_header + 18] = short_crc.to_bytes(
                4,
                "little",
            )
            forged[local_header + 22 : local_header + 26] = len(
                short_payload
            ).to_bytes(4, "little")
            forged[central_header + 16 : central_header + 20] = short_crc.to_bytes(
                4,
                "little",
            )
            forged[central_header + 24 : central_header + 28] = len(
                short_payload
            ).to_bytes(4, "little")
            archive_path.write_bytes(forged)
            extraction = root / "extracted"
            extraction.mkdir()

            with mock.patch.object(
                release_package.zipfile,
                "ZipFile",
            ) as standard_parser:
                with self.assertRaisesRegex(RuntimeError, "required DEFLATE"):
                    release_package.safe_extract_archive(
                        archive_path,
                        "zip",
                        extraction,
                        expected,
                    )
            standard_parser.assert_not_called()
            self.assertEqual(list(extraction.iterdir()), [])

    def test_zip_rejects_bzip2_before_standard_parser(self) -> None:
        expected = ("app-icon-toolkit/README.md",)
        with tempfile.TemporaryDirectory(prefix="zip-bzip2-bound-test-") as temporary:
            root = Path(temporary)
            archive_path = root / "bzip2.zip"
            information = self.zip_member(
                expected[0],
                stat.S_IFREG | 0o644,
            )
            information.compress_type = zipfile.ZIP_BZIP2
            with zipfile.ZipFile(archive_path, mode="w") as archive:
                archive.writestr(information, b"data")
            extraction = root / "extracted"
            extraction.mkdir()

            with mock.patch.object(
                release_package.zipfile,
                "ZipFile",
            ) as standard_parser:
                with self.assertRaisesRegex(RuntimeError, "required DEFLATE"):
                    release_package.safe_extract_archive(
                        archive_path,
                        "zip",
                        extraction,
                        expected,
                    )
            standard_parser.assert_not_called()
            self.assertEqual(list(extraction.iterdir()), [])

    def test_round_trip_detects_same_size_byte_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="archive-byte-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "README.md").write_bytes(b"expected")
            archive_path = root / "mutated.tar.gz"
            staged = root / "staged"
            staged.mkdir()
            (staged / "README.md").write_bytes(b"mutated!")
            prepare_release_package.write_tar_gz(staged, archive_path, 1_700_000_000)
            extraction = root / "extracted"
            extraction.mkdir()
            expected_name = "app-icon-toolkit/README.md"
            release_package.safe_extract_archive(
                archive_path,
                "tar.gz",
                extraction,
                (expected_name,),
                {expected_name: len(b"expected")},
            )
            with self.assertRaisesRegex(RuntimeError, "changed packaged file bytes"):
                prepare_release_package.validate_extracted_package(extraction, source)



if __name__ == "__main__":
    unittest.main()
