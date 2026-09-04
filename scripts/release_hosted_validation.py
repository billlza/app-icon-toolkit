"""Strict contracts for secret-free hosted validation of signed draft assets.

The local signing finalizer and the GitHub-hosted validator exchange only the
bounded JSON values defined here.  SHA-256 values detect byte replacement or
accidental corruption; trust in a receipt comes from binding it to one exact,
successful GitHub Actions run, not from treating the JSON as a signature.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import re
from typing import Any, Mapping

import release_draft
from release_draft import WorkflowRun
from release_artifacts import (
    ArtifactRecord,
    ReleaseArtifactError,
    parse_artifact_inventory,
)
from release_hosted_validation_model import (
    ARCHITECTURES,
    CHECKSUM_ASSET_NAME,
    EXPECTED_WORKFLOW_EVENT,
    EXPECTED_WORKFLOW_NAME,
    EXPECTED_WORKFLOW_PATH,
    MAX_ASSETS,
    MAX_VALIDATIONS,
    SCHEMA_VERSION,
    DraftRelease,
    DraftReleaseAsset,
    HostedValidationError,
    HostedValidationPlan,
    HostedValidationReceipt,
    HostedWorkflowIdentity,
    ReceiptRelease,
    ValidationResult,
    ValidationSpec,
    bind_receipt,
    create_bound_receipt,
    create_plan,
    require_exact_draft_release,
    validate_receipt_policy,
    validation_specs,
)
from release_targets import (
    ReleaseContract,
    validate_commit_sha,
    validate_release_tag,
    validate_repository,
)


MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_FIELD_CHARS = 512
MAX_RELEASE_BODY_BYTES = 256 * 1024

SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHA1_IDENTITY = re.compile(r"^[0-9A-F]{40}$")

RUN_FIELDS = frozenset({"workflow_id", "run_id", "attempt", "tag", "head_sha"})
RELEASE_FIELDS = frozenset(
    {
        "release_id",
        "release_database_id",
        "tag",
        "name",
        "body_sha256",
        "is_draft",
        "is_prerelease",
    }
)
ASSET_FIELDS = frozenset({"asset_id", "name", "size", "sha256"})
SPEC_FIELDS = frozenset(
    {
        "validation_id",
        "target_id",
        "runner",
        "runtime_architecture",
        "archive_asset_id",
        "archive_name",
        "archive_size",
        "archive_sha256",
        "archive_format",
        "binary_name",
        "expected_architectures",
    }
)
RESULT_FIELDS = frozenset(
    {
        *SPEC_FIELDS,
        "binary_sha256",
        "identity_sha1",
        "identifier",
        "team_id",
        "architectures",
        "signature_valid",
        "notarization_ticket_valid",
        "mcp_smoke_valid",
    }
)
PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "source_run",
        "validation_run",
        "release",
        "identity_sha1",
        "assets",
        "validations",
    }
)
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "source_run",
        "validation_run",
        "release",
        "identity_sha1",
        "assets",
        "results",
    }
)


def _load_json_object(payload: str, context: str) -> dict[str, Any]:
    if not isinstance(payload, str):
        raise HostedValidationError(f"{context} JSON must be text")
    try:
        size = len(payload.encode("utf-8", errors="strict"))
    except UnicodeError as error:
        raise HostedValidationError(f"{context} JSON is not valid UTF-8") from error
    if size <= 0 or size > MAX_JSON_BYTES:
        raise HostedValidationError(
            f"{context} JSON size {size} is outside the allowed range"
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise HostedValidationError(f"{context} repeats JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise HostedValidationError(
            f"{context} contains non-standard JSON constant {value!r}"
        )

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except HostedValidationError:
        raise
    except (json.JSONDecodeError, RecursionError) as error:
        raise HostedValidationError(f"{context} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise HostedValidationError(f"{context} JSON root must be an object")
    return value


def _object(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HostedValidationError(f"{context} must be an object")
    return value


def _exact_fields(value: Mapping[str, object], fields: frozenset[str], context: str) -> None:
    actual = set(value)
    if actual != fields:
        raise HostedValidationError(
            f"{context} fields differ from the contract; "
            f"missing={sorted(fields - actual)}; extra={sorted(actual - fields)}"
        )


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_FIELD_CHARS:
        raise HostedValidationError(f"{context} must be a bounded non-empty string")
    return value


def _positive_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HostedValidationError(f"{context} must be a positive integer")
    return value


def _boolean(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise HostedValidationError(f"{context} must be a boolean")
    return value


def _sha256(value: object, context: str) -> str:
    digest = _string(value, context)
    if SHA256.fullmatch(digest) is None:
        raise HostedValidationError(f"{context} must be an exact lowercase SHA-256")
    return digest


def _identity_sha1(value: object, context: str) -> str:
    identity = _string(value, context)
    if SHA1_IDENTITY.fullmatch(identity) is None:
        raise HostedValidationError(
            f"{context} must be an exact uppercase SHA-1 identity fingerprint"
        )
    return identity


def _architecture(value: object, context: str) -> str:
    architecture = _string(value, context)
    if architecture not in ARCHITECTURES:
        raise HostedValidationError(f"{context} is not an approved macOS architecture")
    return architecture


def _architectures(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise HostedValidationError(f"{context} must be a non-empty array")
    architectures = tuple(_architecture(item, context) for item in value)
    if len(set(architectures)) != len(architectures) or architectures != tuple(
        sorted(architectures)
    ):
        raise HostedValidationError(f"{context} must be unique and sorted")
    return architectures


def _validated_repository(value: object) -> str:
    repository = _string(value, "repository")
    try:
        return validate_repository(repository)
    except RuntimeError as error:
        raise HostedValidationError(str(error)) from error


def _validated_tag(value: object, context: str) -> str:
    tag = _string(value, context)
    try:
        return validate_release_tag(tag)
    except RuntimeError as error:
        raise HostedValidationError(str(error)) from error


def _validated_sha(value: object, context: str) -> str:
    sha = _string(value, context)
    try:
        return validate_commit_sha(sha)
    except RuntimeError as error:
        raise HostedValidationError(str(error)) from error


def _run(value: object, context: str) -> WorkflowRun:
    raw = _object(value, context)
    _exact_fields(raw, RUN_FIELDS, context)
    return WorkflowRun(
        workflow_id=_positive_int(raw["workflow_id"], f"{context}.workflow_id"),
        run_id=_positive_int(raw["run_id"], f"{context}.run_id"),
        attempt=_positive_int(raw["attempt"], f"{context}.attempt"),
        tag=_validated_tag(raw["tag"], f"{context}.tag"),
        head_sha=_validated_sha(raw["head_sha"], f"{context}.head_sha"),
    )


def _release(value: object, context: str) -> ReceiptRelease:
    raw = _object(value, context)
    _exact_fields(raw, RELEASE_FIELDS, context)
    release = ReceiptRelease(
        release_id=_string(raw["release_id"], f"{context}.release_id"),
        release_database_id=_positive_int(
            raw["release_database_id"], f"{context}.release_database_id"
        ),
        tag=_validated_tag(raw["tag"], f"{context}.tag"),
        name=_string(raw["name"], f"{context}.name"),
        body_sha256=_sha256(raw["body_sha256"], f"{context}.body_sha256"),
        is_draft=_boolean(raw["is_draft"], f"{context}.is_draft"),
        is_prerelease=_boolean(
            raw["is_prerelease"], f"{context}.is_prerelease"
        ),
    )
    if not release.is_draft or release.is_prerelease:
        raise HostedValidationError(f"{context} must identify a stable unpublished draft")
    return release


def _asset(value: object, context: str) -> DraftReleaseAsset:
    raw = _object(value, context)
    _exact_fields(raw, ASSET_FIELDS, context)
    return DraftReleaseAsset(
        asset_id=_positive_int(raw["asset_id"], f"{context}.asset_id"),
        name=_string(raw["name"], f"{context}.name"),
        size=_positive_int(raw["size"], f"{context}.size"),
        sha256=_sha256(raw["sha256"], f"{context}.sha256"),
    )


def _assets(value: object, context: str) -> tuple[DraftReleaseAsset, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_ASSETS:
        raise HostedValidationError(f"{context} must be a bounded non-empty array")
    assets = tuple(_asset(item, f"{context}[{index}]") for index, item in enumerate(value))
    names = [asset.name for asset in assets]
    identifiers = [asset.asset_id for asset in assets]
    if len(set(names)) != len(names) or len(set(identifiers)) != len(identifiers):
        raise HostedValidationError(f"{context} contains duplicate names or IDs")
    if assets != tuple(sorted(assets, key=lambda asset: asset.name)):
        raise HostedValidationError(f"{context} must be sorted by name")
    return assets


def _spec(value: object, context: str) -> ValidationSpec:
    raw = _object(value, context)
    _exact_fields(raw, SPEC_FIELDS, context)
    archive_format = _string(raw["archive_format"], f"{context}.archive_format")
    if archive_format != "zip":
        raise HostedValidationError(f"{context}.archive_format must be zip")
    return ValidationSpec(
        validation_id=_string(raw["validation_id"], f"{context}.validation_id"),
        target_id=_string(raw["target_id"], f"{context}.target_id"),
        runner=_string(raw["runner"], f"{context}.runner"),
        runtime_architecture=_architecture(
            raw["runtime_architecture"], f"{context}.runtime_architecture"
        ),
        archive_asset_id=_positive_int(
            raw["archive_asset_id"], f"{context}.archive_asset_id"
        ),
        archive_name=_string(raw["archive_name"], f"{context}.archive_name"),
        archive_size=_positive_int(raw["archive_size"], f"{context}.archive_size"),
        archive_sha256=_sha256(
            raw["archive_sha256"], f"{context}.archive_sha256"
        ),
        archive_format=archive_format,
        binary_name=_string(raw["binary_name"], f"{context}.binary_name"),
        expected_architectures=_architectures(
            raw["expected_architectures"], f"{context}.expected_architectures"
        ),
    )


def _specs(value: object, context: str) -> tuple[ValidationSpec, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_VALIDATIONS:
        raise HostedValidationError(f"{context} must be a bounded non-empty array")
    specs = tuple(_spec(item, f"{context}[{index}]") for index, item in enumerate(value))
    identifiers = [spec.validation_id for spec in specs]
    if len(set(identifiers)) != len(identifiers):
        raise HostedValidationError(f"{context} contains duplicate validation IDs")
    if specs != tuple(sorted(specs, key=lambda spec: spec.validation_id)):
        raise HostedValidationError(f"{context} must be sorted by validation ID")
    return specs


def _result(value: object, context: str) -> ValidationResult:
    raw = _object(value, context)
    _exact_fields(raw, RESULT_FIELDS, context)
    spec = _spec({field: raw[field] for field in SPEC_FIELDS}, context)
    result = ValidationResult(
        **asdict(spec),
        binary_sha256=_sha256(raw["binary_sha256"], f"{context}.binary_sha256"),
        identity_sha1=_identity_sha1(
            raw["identity_sha1"], f"{context}.identity_sha1"
        ),
        identifier=_string(raw["identifier"], f"{context}.identifier"),
        team_id=_string(raw["team_id"], f"{context}.team_id"),
        architectures=_architectures(raw["architectures"], f"{context}.architectures"),
        signature_valid=_boolean(
            raw["signature_valid"], f"{context}.signature_valid"
        ),
        notarization_ticket_valid=_boolean(
            raw["notarization_ticket_valid"],
            f"{context}.notarization_ticket_valid",
        ),
        mcp_smoke_valid=_boolean(
            raw["mcp_smoke_valid"], f"{context}.mcp_smoke_valid"
        ),
    )
    if not (
        result.signature_valid
        and result.notarization_ticket_valid
        and result.mcp_smoke_valid
    ):
        raise HostedValidationError(f"{context} contains an unsuccessful validation")
    return result


def _results(value: object, context: str) -> tuple[ValidationResult, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_VALIDATIONS:
        raise HostedValidationError(f"{context} must be a bounded non-empty array")
    results = tuple(
        _result(item, f"{context}[{index}]") for index, item in enumerate(value)
    )
    identifiers = [result.validation_id for result in results]
    if len(set(identifiers)) != len(identifiers):
        raise HostedValidationError(f"{context} contains duplicate validation IDs")
    if results != tuple(sorted(results, key=lambda result: result.validation_id)):
        raise HostedValidationError(f"{context} must be sorted by validation ID")
    return results


def parse_draft_release(payload: str, *, expected_tag: str) -> DraftRelease:
    """Parse the critical fields from one GitHub REST release response."""

    expected_tag = _validated_tag(expected_tag, "expected release tag")
    raw = _load_json_object(payload, "GitHub draft release")
    required = {
        "id",
        "node_id",
        "tag_name",
        "name",
        "body",
        "draft",
        "prerelease",
        "assets",
    }
    missing = required - set(raw)
    if missing:
        raise HostedValidationError(
            f"GitHub draft release is missing fields: {sorted(missing)}"
        )
    raw_assets = raw["assets"]
    if not isinstance(raw_assets, list) or not raw_assets or len(raw_assets) > MAX_ASSETS:
        raise HostedValidationError("GitHub draft release assets are outside the size limit")
    assets: list[DraftReleaseAsset] = []
    for index, value in enumerate(raw_assets):
        context = f"GitHub draft release assets[{index}]"
        asset = _object(value, context)
        required_asset_fields = {"id", "name", "size", "digest", "state"}
        missing_asset_fields = required_asset_fields - set(asset)
        if missing_asset_fields:
            raise HostedValidationError(
                f"{context} is missing fields: {sorted(missing_asset_fields)}"
            )
        digest = _string(asset["digest"], f"{context}.digest")
        if not digest.startswith("sha256:"):
            raise HostedValidationError(f"{context}.digest is not SHA-256")
        if asset["state"] != "uploaded":
            raise HostedValidationError(f"{context}.state is not uploaded")
        assets.append(
            DraftReleaseAsset(
                asset_id=_positive_int(asset["id"], f"{context}.id"),
                name=_string(asset["name"], f"{context}.name"),
                size=_positive_int(asset["size"], f"{context}.size"),
                sha256=_sha256(digest[7:], f"{context}.digest"),
            )
        )
    assets.sort(key=lambda asset: asset.name)
    names = [asset.name for asset in assets]
    identifiers = [asset.asset_id for asset in assets]
    if len(set(names)) != len(names) or len(set(identifiers)) != len(identifiers):
        raise HostedValidationError("GitHub draft release has duplicate asset names or IDs")
    tag = _validated_tag(raw["tag_name"], "GitHub draft release tag_name")
    if tag != expected_tag:
        raise HostedValidationError("GitHub draft release tag differs from the expected tag")
    body = raw["body"]
    if not isinstance(body, str) or not body:
        raise HostedValidationError("GitHub draft release body must be non-empty text")
    try:
        body_size = len(body.encode("utf-8", errors="strict"))
    except UnicodeError as error:
        raise HostedValidationError(
            "GitHub draft release body is not valid UTF-8 text"
        ) from error
    if body_size > MAX_RELEASE_BODY_BYTES:
        raise HostedValidationError("GitHub draft release body exceeds the size limit")
    release = DraftRelease(
        release_id=_string(raw["node_id"], "GitHub draft release node_id"),
        release_database_id=_positive_int(raw["id"], "GitHub draft release id"),
        tag=tag,
        name=_string(raw["name"], "GitHub draft release name"),
        body=body,
        is_draft=_boolean(raw["draft"], "GitHub draft release draft"),
        is_prerelease=_boolean(
            raw["prerelease"], "GitHub draft release prerelease"
        ),
        assets=tuple(assets),
    )
    if not release.is_draft or release.is_prerelease:
        raise HostedValidationError("GitHub release must be a stable unpublished draft")
    return release


def plan_payload(plan: HostedValidationPlan) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": plan.repository,
        "source_run": asdict(plan.source_run),
        "validation_run": asdict(plan.validation_run),
        "release": asdict(plan.release),
        "identity_sha1": plan.identity_sha1,
        "assets": [asdict(asset) for asset in plan.assets],
        "validations": [asdict(spec) for spec in plan.validations],
    }


def receipt_payload(receipt: HostedValidationReceipt) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": receipt.repository,
        "source_run": asdict(receipt.source_run),
        "validation_run": asdict(receipt.validation_run),
        "release": asdict(receipt.release),
        "identity_sha1": receipt.identity_sha1,
        "assets": [asdict(asset) for asset in receipt.assets],
        "results": [asdict(result) for result in receipt.results],
    }


def canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def parse_plan(payload: str, *, contract: ReleaseContract) -> HostedValidationPlan:
    raw = _load_json_object(payload, "hosted validation plan")
    _exact_fields(raw, PLAN_FIELDS, "hosted validation plan")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise HostedValidationError("hosted validation plan schema_version is unsupported")
    plan = HostedValidationPlan(
        repository=_validated_repository(raw["repository"]),
        source_run=_run(raw["source_run"], "source_run"),
        validation_run=_run(raw["validation_run"], "validation_run"),
        release=_release(raw["release"], "release"),
        identity_sha1=_identity_sha1(raw["identity_sha1"], "identity_sha1"),
        assets=_assets(raw["assets"], "assets"),
        validations=_specs(raw["validations"], "validations"),
    )
    if (
        plan.source_run.tag != plan.validation_run.tag
        or plan.source_run.head_sha != plan.validation_run.head_sha
        or plan.release.tag != plan.source_run.tag
        or plan.release.name != f"App Icon Toolkit {plan.release.tag}"
    ):
        raise HostedValidationError("hosted validation plan bindings disagree")
    expected_names = {
        *(target.release_filename(plan.release.tag) for target in contract.targets),
        CHECKSUM_ASSET_NAME,
    }
    if {asset.name for asset in plan.assets} != expected_names:
        raise HostedValidationError("hosted validation plan asset set is not exact")
    expected_specs = validation_specs(contract, plan.release.tag, plan.assets)
    if plan.validations != expected_specs:
        raise HostedValidationError(
            "hosted validation plan jobs differ from the release target contract"
        )
    return plan


def parse_validation_result(payload: str) -> ValidationResult:
    raw = _load_json_object(payload, "hosted validation result")
    return _result(raw, "hosted validation result")


def parse_receipt(payload: str, *, contract: ReleaseContract) -> HostedValidationReceipt:
    raw = _load_json_object(payload, "hosted validation receipt")
    _exact_fields(raw, RECEIPT_FIELDS, "hosted validation receipt")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise HostedValidationError("hosted validation receipt schema_version is unsupported")
    receipt = HostedValidationReceipt(
        repository=_validated_repository(raw["repository"]),
        source_run=_run(raw["source_run"], "source_run"),
        validation_run=_run(raw["validation_run"], "validation_run"),
        release=_release(raw["release"], "release"),
        identity_sha1=_identity_sha1(raw["identity_sha1"], "identity_sha1"),
        assets=_assets(raw["assets"], "assets"),
        results=_results(raw["results"], "results"),
    )
    validate_receipt_policy(receipt, contract)
    return receipt


def parse_hosted_workflow(
    payload: str,
    *,
    expected_workflow_id: int,
) -> HostedWorkflowIdentity:
    """Validate the canonical active workflow selected by its repository path."""

    try:
        workflow = release_draft.parse_workflow_identity(
            payload,
            expected_workflow_id=expected_workflow_id,
            expected_name=EXPECTED_WORKFLOW_NAME,
            expected_path=EXPECTED_WORKFLOW_PATH,
        )
    except release_draft.ReleaseDraftError as error:
        raise HostedValidationError(
            f"hosted validation workflow binding failed: {error}"
        ) from error
    return HostedWorkflowIdentity(
        workflow_id=workflow.workflow_id,
        name=workflow.name,
        path=workflow.path,
        state=workflow.state,
    )


def parse_successful_validation_run(
    payload: str,
    *,
    expected_workflow_id: int,
    expected_run_id: int,
    expected_attempt: int,
    expected_tag: str,
    expected_head_sha: str,
) -> WorkflowRun:
    """Validate the exact completed hosted workflow run that vouches for a receipt."""

    raw = _load_json_object(payload, "hosted validation workflow run")
    required = {
        "workflowDatabaseId",
        "databaseId",
        "attempt",
        "headBranch",
        "headSha",
        "workflowName",
        "event",
        "status",
        "conclusion",
    }
    _exact_fields(raw, frozenset(required), "hosted validation workflow run")
    run = WorkflowRun(
        workflow_id=_positive_int(
            raw["workflowDatabaseId"], "hosted workflowDatabaseId"
        ),
        run_id=_positive_int(raw["databaseId"], "hosted databaseId"),
        attempt=_positive_int(raw["attempt"], "hosted attempt"),
        tag=_validated_tag(raw["headBranch"], "hosted headBranch"),
        head_sha=_validated_sha(raw["headSha"], "hosted headSha"),
    )
    expected = WorkflowRun(
        workflow_id=_positive_int(expected_workflow_id, "expected workflow id"),
        run_id=_positive_int(expected_run_id, "expected run id"),
        attempt=_positive_int(expected_attempt, "expected run attempt"),
        tag=_validated_tag(expected_tag, "expected run tag"),
        head_sha=_validated_sha(expected_head_sha, "expected run head SHA"),
    )
    if run != expected:
        raise HostedValidationError("hosted validation workflow run identity differs")
    if raw["workflowName"] != EXPECTED_WORKFLOW_NAME:
        raise HostedValidationError("hosted validation workflow name differs")
    if raw["event"] != EXPECTED_WORKFLOW_EVENT:
        raise HostedValidationError("hosted validation workflow event differs")
    if raw["status"] != "completed" or raw["conclusion"] != "success":
        raise HostedValidationError("hosted validation workflow did not complete successfully")
    return run


def receipt_artifact_name(run: WorkflowRun) -> str:
    """Return the unique receipt artifact name for one hosted run attempt."""

    if (
        isinstance(run.run_id, bool)
        or not isinstance(run.run_id, int)
        or run.run_id <= 0
        or isinstance(run.attempt, bool)
        or not isinstance(run.attempt, int)
        or run.attempt <= 0
    ):
        raise HostedValidationError("hosted validation run identity is invalid")
    return (
        f"hosted-validation-receipt-run-{run.run_id}-attempt-{run.attempt}"
    )


def parse_receipt_artifact(
    payload: str,
    *,
    run: WorkflowRun,
    expected_artifact_id: int,
) -> ArtifactRecord:
    """Bind one numeric receipt artifact response to a hosted run and API digest."""

    if (
        isinstance(expected_artifact_id, bool)
        or not isinstance(expected_artifact_id, int)
        or expected_artifact_id <= 0
    ):
        raise HostedValidationError("expected receipt artifact ID must be positive")
    raw = _load_json_object(payload, "hosted validation receipt artifact")
    try:
        records = parse_artifact_inventory(
            json.dumps(
                {"total_count": 1, "artifacts": [raw]},
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            run=run,
            expected_names=(receipt_artifact_name(run),),
        )
    except (ReleaseArtifactError, TypeError, ValueError) as error:
        raise HostedValidationError(
            f"hosted validation receipt artifact is invalid: {error}"
        ) from error
    record = records[0]
    if record.artifact_id != expected_artifact_id:
        raise HostedValidationError(
            "hosted validation receipt artifact ID differs from the bound ID"
        )
    return record
