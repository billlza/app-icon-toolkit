from __future__ import annotations

import io
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

from release_test_support import prepare_release_package, release_package


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
        information.external_attr = mode << 16
        return information

    def test_static_release_inputs_reject_symlinks_and_empty_files(self) -> None:
        symlink = mock.Mock(spec=Path)
        symlink.is_symlink.return_value = True
        with self.assertRaisesRegex(RuntimeError, "non-empty regular non-symlink"):
            prepare_release_package.validate_static_input(symlink)

        with tempfile.TemporaryDirectory(prefix="release-input-test-") as temporary:
            empty = Path(temporary) / "empty"
            empty.write_bytes(b"")
            with self.assertRaisesRegex(RuntimeError, "non-empty regular non-symlink"):
                prepare_release_package.validate_static_input(empty)

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
                        prepare_release_package.smoke_test_archive(
                            Path("/plugin"), package, archive, archive_format
                        )
                    run.assert_called_once()

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

    def test_tar_rejects_oversize_before_requesting_the_next_header(self) -> None:
        expected = ("app-icon-toolkit/README.md",)

        class OversizeArchive:
            def __init__(self) -> None:
                self.calls = 0

            def __enter__(self):
                return self

            def __exit__(self, _kind, _value, _traceback) -> None:
                return None

            def next(self):
                self.calls += 1
                if self.calls != 1:
                    raise AssertionError("extractor requested another header after oversize")
                member = tarfile.TarInfo(expected[0])
                member.mode = 0o644
                member.size = release_package.MAX_MEMBER_BYTES + 1
                return member

        with tempfile.TemporaryDirectory(prefix="tar-bomb-bound-test-") as temporary:
            root = Path(temporary)
            archive_path = root / "candidate.tar.gz"
            archive_path.write_bytes(b"placeholder")
            extraction = root / "extracted"
            extraction.mkdir()
            fake_archive = OversizeArchive()

            with mock.patch.object(
                release_package.tarfile, "open", return_value=fake_archive
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid size"):
                    release_package.safe_extract_archive(
                        archive_path, "tar.gz", extraction, expected
                    )
            self.assertEqual(fake_archive.calls, 1)
            self.assertEqual(list(extraction.iterdir()), [])

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
            with self.assertRaisesRegex(RuntimeError, "member mismatch"):
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
