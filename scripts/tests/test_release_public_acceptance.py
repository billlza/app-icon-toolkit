from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote
import zipfile
from typing import Callable


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import macos_signing
import release_draft
from release_package import PACKAGE_ROOT_NAME, STATIC_PATHS
import release_public_acceptance
import release_targets


REPOSITORY = "example/app-icon-toolkit"
RELEASE_ID = 456
TAG = "v1.2.3"
NAME = f"App Icon Toolkit {TAG}"
BODY = "# Exact release notes\n"
IDENTITY_SHA1 = "A" * 40
HEAD_SHA = "1" * 40
TAG_OBJECT_SHA = "2" * 40


class FakePublicDownloader:
    def __init__(
        self,
        metadata_payloads: list[bytes],
        asset_payloads: dict[int, bytes],
        tag_ref_payloads: list[bytes] | None = None,
        tag_object_payloads: list[bytes] | None = None,
    ) -> None:
        self.metadata_payloads = metadata_payloads
        self.asset_payloads = asset_payloads
        self.tag_ref_payloads = (
            [_tag_ref_json(), _tag_ref_json()]
            if tag_ref_payloads is None
            else tag_ref_payloads
        )
        self.tag_object_payloads = (
            [_tag_object_json(), _tag_object_json()]
            if tag_object_payloads is None
            else tag_object_payloads
        )
        self.calls: list[tuple[str, int, str]] = []

    def download(
        self,
        url: str,
        destination: Path,
        maximum_bytes: int,
        media_type: str,
    ) -> None:
        self.calls.append((url, maximum_bytes, media_type))
        release_url = f"https://api.github.com/repos/{REPOSITORY}/releases/{RELEASE_ID}"
        if url == release_url:
            if not self.metadata_payloads:
                raise AssertionError("unexpected anonymous release metadata GET")
            destination.write_bytes(self.metadata_payloads.pop(0))
            return
        tag_ref_url = f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{TAG}"
        if url == tag_ref_url:
            if not self.tag_ref_payloads:
                raise AssertionError("unexpected anonymous tag reference GET")
            destination.write_bytes(self.tag_ref_payloads.pop(0))
            return
        tag_object_prefix = f"https://api.github.com/repos/{REPOSITORY}/git/tags/"
        if url.startswith(tag_object_prefix):
            if not self.tag_object_payloads:
                raise AssertionError("unexpected anonymous tag object GET")
            destination.write_bytes(self.tag_object_payloads.pop(0))
            return
        prefix = f"https://api.github.com/repos/{REPOSITORY}/releases/assets/"
        if not url.startswith(prefix):
            raise AssertionError(f"unexpected public GET URL: {url}")
        asset_id = int(url.removeprefix(prefix))
        destination.write_bytes(self.asset_payloads[asset_id])


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> macos_signing.CommandResult:
        self.commands.append(argv)
        return macos_signing.CommandResult(argv, 0, "", "")


class FailingPublicDownloader:
    def __init__(self) -> None:
        self.calls = 0

    def download(
        self,
        url: str,
        destination: Path,
        maximum_bytes: int,
        media_type: str,
    ) -> None:
        del url, destination, maximum_bytes, media_type
        self.calls += 1
        raise release_public_acceptance.release_public.PublicVerificationError(
            "injected anonymous GET failure"
        )


