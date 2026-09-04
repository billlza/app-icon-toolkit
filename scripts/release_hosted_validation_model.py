"""Domain model and binding rules for hosted signed-draft validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
from typing import Sequence

from release_draft import LocalAsset, WorkflowRun
from release_targets import (
    ReleaseContract,
    validate_commit_sha,
    validate_release_tag,
    validate_repository,
)


SCHEMA_VERSION = 1
EXPECTED_WORKFLOW_NAME = "Validate Signed Draft"
EXPECTED_WORKFLOW_EVENT = "workflow_dispatch"
EXPECTED_WORKFLOW_PATH = ".github/workflows/validate-signed-draft.yml"
CHECKSUM_ASSET_NAME = "SHA256SUMS"
MAX_ASSETS = 64
MAX_VALIDATIONS = 8
ARCHITECTURES = frozenset({"arm64", "x86_64"})
HOSTED_RUNNERS = {
    "arm64": "macos-15",
    "x86_64": "macos-15-intel",
}
SHA1_IDENTITY = re.compile(r"^[0-9A-F]{40}$")


class HostedValidationError(RuntimeError):
    """Hosted validation input or evidence does not satisfy the release contract."""


@dataclass(frozen=True)
class DraftReleaseAsset:
    asset_id: int
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class DraftRelease:
    release_id: str
    release_database_id: int
    tag: str
    name: str
    body: str
    is_draft: bool
    is_prerelease: bool
    assets: tuple[DraftReleaseAsset, ...]

    @property
    def body_sha256(self) -> str:
        try:
            encoded = self.body.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise HostedValidationError(
                "GitHub draft release body is not valid UTF-8 text"
            ) from error
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReceiptRelease:
    release_id: str
    release_database_id: int
    tag: str
    name: str
    body_sha256: str
    is_draft: bool
    is_prerelease: bool


@dataclass(frozen=True)
class ValidationSpec:
    validation_id: str
    target_id: str
    runner: str
    runtime_architecture: str
    archive_asset_id: int
    archive_name: str
    archive_size: int
    archive_sha256: str
    archive_format: str
    binary_name: str
    expected_architectures: tuple[str, ...]

    def matrix_entry(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationResult:
    validation_id: str
    target_id: str
    runner: str
    runtime_architecture: str
    archive_asset_id: int
    archive_name: str
    archive_size: int
    archive_sha256: str
    archive_format: str
    binary_name: str
    expected_architectures: tuple[str, ...]
    binary_sha256: str
    identity_sha1: str
    identifier: str
    team_id: str
    architectures: tuple[str, ...]
    signature_valid: bool
    notarization_ticket_valid: bool
    mcp_smoke_valid: bool


@dataclass(frozen=True)
class HostedValidationPlan:
    repository: str
    source_run: WorkflowRun
    validation_run: WorkflowRun
    release: ReceiptRelease
    identity_sha1: str
    assets: tuple[DraftReleaseAsset, ...]
    validations: tuple[ValidationSpec, ...]


@dataclass(frozen=True)
class HostedValidationReceipt:
    repository: str
    source_run: WorkflowRun
    validation_run: WorkflowRun
    release: ReceiptRelease
    identity_sha1: str
    assets: tuple[DraftReleaseAsset, ...]
    results: tuple[ValidationResult, ...]


@dataclass(frozen=True)
class HostedWorkflowIdentity:
    workflow_id: int
    name: str
    path: str
    state: str


def _validated_identity(identity_sha1: str) -> str:
    if not isinstance(identity_sha1, str) or SHA1_IDENTITY.fullmatch(identity_sha1) is None:
        raise HostedValidationError(
            "Developer ID identity must be an exact uppercase SHA-1 fingerprint"
        )
    return identity_sha1


def _validated_run(run: WorkflowRun, context: str) -> WorkflowRun:
    for name, value in (
        ("workflow_id", run.workflow_id),
        ("run_id", run.run_id),
        ("attempt", run.attempt),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise HostedValidationError(f"{context}.{name} must be a positive integer")
    try:
        tag = validate_release_tag(run.tag)
        head_sha = validate_commit_sha(run.head_sha)
    except RuntimeError as error:
        raise HostedValidationError(str(error)) from error
    return WorkflowRun(
        workflow_id=run.workflow_id,
        run_id=run.run_id,
        attempt=run.attempt,
        tag=tag,
        head_sha=head_sha,
    )


def validation_specs(
    contract: ReleaseContract,
    tag: str,
    assets: Sequence[DraftReleaseAsset],
) -> tuple[ValidationSpec, ...]:
    """Derive the native validation jobs from the shared release target contract."""

    try:
        tag = validate_release_tag(tag)
    except RuntimeError as error:
        raise HostedValidationError(str(error)) from error
    asset_map = {asset.name: asset for asset in assets}
    if len(asset_map) != len(assets):
        raise HostedValidationError("release assets contain duplicate names")
    specs: list[ValidationSpec] = []
    for target in contract.targets:
        if target.family not in {"macos", "macos_universal2"}:
            continue
        archive_name = target.release_filename(tag)
        asset = asset_map.get(archive_name)
        if asset is None:
            raise HostedValidationError(f"signed draft omitted macOS asset {archive_name}")
        expected_architectures = target.macos_architectures()
        if target.family == "macos":
            architecture = expected_architectures[0]
            runners = ((HOSTED_RUNNERS[architecture], architecture),)
        else:
            if target.native_verify_runner is None:
                raise HostedValidationError(
                    f"universal target omitted its native verifier: {target.id}"
                )
            runners = (
                (HOSTED_RUNNERS["arm64"], "arm64"),
                (HOSTED_RUNNERS["x86_64"], "x86_64"),
            )
        for runner, runtime_architecture in runners:
            specs.append(
                ValidationSpec(
                    validation_id=f"{target.id}--{runtime_architecture}",
                    target_id=target.id,
                    runner=runner,
                    runtime_architecture=runtime_architecture,
                    archive_asset_id=asset.asset_id,
                    archive_name=asset.name,
                    archive_size=asset.size,
                    archive_sha256=asset.sha256,
                    archive_format=target.archive_format,
                    binary_name=target.binary_name,
                    expected_architectures=expected_architectures,
                )
            )
    result = tuple(sorted(specs, key=lambda spec: spec.validation_id))
    if len(result) != 4:
        raise HostedValidationError(
            f"release contract must produce four hosted macOS validations; found {len(result)}"
        )
    return result


def _receipt_release(release: DraftRelease) -> ReceiptRelease:
    return ReceiptRelease(
        release_id=release.release_id,
        release_database_id=release.release_database_id,
        tag=release.tag,
        name=release.name,
        body_sha256=release.body_sha256,
        is_draft=release.is_draft,
        is_prerelease=release.is_prerelease,
    )


def require_exact_draft_release(
    refreshed_release: DraftRelease,
    *,
    expected_release: ReceiptRelease,
    expected_assets: Sequence[DraftReleaseAsset],
) -> None:
    """Require one REST draft snapshot to preserve exact release and asset IDs."""

    if _receipt_release(refreshed_release) != expected_release:
        raise HostedValidationError(
            "draft release identity or metadata changed during validation"
        )
    if tuple(sorted(refreshed_release.assets, key=lambda asset: asset.name)) != tuple(
        expected_assets
    ):
        raise HostedValidationError("draft release assets changed during validation")


def create_plan(
    *,
    repository: str,
    source_run: WorkflowRun,
    validation_run: WorkflowRun,
    release: DraftRelease,
    release_notes: str,
    identity_sha1: str,
    contract: ReleaseContract,
) -> HostedValidationPlan:
    try:
        repository = validate_repository(repository)
    except RuntimeError as error:
        raise HostedValidationError(str(error)) from error
    source_run = _validated_run(source_run, "source_run")
    validation_run = _validated_run(validation_run, "validation_run")
    identity_sha1 = _validated_identity(identity_sha1)
    if source_run.tag != validation_run.tag or source_run.head_sha != validation_run.head_sha:
        raise HostedValidationError(
            "source and hosted validation runs must bind the same tag and commit"
        )
    if release.tag != source_run.tag:
        raise HostedValidationError("draft release tag differs from the workflow binding")
    if release.name != f"App Icon Toolkit {release.tag}":
        raise HostedValidationError("draft release name differs from the release contract")
    if release.body != release_notes:
        raise HostedValidationError("draft release body differs from tagged release notes")
    if not release.is_draft or release.is_prerelease:
        raise HostedValidationError("release must be a stable unpublished draft")
    expected_names = {
        *(target.release_filename(release.tag) for target in contract.targets),
        CHECKSUM_ASSET_NAME,
    }
    actual_names = {asset.name for asset in release.assets}
    if actual_names != expected_names:
        raise HostedValidationError(
            "signed draft asset set is not exact; "
            f"missing={sorted(expected_names - actual_names)}; "
            f"extra={sorted(actual_names - expected_names)}"
        )
    return HostedValidationPlan(
        repository=repository,
        source_run=source_run,
        validation_run=validation_run,
        release=_receipt_release(release),
        identity_sha1=identity_sha1,
        assets=tuple(sorted(release.assets, key=lambda asset: asset.name)),
        validations=validation_specs(contract, release.tag, release.assets),
    )


def _validated_results(
    plan: HostedValidationPlan,
    results: Sequence[ValidationResult],
    contract: ReleaseContract,
) -> tuple[ValidationResult, ...]:
    ordered = tuple(sorted(results, key=lambda result: result.validation_id))
    if len({result.validation_id for result in ordered}) != len(ordered):
        raise HostedValidationError("hosted validation results contain duplicate IDs")
    result_specs = tuple(
        ValidationSpec(
            validation_id=result.validation_id,
            target_id=result.target_id,
            runner=result.runner,
            runtime_architecture=result.runtime_architecture,
            archive_asset_id=result.archive_asset_id,
            archive_name=result.archive_name,
            archive_size=result.archive_size,
            archive_sha256=result.archive_sha256,
            archive_format=result.archive_format,
            binary_name=result.binary_name,
            expected_architectures=result.expected_architectures,
        )
        for result in ordered
    )
    if result_specs != plan.validations:
        raise HostedValidationError("hosted validation results do not match the exact plan")
    for result in ordered:
        if result.identity_sha1 != plan.identity_sha1:
            raise HostedValidationError("hosted result uses a different signing identity")
        if result.identifier != contract.macos_signing.code_identifier:
            raise HostedValidationError("hosted result uses a different code identifier")
        if result.team_id != contract.macos_signing.team_id:
            raise HostedValidationError("hosted result uses a different signing team")
        if result.architectures != result.expected_architectures:
            raise HostedValidationError("hosted result architecture set differs from its target")
        if not (
            result.signature_valid
            and result.notarization_ticket_valid
            and result.mcp_smoke_valid
        ):
            raise HostedValidationError("hosted validation result is not successful")
    return ordered


def create_bound_receipt(
    plan: HostedValidationPlan,
    *,
    refreshed_release: DraftRelease,
    results: Sequence[ValidationResult],
    contract: ReleaseContract,
) -> HostedValidationReceipt:
    require_exact_draft_release(
        refreshed_release,
        expected_release=plan.release,
        expected_assets=plan.assets,
    )
    ordered = _validated_results(plan, results, contract)
    return HostedValidationReceipt(
        repository=plan.repository,
        source_run=plan.source_run,
        validation_run=plan.validation_run,
        release=plan.release,
        identity_sha1=plan.identity_sha1,
        assets=plan.assets,
        results=ordered,
    )


def validate_receipt_policy(
    receipt: HostedValidationReceipt,
    contract: ReleaseContract,
) -> None:
    plan = HostedValidationPlan(
        repository=receipt.repository,
        source_run=receipt.source_run,
        validation_run=receipt.validation_run,
        release=receipt.release,
        identity_sha1=receipt.identity_sha1,
        assets=receipt.assets,
        validations=validation_specs(contract, receipt.release.tag, receipt.assets),
    )
    if (
        receipt.source_run.tag != receipt.validation_run.tag
        or receipt.source_run.head_sha != receipt.validation_run.head_sha
        or receipt.release.tag != receipt.source_run.tag
        or receipt.release.name != f"App Icon Toolkit {receipt.release.tag}"
    ):
        raise HostedValidationError("hosted validation receipt bindings disagree")
    expected_names = {
        *(target.release_filename(receipt.release.tag) for target in contract.targets),
        CHECKSUM_ASSET_NAME,
    }
    if {asset.name for asset in receipt.assets} != expected_names:
        raise HostedValidationError("hosted validation receipt asset set is not exact")
    _validated_results(plan, receipt.results, contract)


def bind_receipt(
    receipt: HostedValidationReceipt,
    *,
    repository: str,
    source_run: WorkflowRun,
    validation_run: WorkflowRun,
    release_id: str,
    release_database_id: int,
    release_body: str,
    identity_sha1: str,
    local_assets: Sequence[LocalAsset],
) -> None:
    """Bind parsed hosted evidence to one local finalization attempt."""

    try:
        repository = validate_repository(repository)
    except RuntimeError as error:
        raise HostedValidationError(str(error)) from error
    identity_sha1 = _validated_identity(identity_sha1)
    if receipt.repository != repository:
        raise HostedValidationError("hosted receipt repository differs from the attempt")
    if receipt.source_run != source_run:
        raise HostedValidationError("hosted receipt source run differs from the attempt")
    if receipt.validation_run != validation_run:
        raise HostedValidationError("hosted receipt validation run differs from the bound run")
    if (
        receipt.release.release_id != release_id
        or receipt.release.release_database_id != release_database_id
    ):
        raise HostedValidationError("hosted receipt release identity differs from the draft")
    if not isinstance(release_body, str) or not release_body:
        raise HostedValidationError("expected release body must be non-empty text")
    try:
        body_sha256 = hashlib.sha256(
            release_body.encode("utf-8", errors="strict")
        ).hexdigest()
    except UnicodeError as error:
        raise HostedValidationError("expected release body is not valid UTF-8") from error
    if receipt.release.body_sha256 != body_sha256:
        raise HostedValidationError("hosted receipt release body differs from the draft")
    if receipt.identity_sha1 != identity_sha1:
        raise HostedValidationError("hosted receipt signing identity differs from the attempt")
    expected_assets = {asset.name: (asset.size, asset.sha256) for asset in local_assets}
    if len(expected_assets) != len(local_assets):
        raise HostedValidationError("local release asset set contains duplicate names")
    actual_assets = {asset.name: (asset.size, asset.sha256) for asset in receipt.assets}
    if actual_assets != expected_assets:
        raise HostedValidationError("hosted receipt asset digests differ from local assets")
