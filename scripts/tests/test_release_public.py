from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_draft
import release_public
import release_public_download


REPOSITORY = "example/app-icon-toolkit"
TAG = "v1.2.3"


class RecordingDownloader:
    def __init__(self, payload: bytes, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, Path, int, str]] = []

    def download(
        self,
        url: str,
        destination: Path,
        maximum_bytes: int,
        media_type: str,
    ) -> None:
        self.calls.append((url, destination, maximum_bytes, media_type))
        if self.error is not None:
            raise self.error
        destination.write_bytes(self.payload)


def local_asset(path: Path) -> release_draft.LocalAsset:
    payload = path.read_bytes()
    return release_draft.LocalAsset(
        name=path.name,
        path=path,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def public_release(
    assets: tuple[release_draft.LocalAsset, ...],
) -> release_public.AnonymousPublicRelease:
    return release_public.AnonymousPublicRelease(
        repository=REPOSITORY,
        release_id=456,
        tag=TAG,
        name=f"App Icon Toolkit {TAG}",
        body="# Release notes\n",
        assets=tuple(
            release_public.AnonymousPublicAsset(
                asset_id=100 + index,
                name=asset.name,
                size=asset.size,
                sha256=asset.sha256,
                api_url=(
                    f"https://api.github.com/repos/{REPOSITORY}/releases/assets/"
                    f"{100 + index}"
                ),
            )
            for index, asset in enumerate(assets)
        ),
    )


def public_release_json(
    expected_assets: tuple[release_draft.LocalAsset, ...], **changes: object
) -> bytes:
    value: dict[str, object] = {
        "id": 456,
        "tag_name": TAG,
        "name": f"App Icon Toolkit {TAG}",
        "body": "# Release notes\n",
        "draft": False,
        "prerelease": False,
        "immutable": True,
        "assets": [
            {
                "id": 100 + index,
                "name": asset.name,
                "size": asset.size,
                "digest": f"sha256:{asset.sha256}",
                "state": "uploaded",
                "url": (
                    f"https://api.github.com/repos/{REPOSITORY}/releases/assets/"
                    f"{100 + index}"
                ),
                "browser_download_url": (
                    f"https://github.com/{REPOSITORY}/releases/download/{TAG}/"
                    f"{asset.name}"
                ),
            }
            for index, asset in enumerate(expected_assets)
        ],
    }
    value.update(changes)
    return json.dumps(value).encode("utf-8")


class PublicReleaseTests(unittest.TestCase):
    def test_anonymous_numeric_release_and_asset_identities_are_exact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-public-test-") as temporary:
            archive = Path(temporary) / "candidate.zip"
            archive.write_bytes(b"candidate")
            assets = (local_asset(archive),)
            release = release_public.parse_anonymous_public_release(
                public_release_json(assets),
                repository=REPOSITORY,
                expected_release_id=456,
                expected_tag=TAG,
                expected_name=f"App Icon Toolkit {TAG}",
                expected_body="# Release notes\n",
                expected_assets=assets,
            )
            self.assertEqual(release.release_id, 456)
            self.assertEqual(release.assets[0].asset_id, 100)

            with self.assertRaisesRegex(
                release_public.PublicVerificationError, "repeats key"
            ):
                release_public.parse_anonymous_public_release(
                    b'{"id":456,"id":456}',
                    repository=REPOSITORY,
                    expected_release_id=456,
                    expected_tag=TAG,
                    expected_name=f"App Icon Toolkit {TAG}",
                    expected_body="# Release notes\n",
                    expected_assets=assets,
                )

            for changes in (
                {"id": 457},
                {"draft": True},
                {"immutable": False},
                {
                    "assets": [
                        {
                            "id": 100,
                            "name": archive.name,
                            "size": archive.stat().st_size,
                            "digest": f"sha256:{'0' * 64}",
                            "state": "uploaded",
                            "url": (
                                f"https://api.github.com/repos/{REPOSITORY}/"
                                "releases/assets/100"
                            ),
                            "browser_download_url": (
                                f"https://github.com/{REPOSITORY}/releases/download/"
                                f"{TAG}/{archive.name}"
                            ),
                        }
                    ]
                },
            ):
                with self.subTest(changes=changes):
                    with self.assertRaises(release_public.PublicVerificationError):
                        release_public.parse_anonymous_public_release(
                            public_release_json(assets, **changes),
                            repository=REPOSITORY,
                            expected_release_id=456,
                            expected_tag=TAG,
                            expected_name=f"App Icon Toolkit {TAG}",
                            expected_body="# Release notes\n",
                            expected_assets=assets,
                        )

    @unittest.skipUnless(os.name == "posix", "POSIX release download boundary")
    def test_anonymous_release_metadata_download_is_bounded_and_temporary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-public-test-") as temporary:
            transfer = Path(temporary) / "transfer"
            transfer.mkdir(mode=0o700)
            payload = b'{"id":456}'
            downloader = RecordingDownloader(payload)
            observed = release_public_download.download_anonymous_release_json(
                REPOSITORY, 456, transfer, downloader
            )
            self.assertEqual(observed, payload)
            self.assertEqual(len(downloader.calls), 1)
            self.assertEqual(
                downloader.calls[0][3], "application/vnd.github+json"
            )
            self.assertEqual(list(transfer.iterdir()), [])

    def test_plan_uses_exact_https_release_urls_and_safe_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-public-test-") as temporary:
            root = Path(temporary)
            source = root / "candidate.zip"
            source.write_bytes(b"candidate")
            destination = root / "downloads"
            destination.mkdir()
            destination.chmod(0o700)
            asset = local_asset(source)
            plans = release_public.plan_public_downloads(
                public_release((asset,)), (asset,), destination
            )
            self.assertEqual(len(plans), 1)
            self.assertEqual(
                plans[0].url,
                "https://api.github.com/repos/example/app-icon-toolkit/"
                "releases/assets/100",
            )
            self.assertEqual(plans[0].destination, destination / "candidate.zip")

            unsafe = release_draft.LocalAsset(
                name="../candidate.zip",
                path=source,
                size=9,
                sha256="0" * 64,
            )
            with self.assertRaisesRegex(
                release_public.PublicVerificationError, "unsafe"
            ):
                release_public.plan_public_downloads(
                    public_release((unsafe,)), (unsafe,), destination
                )

    @unittest.skipUnless(os.name == "posix", "POSIX release download boundary")
    def test_curl_boundary_drops_github_auth_and_disables_non_https_redirects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-public-test-") as temporary:
            destination = Path(temporary) / "asset.zip"
            destination.write_bytes(b"")

            def run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                environment = kwargs["env"]
                self.assertEqual(environment, {"LC_ALL": "C", "LANG": "C"})
                for name in release_public_download._GITHUB_AUTH_ENVIRONMENT:
                    self.assertNotIn(name, environment)
                self.assertEqual(command[0:2], ("/usr/bin/curl", "--disable"))
                self.assertIn("=https", command)
                self.assertIn("--proto-redir", command)
                self.assertNotIn("--output", command)
                self.assertNotIn("Authorization", " ".join(command))
                self.assertNotEqual(kwargs["stdout"], subprocess.DEVNULL)
                return subprocess.CompletedProcess(command, 0, b"", b"")

            environment = {
                name: f"secret-{name}"
                for name in release_public_download._GITHUB_AUTH_ENVIRONMENT
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                release_public_download.subprocess, "run", side_effect=run
            ):
                release_public_download.CurlPublicAssetDownloader(timeout_seconds=30).download(
                    "https://github.com/example/repo/releases/download/v1.2.3/asset.zip",
                    destination,
                    123,
                    "application/octet-stream",
                )

    @unittest.skipUnless(os.name == "posix", "POSIX release download boundary")
    def test_download_is_exact_resumable_and_never_publishes_wrong_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-public-test-") as temporary:
            root = Path(temporary)
            source = root / "candidate.zip"
            source.write_bytes(b"exact public bytes")
            destination = root / "downloads"
            transfer = root / "transfer"
            destination.mkdir()
            destination.chmod(0o700)
            transfer.mkdir()
            transfer.chmod(0o700)
            asset = local_asset(source)
            plan = release_public.plan_public_downloads(
                public_release((asset,)), (asset,), destination
            )[0]

            downloader = RecordingDownloader(source.read_bytes())
            published = release_public_download.download_public_asset(
                plan, downloader
            )
            self.assertEqual(published.read_bytes(), source.read_bytes())
            self.assertEqual(len(downloader.calls), 1)
            self.assertEqual(list(destination.glob(".*.public-download")), [])

            release_public_download.download_public_asset(plan, downloader)
            self.assertEqual(len(downloader.calls), 1)

        with tempfile.TemporaryDirectory(prefix="release-public-test-") as temporary:
            root = Path(temporary)
            source = root / "candidate.zip"
            source.write_bytes(b"expected")
            destination = root / "downloads"
            transfer = root / "transfer"
            destination.mkdir()
            destination.chmod(0o700)
            transfer.mkdir()
            transfer.chmod(0o700)
            asset = local_asset(source)
            plan = release_public.plan_public_downloads(
                public_release((asset,)), (asset,), destination
            )[0]
            stale = destination / f".{plan.name}.interrupted.public-download"
            stale.write_bytes(b"partial")
            stale.chmod(0o600)
            with self.assertRaisesRegex(
                release_public.PublicVerificationError, "differs"
            ):
                release_public_download.download_public_asset(
                    plan, RecordingDownloader(b"corrupt")
                )
            self.assertFalse(plan.destination.exists())
            self.assertEqual(list(destination.glob(".*.public-download")), [])

    @unittest.skipUnless(os.name == "posix", "POSIX release download boundary")
    def test_indeterminate_link_is_reconciled_without_redownload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-public-test-") as temporary:
            root = Path(temporary)
            source = root / "candidate.zip"
            source.write_bytes(b"exact public bytes")
            destination = root / "downloads"
            destination.mkdir(mode=0o700)
            asset = local_asset(source)
            plan = release_public.plan_public_downloads(
                public_release((asset,)), (asset,), destination
            )[0]
            downloader = RecordingDownloader(source.read_bytes())

            def link_then_fail(temporary: Path, final: Path, *, label: str) -> None:
                self.assertIn("anonymous public asset", label)
                os.link(temporary, final)
                raise release_public_download.FilePublicationIndeterminate(
                    "injected late error"
                )

            with mock.patch.object(
                release_public_download,
                "publish_sibling_no_replace",
                side_effect=link_then_fail,
            ):
                with self.assertRaisesRegex(
                    release_public.PublicVerificationError, "cannot reconcile"
                ):
                    release_public_download.download_public_asset(plan, downloader)
            self.assertEqual(os.lstat(plan.destination).st_nlink, 2)
            self.assertEqual(len(list(destination.glob(".*.public-download"))), 1)

            recovered = release_public_download.download_public_asset(plan, downloader)
            self.assertEqual(recovered.read_bytes(), source.read_bytes())
            self.assertEqual(os.lstat(recovered).st_nlink, 1)
            self.assertEqual(len(downloader.calls), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX release download boundary")
    def test_exact_set_and_sha256sums_are_verified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-public-test-") as temporary:
            root = Path(temporary)
            prepared = root / "prepared"
            downloads = root / "downloads"
            prepared.mkdir()
            downloads.mkdir()
            downloads.chmod(0o700)
            archive = prepared / "candidate.zip"
            archive.write_bytes(b"archive")
            archive_asset = local_asset(archive)
            checksum = prepared / release_draft.CHECKSUM_ASSET_NAME
            checksum.write_bytes(release_draft.render_sha256sums((archive_asset,)))
            assets = (archive_asset, local_asset(checksum))
            plans = release_public.plan_public_downloads(
                public_release(assets), assets, downloads
            )
            for plan in plans:
                source = prepared / plan.name
                plan.destination.write_bytes(source.read_bytes())
                plan.destination.chmod(0o600)

            verified = release_public.validate_public_downloads(plans, downloads)
            self.assertEqual({path.name for path in verified}, {asset.name for asset in assets})

            unexpected = downloads / "unexpected.zip"
            unexpected.write_bytes(b"unexpected")
            with self.assertRaisesRegex(
                release_public.PublicVerificationError, "asset set mismatch"
            ):
                release_public.validate_public_downloads(plans, downloads)

    @unittest.skipUnless(os.name == "posix", "POSIX release download boundary")
    def test_checksum_semantics_are_independent_of_checksum_file_digest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-public-test-") as temporary:
            root = Path(temporary)
            archive = root / "candidate.zip"
            archive.write_bytes(b"archive")
            checksum = root / release_draft.CHECKSUM_ASSET_NAME
            checksum.write_bytes(b"0" * 64 + b"  candidate.zip\n")
            plans = (
                release_public.PublicAssetPlan(
                    asset_id=100,
                    name=archive.name,
                    url="https://github.com/example/repo/archive",
                    destination=archive,
                    size=archive.stat().st_size,
                    sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                ),
                release_public.PublicAssetPlan(
                    asset_id=101,
                    name=checksum.name,
                    url="https://github.com/example/repo/checksum",
                    destination=checksum,
                    size=checksum.stat().st_size,
                    sha256=hashlib.sha256(checksum.read_bytes()).hexdigest(),
                ),
            )
            with self.assertRaisesRegex(
                release_public.PublicVerificationError, "SHA256SUMS differs"
            ):
                release_public.validate_public_downloads(plans, root)


if __name__ == "__main__":
    unittest.main()
