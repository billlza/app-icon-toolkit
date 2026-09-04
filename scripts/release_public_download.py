"""Credential-free HTTPS transfer and crash reconciliation for public releases."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Protocol

from release_files import (
    FilePublicationIndeterminate,
    ReleaseFileError,
    inspect_regular_file,
    open_stable_regular_file,
    publish_sibling_no_replace,
)
from release_public import (
    MAX_PUBLIC_RELEASE_JSON_BYTES,
    PublicAssetPlan,
    PublicVerificationError,
    matches_public_asset,
    validate_private_directory,
)
from release_targets import (
    validate_commit_sha,
    validate_release_tag,
    validate_repository,
)


MAX_CURL_DIAGNOSTIC_BYTES = 64 * 1024
PUBLIC_DOWNLOAD_TIMEOUT_SECONDS = 600
MAX_PUBLIC_TAG_JSON_BYTES = 256 * 1024
_GITHUB_AUTH_ENVIRONMENT = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
_SYSTEM_CURL = Path("/usr/bin/curl")


class PublicAssetDownloader(Protocol):
    """Boundary for one anonymous HTTPS transfer into an existing file."""

    def download(
        self,
        url: str,
        destination: Path,
        maximum_bytes: int,
        media_type: str,
    ) -> None: ...


class CurlPublicAssetDownloader:
    """Download without GitHub credentials or user curl configuration."""

    def __init__(self, timeout_seconds: int = PUBLIC_DOWNLOAD_TIMEOUT_SECONDS) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("public download timeout must be positive")
        try:
            curl_metadata = os.lstat(_SYSTEM_CURL)
        except OSError as error:
            raise PublicVerificationError(
                f"cannot inspect the system curl executable: {error}"
            ) from error
        if (
            not stat.S_ISREG(curl_metadata.st_mode)
            or curl_metadata.st_uid != 0
            or curl_metadata.st_mode & 0o111 == 0
            or curl_metadata.st_mode & 0o022 != 0
        ):
            raise PublicVerificationError(
                f"system curl is not a root-owned executable file: {_SYSTEM_CURL}"
            )
        self.timeout_seconds = timeout_seconds

    def download(
        self,
        url: str,
        destination: Path,
        maximum_bytes: int,
        media_type: str,
    ) -> None:
        if (
            isinstance(maximum_bytes, bool)
            or not isinstance(maximum_bytes, int)
            or maximum_bytes <= 0
        ):
            raise ValueError("public download size limit must be positive")
        if media_type not in {
            "application/octet-stream",
            "application/vnd.github+json",
        }:
            raise ValueError("unsupported public download media type")
        command = (
            str(_SYSTEM_CURL),
            "--disable",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-redirs",
            "5",
            "--connect-timeout",
            "30",
            "--max-time",
            str(self.timeout_seconds),
            "--max-filesize",
            str(maximum_bytes),
            "--header",
            f"Accept: {media_type}",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
            url,
        )
        try:
            named_before = os.lstat(destination)
            descriptor = os.open(
                destination,
                os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as error:
            raise PublicVerificationError(
                f"cannot open the public download destination safely: {error}"
            ) from error
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(named_before.st_mode)
            or named_before.st_nlink != 1
            or (named_before.st_dev, named_before.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            os.close(descriptor)
            raise PublicVerificationError(
                "public download destination changed while it was opened"
            )
        try:
            os.ftruncate(descriptor, 0)
        except OSError as error:
            os.close(descriptor)
            raise PublicVerificationError(
                f"cannot truncate the verified public download destination: {error}"
            ) from error

        with os.fdopen(descriptor, "wb") as output, tempfile.TemporaryFile(
            mode="w+b"
        ) as diagnostic:
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=diagnostic,
                    env={"LC_ALL": "C", "LANG": "C"},
                    timeout=self.timeout_seconds + 30,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise PublicVerificationError(
                    f"anonymous public download failed to execute: {error}"
                ) from error
            try:
                output.flush()
                os.fsync(output.fileno())
            except OSError as error:
                raise PublicVerificationError(
                    f"cannot synchronize anonymous public download: {error}"
                ) from error
            diagnostic_size = os.fstat(diagnostic.fileno()).st_size
            if diagnostic_size > MAX_CURL_DIAGNOSTIC_BYTES:
                raise PublicVerificationError(
                    "anonymous public download diagnostic exceeded the size limit"
                )
            diagnostic.seek(0)
            try:
                message = diagnostic.read().decode("utf-8", errors="strict").strip()
            except UnicodeError as error:
                raise PublicVerificationError(
                    "anonymous public download diagnostic was not UTF-8"
                ) from error
        if result.returncode != 0:
            raise PublicVerificationError(
                f"anonymous public download failed with exit {result.returncode}: "
                f"{message or 'no diagnostic'}"
            )
        if message:
            raise PublicVerificationError(
                f"anonymous public download wrote unexpected diagnostic: {message}"
            )


def _fsync_directory(directory: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise PublicVerificationError(
            f"cannot synchronize public download directory {directory}: {error}"
        ) from error


def _transfer_candidates(plan: PublicAssetPlan) -> tuple[Path, ...]:
    prefix = f".{plan.name}."
    suffix = ".public-download"
    try:
        return tuple(
            entry
            for entry in plan.destination.parent.iterdir()
            if entry.name.startswith(prefix) and entry.name.endswith(suffix)
        )
    except OSError as error:
        raise PublicVerificationError(
            f"cannot inspect public download transfer state: {error}"
        ) from error


def _select_resumable_transfer(plan: PublicAssetPlan) -> Path | None:
    candidates = _transfer_candidates(plan)
    if not candidates:
        return None
    exact: list[Path] = []
    for candidate in candidates:
        try:
            metadata = os.lstat(candidate)
        except OSError as error:
            raise PublicVerificationError(
                f"cannot inspect public download transfer evidence: {error}"
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise PublicVerificationError(
                f"public download transfer evidence is unsafe: {candidate}"
            )
        if matches_public_asset(candidate, plan):
            exact.append(candidate)

    selected = exact[0] if exact else None
    for candidate in candidates:
        if candidate == selected:
            continue
        try:
            candidate.unlink()
        except OSError as error:
            raise PublicVerificationError(
                f"cannot discard failed public download transfer: {error}"
            ) from error
    _fsync_directory(plan.destination.parent)
    return selected


def _reconcile_linked_download(plan: PublicAssetPlan) -> bool:
    if not matches_public_asset(
        plan.destination, plan, require_single_link=False
    ):
        return False
    try:
        destination_metadata = os.lstat(plan.destination)
    except OSError as error:
        raise PublicVerificationError(
            f"cannot inspect linked public asset {plan.name}: {error}"
        ) from error
    if destination_metadata.st_nlink != 2:
        return False
    aliases = []
    for candidate in _transfer_candidates(plan):
        try:
            metadata = os.lstat(candidate)
        except OSError as error:
            raise PublicVerificationError(
                f"cannot inspect public download transfer alias: {error}"
            ) from error
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 2
            and (metadata.st_dev, metadata.st_ino)
            == (destination_metadata.st_dev, destination_metadata.st_ino)
            and matches_public_asset(candidate, plan, require_single_link=False)
        ):
            aliases.append(candidate)
    if len(aliases) != 1:
        return False
    try:
        aliases[0].unlink()
    except OSError as error:
        raise PublicVerificationError(
            f"cannot reconcile public download transfer alias: {error}"
        ) from error
    _fsync_directory(plan.destination.parent)
    return matches_public_asset(plan.destination, plan)


def _download_anonymous_json(
    *,
    url: str,
    temporary_prefix: str,
    maximum_bytes: int,
    label: str,
    transfer_directory: Path,
    downloader: PublicAssetDownloader,
) -> bytes:
    """Download one bounded public JSON document through the anonymous boundary."""

    if maximum_bytes <= 0:
        raise ValueError("anonymous JSON size limit must be positive")
    validate_private_directory(transfer_directory, "public transfer directory")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=temporary_prefix,
        suffix=".json",
        dir=transfer_directory,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        downloader.download(
            url,
            temporary,
            maximum_bytes,
            "application/vnd.github+json",
        )
        try:
            with temporary.open("rb") as downloaded:
                os.fsync(downloaded.fileno())
        except OSError as error:
            raise PublicVerificationError(
                f"cannot synchronize {label}: {error}"
            ) from error
        with open_stable_regular_file(
            temporary,
            label=label,
            require_single_link=True,
        ) as (opened, snapshot):
            if snapshot.size > maximum_bytes:
                raise PublicVerificationError(f"{label} exceeded the size limit")
            payload = opened.read(maximum_bytes + 1)
        if not payload or len(payload) > maximum_bytes:
            raise PublicVerificationError(f"{label} is empty or oversized")
        return payload
    except ReleaseFileError as error:
        raise PublicVerificationError(str(error)) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def download_anonymous_release_json(
    repository: str,
    release_id: int,
    transfer_directory: Path,
    downloader: PublicAssetDownloader,
) -> bytes:
    """Read the public numeric release API without any GitHub credential."""

    try:
        repository = validate_repository(repository)
    except RuntimeError as error:
        raise PublicVerificationError(str(error)) from error
    if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
        raise PublicVerificationError("public release id must be a positive integer")
    return _download_anonymous_json(
        url=f"https://api.github.com/repos/{repository}/releases/{release_id}",
        temporary_prefix=f".release-{release_id}.",
        maximum_bytes=MAX_PUBLIC_RELEASE_JSON_BYTES,
        label="anonymous public release JSON",
        transfer_directory=transfer_directory,
        downloader=downloader,
    )


def download_anonymous_tag_ref_json(
    repository: str,
    tag: str,
    transfer_directory: Path,
    downloader: PublicAssetDownloader,
) -> bytes:
    """Read the public annotated-tag reference without credentials."""

    try:
        repository = validate_repository(repository)
        tag = validate_release_tag(tag)
    except RuntimeError as error:
        raise PublicVerificationError(str(error)) from error
    return _download_anonymous_json(
        url=f"https://api.github.com/repos/{repository}/git/ref/tags/{tag}",
        temporary_prefix=f".tag-ref-{tag}.",
        maximum_bytes=MAX_PUBLIC_TAG_JSON_BYTES,
        label="anonymous public tag reference JSON",
        transfer_directory=transfer_directory,
        downloader=downloader,
    )


def download_anonymous_tag_object_json(
    repository: str,
    tag_object_sha: str,
    transfer_directory: Path,
    downloader: PublicAssetDownloader,
) -> bytes:
    """Read one public annotated-tag object without credentials."""

    try:
        repository = validate_repository(repository)
        tag_object_sha = validate_commit_sha(tag_object_sha)
    except RuntimeError as error:
        raise PublicVerificationError(str(error)) from error
    return _download_anonymous_json(
        url=f"https://api.github.com/repos/{repository}/git/tags/{tag_object_sha}",
        temporary_prefix=f".tag-object-{tag_object_sha}.",
        maximum_bytes=MAX_PUBLIC_TAG_JSON_BYTES,
        label="anonymous public tag object JSON",
        transfer_directory=transfer_directory,
        downloader=downloader,
    )


def download_public_asset(
    plan: PublicAssetPlan,
    downloader: PublicAssetDownloader,
) -> Path:
    """Download one expected public asset, or prove a prior exact download."""

    validate_private_directory(
        plan.destination.parent, "public download destination"
    )
    if matches_public_asset(plan.destination, plan):
        return plan.destination
    if os.path.lexists(plan.destination):
        if _reconcile_linked_download(plan):
            return plan.destination
        raise PublicVerificationError(
            f"persisted anonymous public asset differs from the release: {plan.name}"
        )

    resumable = _select_resumable_transfer(plan)
    if resumable is not None:
        temporary = resumable
        descriptor = -1
        created = False
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{plan.name}.",
            suffix=".public-download",
            dir=plan.destination.parent,
        )
        temporary = Path(temporary_name)
        created = True
    linked = False
    preserve_temporary = not created
    try:
        if created:
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
            descriptor = -1
            downloader.download(
                plan.url,
                temporary,
                plan.size,
                "application/octet-stream",
            )
        try:
            with temporary.open("rb") as downloaded:
                os.fsync(downloaded.fileno())
        except OSError as error:
            raise PublicVerificationError(
                f"cannot synchronize anonymous public asset {plan.name}: {error}"
            ) from error
        if not matches_public_asset(temporary, plan):
            raise PublicVerificationError(
                f"anonymous public asset differs from the prepared bytes: {plan.name}"
            )
        try:
            publish_sibling_no_replace(
                temporary,
                plan.destination,
                label=f"anonymous public asset {plan.name}",
            )
            linked = True
            _fsync_directory(plan.destination.parent)
            temporary.unlink()
            _fsync_directory(plan.destination.parent)
        except FilePublicationIndeterminate as error:
            preserve_temporary = True
            raise PublicVerificationError(
                f"cannot reconcile anonymous public asset publication {plan.name}: {error}"
            ) from error
        except (OSError, ReleaseFileError) as error:
            raise PublicVerificationError(
                f"cannot preserve anonymous public asset {plan.name}: {error}"
            ) from error
        if not matches_public_asset(plan.destination, plan):
            raise PublicVerificationError(
                f"published anonymous public asset cannot be reconciled: {plan.name}"
            )
        return plan.destination
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created and not linked and not preserve_temporary:
            temporary.unlink(missing_ok=True)