def _tag_ref_json(tag_object_sha: str = TAG_OBJECT_SHA) -> bytes:
    return json.dumps(
        {
            "ref": f"refs/tags/{TAG}",
            "object": {
                "sha": tag_object_sha,
                "type": "tag",
                "url": (
                    f"https://api.github.com/repos/{REPOSITORY}/git/tags/"
                    f"{tag_object_sha}"
                ),
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _tag_object_json(
    tag_object_sha: str = TAG_OBJECT_SHA,
    head_sha: str = HEAD_SHA,
) -> bytes:
    return json.dumps(
        {
            "sha": tag_object_sha,
            "tag": TAG,
            "object": {
                "sha": head_sha,
                "type": "commit",
                "url": (
                    f"https://api.github.com/repos/{REPOSITORY}/git/commits/"
                    f"{head_sha}"
                ),
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _write_archive(
    path: Path,
    archive_format: str,
    members: dict[str, tuple[bytes, int]],
) -> None:
    if archive_format == "zip":
        with zipfile.ZipFile(path, mode="x", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, (payload, mode) in sorted(members.items()):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, payload)
        return
    if archive_format == "tar.gz":
        with tarfile.open(path, mode="x:gz") as archive:
            for name, (payload, mode) in sorted(members.items()):
                info = tarfile.TarInfo(name)
                info.type = tarfile.REGTYPE
                info.mode = mode
                info.size = len(payload)
                info.mtime = 1
                archive.addfile(info, BytesIO(payload))
        return
    raise AssertionError(f"unsupported fixture format: {archive_format}")


def _public_json(
    assets: tuple[release_draft.LocalAsset, ...],
    ids: dict[str, int],
    *,
    immutable: bool = True,
    omitted_name: str | None = None,
) -> bytes:
    value = {
        "id": RELEASE_ID,
        "tag_name": TAG,
        "name": NAME,
        "body": BODY,
        "draft": False,
        "prerelease": False,
        "immutable": immutable,
        "download_count": 0,
        "assets": [
            {
                "id": ids[asset.name],
                "name": asset.name,
                "size": asset.size,
                "digest": f"sha256:{asset.sha256}",
                "state": "uploaded",
                "url": (
                    f"https://api.github.com/repos/{REPOSITORY}/releases/assets/"
                    f"{ids[asset.name]}"
                ),
                "browser_download_url": (
                    f"https://github.com/{REPOSITORY}/releases/download/"
                    f"{quote(TAG, safe='')}/{quote(asset.name, safe='')}"
                ),
                "download_count": 0,
            }
            for asset in assets
            if asset.name != omitted_name
        ],
    }
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class AcceptanceFixture:
    def __init__(self, root: Path) -> None:
        self.plugin_root = root / "plugin"
        self.assets_root = root / "assets"
        self.plugin_root.mkdir()
        self.assets_root.mkdir()
        self.contract = release_targets.load_contract()
        self.static_payloads: dict[str, bytes] = {}
        for relative in STATIC_PATHS:
            payload = f"fixture:{relative.as_posix()}\n".encode("utf-8")
            path = self.plugin_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(0o644)
            self.static_payloads[relative.as_posix()] = payload

        archive_paths: dict[str, Path] = {}
        for target in self.contract.targets:
            archive_name = target.release_filename(TAG)
            archive_path = self.assets_root / archive_name
            members = {
                (PurePosixPath(PACKAGE_ROOT_NAME) / relative).as_posix(): (
                    payload,
                    0o644,
                )
                for relative, payload in self.static_payloads.items()
            }
            members[
                (
                    PurePosixPath(PACKAGE_ROOT_NAME)
                    / "bin"
                    / target.binary_name
                ).as_posix()
            ] = (f"binary:{target.id}".encode("ascii"), 0o755)
            _write_archive(archive_path, target.archive_format, members)
            archive_paths[archive_name] = archive_path

        archives = release_draft.snapshot_local_assets(
            archive_paths,
            expected_names=tuple(sorted(archive_paths)),
        )
        checksum = self.assets_root / release_draft.CHECKSUM_ASSET_NAME
        checksum.write_bytes(release_draft.render_sha256sums(archives))
        checksum.chmod(0o644)
        all_paths = {**archive_paths, checksum.name: checksum}
        self.assets = release_draft.snapshot_local_assets(
            all_paths,
            expected_names=tuple(sorted(all_paths)),
        )
        self.ids = {
            asset.name: 1000 + index for index, asset in enumerate(self.assets)
        }
        self.asset_payloads = {
            self.ids[asset.name]: asset.path.read_bytes() for asset in self.assets
        }

    def request(self) -> release_public_acceptance.PublicAcceptanceRequest:
        return release_public_acceptance.PublicAcceptanceRequest(
            repository=REPOSITORY,
            release_id=RELEASE_ID,
            tag=TAG,
            expected_tag_object_sha=TAG_OBJECT_SHA,
            expected_head_sha=HEAD_SHA,
            name=NAME,
            body=BODY,
            local_assets=self.assets,
            contract=self.contract,
            plugin_root=self.plugin_root,
            identity_sha1=IDENTITY_SHA1,
        )


def _signature(binary: Path, **kwargs: object) -> macos_signing.SignatureVerificationReceipt:
    target_architectures = tuple(kwargs["expected_architectures"])
    digest = release_public_acceptance.release_files.sha256_file(
        binary, label="fixture signed binary"
    )
    return macos_signing.SignatureVerificationReceipt(
        signed_sha256=digest,
        identity_sha1=str(kwargs["identity_sha1"]),
        identifier=str(kwargs["identifier"]),
        team_id=str(kwargs["team_id"]),
        architectures=target_architectures,
        slices=tuple(
            macos_signing.SignedSlice(
                architecture=architecture,
                leaf_authority="Developer ID Application: Fixture (YKUPL7Z869)",
                cdhash=f"CDHASH-{architecture}",
                timestamp="Sep 4, 2026 at 12:00:00",
                designated_requirement=f"identifier fixture and arch {architecture}",
            )
            for architecture in target_architectures
        ),
    )


@unittest.skipUnless(os.name == "posix", "POSIX public acceptance boundary")
class PublicAcceptanceTests(unittest.TestCase):
    def _assert_public_failure(
        self, operation: Callable[[], object], pattern: str
    ) -> None:
        with self.assertRaisesRegex(
            release_public_acceptance.PublicButUnverifiedError,
            rf"^{release_public_acceptance.PUBLIC_BUT_UNVERIFIED}: .*{pattern}",
        ) as raised:
            operation()
        self.assertFalse(raised.exception.github_mutation_performed)

    def test_exact_public_release_is_serializable_and_never_executes_candidate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="public-acceptance-test-") as temporary:
            fixture = AcceptanceFixture(Path(temporary))
            first = _public_json(fixture.assets, fixture.ids)
            second_value = json.loads(first)
            second_value["download_count"] = len(fixture.assets)
            for asset in second_value["assets"]:
                asset["download_count"] = 1
            downloader = FakePublicDownloader(
                [first, json.dumps(second_value).encode("utf-8")],
                fixture.asset_payloads,
            )
            runner = RecordingRunner()

            with mock.patch.object(
                release_public_acceptance.macos_signing,
                "verify_signed",
                side_effect=_signature,
            ) as verify_signed, mock.patch.object(
                release_public_acceptance.macos_signing,
                "check_notarization_ticket",
            ) as check_ticket:
                receipt = release_public_acceptance.verify_public_release(
                    fixture.request(),
                    downloader=downloader,
                    runner=runner,
                    get_attempts=1,
                )

            self.assertEqual(receipt.status, release_public_acceptance.PUBLIC_VERIFIED)
            self.assertTrue(receipt.immutable)
            self.assertTrue(receipt.tag_binding_verified)
            self.assertEqual(receipt.tag_object_sha, TAG_OBJECT_SHA)
            self.assertEqual(receipt.head_sha, HEAD_SHA)
            self.assertEqual(receipt.identity_sha1, IDENTITY_SHA1)
            self.assertTrue(receipt.snapshots_match)
            self.assertFalse(receipt.github_mutation_performed)
            self.assertFalse(receipt.candidate_execution_performed)
            self.assertEqual(len(receipt.assets), len(fixture.assets))
            self.assertEqual(len(receipt.archives), len(fixture.contract.targets))
            self.assertEqual(
                len([archive for archive in receipt.archives if archive.macos]), 3
            )
            json.dumps(receipt.to_json_value(), sort_keys=True, allow_nan=False)

            self.assertEqual(verify_signed.call_count, 3)
            self.assertEqual(check_ticket.call_count, 3)
            self.assertEqual(len(runner.commands), 3)
            for command in runner.commands:
                self.assertEqual(command[0], sys.executable)
                self.assertTrue(command[1].endswith("scripts/check-release-binary.py"))
                self.assertNotIn("smoke-installed-plugin.py", " ".join(command))
                self.assertFalse(command[0].endswith("app-icon-toolkit-mcp"))

            self.assertEqual(
                [call[2] for call in downloader.calls].count(
                    "application/vnd.github+json"
                ),
                6,
            )
            self.assertTrue(
                all(call[0].startswith("https://api.github.com/repos/") for call in downloader.calls)
            )

    def test_asset_identity_replacement_between_snapshots_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="public-acceptance-test-") as temporary:
            fixture = AcceptanceFixture(Path(temporary))
            replaced_ids = dict(fixture.ids)
            replaced_ids[fixture.assets[0].name] += 50_000
            downloader = FakePublicDownloader(
                [
                    _public_json(fixture.assets, fixture.ids),
                    _public_json(fixture.assets, replaced_ids),
                ],
                fixture.asset_payloads,
            )
            with mock.patch.object(
                release_public_acceptance.macos_signing,
                "verify_signed",
                side_effect=_signature,
            ), mock.patch.object(
                release_public_acceptance.macos_signing,
                "check_notarization_ticket",
            ):
                self._assert_public_failure(
                    lambda: release_public_acceptance.verify_public_release(
                        fixture.request(),
                        downloader=downloader,
                        runner=RecordingRunner(),
                        get_attempts=1,
                    ),
                    "changed between snapshots",
                )

    def test_tag_binding_replacement_between_snapshots_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="public-acceptance-test-") as temporary:
            fixture = AcceptanceFixture(Path(temporary))
            metadata = _public_json(fixture.assets, fixture.ids)
            downloader = FakePublicDownloader(
                [metadata, metadata],
                fixture.asset_payloads,
                tag_ref_payloads=[
                    _tag_ref_json(),
                    _tag_ref_json("3" * 40),
                ],
            )
            with mock.patch.object(
                release_public_acceptance.macos_signing,
                "verify_signed",
                side_effect=_signature,
            ), mock.patch.object(
                release_public_acceptance.macos_signing,
                "check_notarization_ticket",
            ):
                self._assert_public_failure(
                    lambda: release_public_acceptance.verify_public_release(
                        fixture.request(),
                        downloader=downloader,
                        runner=RecordingRunner(),
                        get_attempts=1,
                    ),
                    "annotated-tag reference is invalid",
                )

    def test_mac_signature_or_online_ticket_failure_is_public_but_unverified(self) -> None:
        for failed_operation in ("signature", "ticket"):
            with self.subTest(failed_operation=failed_operation), tempfile.TemporaryDirectory(
                prefix="public-acceptance-test-"
            ) as temporary:
                fixture = AcceptanceFixture(Path(temporary))
                metadata = _public_json(fixture.assets, fixture.ids)
                downloader = FakePublicDownloader(
                    [metadata, metadata], fixture.asset_payloads
                )
                signature_effect: object = _signature
                ticket_effect: Exception | None = None
                expected = "signature rejected"
                if failed_operation == "signature":
                    signature_effect = macos_signing.SignatureValidationError(
                        expected
                    )
                else:
                    expected = "ticket rejected"
                    ticket_effect = macos_signing.SignatureValidationError(expected)
                with mock.patch.object(
                    release_public_acceptance.macos_signing,
                    "verify_signed",
                    side_effect=signature_effect,
                ), mock.patch.object(
                    release_public_acceptance.macos_signing,
                    "check_notarization_ticket",
                    side_effect=ticket_effect,
                ):
                    self._assert_public_failure(
                        lambda: release_public_acceptance.verify_public_release(
                            fixture.request(),
                            downloader=downloader,
                            runner=RecordingRunner(),
                            get_attempts=1,
                        ),
                        expected,
                    )

    def test_local_prepared_asset_change_during_acceptance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="public-acceptance-test-") as temporary:
            fixture = AcceptanceFixture(Path(temporary))
            metadata = _public_json(fixture.assets, fixture.ids)

            class MutatingDownloader(FakePublicDownloader):
                def __init__(self) -> None:
                    super().__init__([metadata, metadata], fixture.asset_payloads)
                    self.metadata_gets = 0

                def download(
                    self,
                    url: str,
                    destination: Path,
                    maximum_bytes: int,
                    media_type: str,
                ) -> None:
                    super().download(url, destination, maximum_bytes, media_type)
                    if media_type == "application/vnd.github+json":
                        self.metadata_gets += 1
                        if self.metadata_gets == 2:
                            fixture.assets[0].path.write_bytes(b"changed local asset")

            with mock.patch.object(
                release_public_acceptance.macos_signing,
                "verify_signed",
                side_effect=_signature,
            ), mock.patch.object(
                release_public_acceptance.macos_signing,
                "check_notarization_ticket",
            ):
                self._assert_public_failure(
                    lambda: release_public_acceptance.verify_public_release(
                        fixture.request(),
                        downloader=MutatingDownloader(),
                        runner=RecordingRunner(),
                        get_attempts=1,
                    ),
                    "changed after it was prepared",
                )

    def test_anonymous_get_retry_exhaustion_is_explicit_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="public-acceptance-test-") as temporary:
            fixture = AcceptanceFixture(Path(temporary))
            downloader = FailingPublicDownloader()
            self._assert_public_failure(
                lambda: release_public_acceptance.verify_public_release(
                    fixture.request(),
                    downloader=downloader,
                    runner=RecordingRunner(),
                    get_attempts=3,
                ),
                "failed after 3 read-only attempts",
            )
            self.assertEqual(downloader.calls, 3)

    def test_corrupt_or_missing_public_asset_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="public-acceptance-test-") as temporary:
            fixture = AcceptanceFixture(Path(temporary))
            corrupt_payloads = dict(fixture.asset_payloads)
            corrupt_payloads[fixture.ids[fixture.assets[0].name]] = b"corrupt"
            self._assert_public_failure(
                lambda: release_public_acceptance.verify_public_release(
                    fixture.request(),
                    downloader=FakePublicDownloader(
                        [_public_json(fixture.assets, fixture.ids)], corrupt_payloads
                    ),
                    runner=RecordingRunner(),
                    get_attempts=2,
                ),
                "asset GET",
            )

        with tempfile.TemporaryDirectory(prefix="public-acceptance-test-") as temporary:
            fixture = AcceptanceFixture(Path(temporary))
            missing = fixture.assets[0].name
            self._assert_public_failure(
                lambda: release_public_acceptance.verify_public_release(
                    fixture.request(),
                    downloader=FakePublicDownloader(
                        [
                            _public_json(
                                fixture.assets,
                                fixture.ids,
                                omitted_name=missing,
                            )
                        ],
                        fixture.asset_payloads,
                    ),
                    runner=RecordingRunner(),
                    get_attempts=1,
                ),
                "asset names differ",
            )

    def test_mutable_public_release_is_never_accepted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="public-acceptance-test-") as temporary:
            fixture = AcceptanceFixture(Path(temporary))
            self._assert_public_failure(
                lambda: release_public_acceptance.verify_public_release(
                    fixture.request(),
                    downloader=FakePublicDownloader(
                        [
                            _public_json(
                                fixture.assets, fixture.ids, immutable=False
                            )
                        ],
                        fixture.asset_payloads,
                    ),
                    runner=RecordingRunner(),
                    get_attempts=1,
                ),
                "metadata differs",
            )


if __name__ == "__main__":
    unittest.main()
