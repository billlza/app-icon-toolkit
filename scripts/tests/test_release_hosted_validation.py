from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
import sys

if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_draft
import release_hosted_validation as hosted
import release_targets


REPOSITORY = "example/app-icon-toolkit"
TAG = "v1.2.3"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
IDENTITY = "2DA7764ED42B213AE04925B6261238B24C758FE1"
NOTES = "# Release notes\n\nVerified changes.\n"


class HostedValidationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = release_targets.load_contract()
        self.source_run = release_draft.WorkflowRun(
            workflow_id=100,
            run_id=101,
            attempt=2,
            tag=TAG,
            head_sha=HEAD_SHA,
        )
        self.validation_run = release_draft.WorkflowRun(
            workflow_id=200,
            run_id=201,
            attempt=1,
            tag=TAG,
            head_sha=HEAD_SHA,
        )
        self.release = self.release_fixture()
        self.plan = hosted.create_plan(
            repository=REPOSITORY,
            source_run=self.source_run,
            validation_run=self.validation_run,
            release=self.release,
            release_notes=NOTES,
            identity_sha1=IDENTITY,
            contract=self.contract,
        )

    def release_fixture(self) -> hosted.DraftRelease:
        names = sorted(
            {
                *(target.release_filename(TAG) for target in self.contract.targets),
                hosted.CHECKSUM_ASSET_NAME,
            }
        )
        return hosted.DraftRelease(
            release_id="RE_kwDOExample",
            release_database_id=300,
            tag=TAG,
            name=f"App Icon Toolkit {TAG}",
            body=NOTES,
            is_draft=True,
            is_prerelease=False,
            assets=tuple(
                hosted.DraftReleaseAsset(
                    asset_id=400 + index,
                    name=name,
                    size=1000 + index,
                    sha256=f"{index + 1:064x}",
                )
                for index, name in enumerate(names)
            ),
        )

    def result(self, spec: hosted.ValidationSpec) -> hosted.ValidationResult:
        return hosted.ValidationResult(
            **asdict(spec),
            binary_sha256="f" * 64,
            identity_sha1=IDENTITY,
            identifier=self.contract.macos_signing.code_identifier,
            team_id=self.contract.macos_signing.team_id,
            architectures=spec.expected_architectures,
            signature_valid=True,
            notarization_ticket_valid=True,
            mcp_smoke_valid=True,
        )

    def test_plan_derives_four_native_jobs_from_shared_target_contract(self) -> None:
        self.assertEqual(len(self.plan.validations), 4)
        observed = {
            (spec.target_id, spec.runtime_architecture, spec.runner)
            for spec in self.plan.validations
        }
        self.assertEqual(
            observed,
            {
                ("aarch64-apple-darwin", "arm64", "macos-15"),
                ("x86_64-apple-darwin", "x86_64", "macos-15-intel"),
                ("universal2-apple-darwin", "arm64", "macos-15"),
                ("universal2-apple-darwin", "x86_64", "macos-15-intel"),
            },
        )
        for spec in self.plan.validations:
            asset = next(
                candidate
                for candidate in self.plan.assets
                if candidate.name == spec.archive_name
            )
            self.assertEqual(spec.archive_asset_id, asset.asset_id)
            self.assertEqual(spec.archive_sha256, asset.sha256)

    def test_plan_round_trip_is_strict_and_duplicate_keys_fail_closed(self) -> None:
        payload = hosted.canonical_json(hosted.plan_payload(self.plan))
        self.assertEqual(
            hosted.parse_plan(payload, contract=self.contract),
            self.plan,
        )
        duplicated = payload.replace(
            '"schema_version":1',
            '"schema_version":1,"schema_version":1',
            1,
        )
        with self.assertRaisesRegex(hosted.HostedValidationError, "repeats JSON key"):
            hosted.parse_plan(duplicated, contract=self.contract)

        value = hosted.plan_payload(self.plan)
        value["unexpected"] = True
        with self.assertRaisesRegex(hosted.HostedValidationError, "extra=.*unexpected"):
            hosted.parse_plan(hosted.canonical_json(value), contract=self.contract)

    def test_plan_rejects_matrix_or_asset_digest_substitution(self) -> None:
        value = hosted.plan_payload(self.plan)
        validations = value["validations"]
        self.assertIsInstance(validations, list)
        validations[0]["runner"] = "macos-latest"
        with self.assertRaisesRegex(hosted.HostedValidationError, "target contract"):
            hosted.parse_plan(hosted.canonical_json(value), contract=self.contract)

        value = hosted.plan_payload(self.plan)
        assets = value["assets"]
        validations = value["validations"]
        self.assertIsInstance(assets, list)
        self.assertIsInstance(validations, list)
        target_name = validations[0]["archive_name"]
        for asset in assets:
            if asset["name"] == target_name:
                asset["sha256"] = "e" * 64
                break
        with self.assertRaisesRegex(hosted.HostedValidationError, "target contract"):
            hosted.parse_plan(hosted.canonical_json(value), contract=self.contract)

    def test_receipt_round_trip_requires_all_successful_exact_results(self) -> None:
        results = tuple(self.result(spec) for spec in self.plan.validations)
        receipt = hosted.create_bound_receipt(
            self.plan,
            refreshed_release=self.release,
            results=results,
            contract=self.contract,
        )
        payload = hosted.canonical_json(hosted.receipt_payload(receipt))
        parsed = hosted.parse_receipt(payload, contract=self.contract)
        self.assertEqual(parsed, receipt)

        failed = replace(results[0], mcp_smoke_valid=False)
        with self.assertRaisesRegex(hosted.HostedValidationError, "not successful"):
            hosted.create_bound_receipt(
                self.plan,
                refreshed_release=self.release,
                results=(failed, *results[1:]),
                contract=self.contract,
            )

        with self.assertRaisesRegex(hosted.HostedValidationError, "exact plan"):
            hosted.create_bound_receipt(
                self.plan,
                refreshed_release=self.release,
                results=results[:-1],
                contract=self.contract,
            )

    def test_exact_draft_binding_rejects_replaced_numeric_asset_identity(self) -> None:
        receipt = hosted.create_bound_receipt(
            self.plan,
            refreshed_release=self.release,
            results=tuple(self.result(spec) for spec in self.plan.validations),
            contract=self.contract,
        )
        hosted.require_exact_draft_release(
            self.release,
            expected_release=receipt.release,
            expected_assets=receipt.assets,
        )
        replaced_asset = replace(
            self.release.assets[0],
            asset_id=self.release.assets[0].asset_id + 10_000,
        )
        replaced = replace(
            self.release,
            assets=(replaced_asset, *self.release.assets[1:]),
        )
        with self.assertRaisesRegex(hosted.HostedValidationError, "assets changed"):
            hosted.require_exact_draft_release(
                replaced,
                expected_release=receipt.release,
                expected_assets=receipt.assets,
            )

    def test_receipt_binding_requires_exact_run_release_and_local_digests(self) -> None:
        receipt = hosted.create_bound_receipt(
            self.plan,
            refreshed_release=self.release,
            results=tuple(self.result(spec) for spec in self.plan.validations),
            contract=self.contract,
        )
        with tempfile.TemporaryDirectory(prefix="hosted-binding-") as temporary:
            root = Path(temporary)
            local_assets = []
            for asset in receipt.assets:
                path = root / asset.name
                path.write_bytes(b"x")
                local_assets.append(
                    release_draft.LocalAsset(
                        name=asset.name,
                        path=path,
                        size=asset.size,
                        sha256=asset.sha256,
                    )
                )
            hosted.bind_receipt(
                receipt,
                repository=REPOSITORY,
                source_run=self.source_run,
                validation_run=self.validation_run,
                release_id=self.release.release_id,
                release_database_id=self.release.release_database_id,
                release_body=NOTES,
                identity_sha1=IDENTITY,
                local_assets=local_assets,
            )
            changed = replace(local_assets[0], sha256="d" * 64)
            with self.assertRaisesRegex(hosted.HostedValidationError, "asset digests"):
                hosted.bind_receipt(
                    receipt,
                    repository=REPOSITORY,
                    source_run=self.source_run,
                    validation_run=self.validation_run,
                    release_id=self.release.release_id,
                    release_database_id=self.release.release_database_id,
                    release_body=NOTES,
                    identity_sha1=IDENTITY,
                    local_assets=(changed, *local_assets[1:]),
                )

    def test_live_release_parser_binds_numeric_asset_ids_and_rejects_duplicates(self) -> None:
        raw_assets = [
            {
                "id": asset.asset_id,
                "name": asset.name,
                "size": asset.size,
                "digest": f"sha256:{asset.sha256}",
                "state": "uploaded",
                "extra_api_field": "ignored",
            }
            for asset in self.release.assets
        ]
        value = {
            "id": self.release.release_database_id,
            "node_id": self.release.release_id,
            "tag_name": TAG,
            "name": self.release.name,
            "body": NOTES,
            "draft": True,
            "prerelease": False,
            "assets": raw_assets,
            "url": "https://api.github.invalid/release",
        }
        parsed = hosted.parse_draft_release(json.dumps(value), expected_tag=TAG)
        self.assertEqual(parsed, self.release)

        raw_assets[1]["id"] = raw_assets[0]["id"]
        with self.assertRaisesRegex(hosted.HostedValidationError, "duplicate"):
            hosted.parse_draft_release(json.dumps(value), expected_tag=TAG)

        raw_assets[1]["id"] = self.release.assets[1].asset_id
        value["body"] = "\ud800"
        with self.assertRaisesRegex(hosted.HostedValidationError, "valid UTF-8"):
            hosted.parse_draft_release(json.dumps(value), expected_tag=TAG)

    def test_successful_run_parser_rejects_wrong_event_or_incomplete_run(self) -> None:
        value = {
            "workflowDatabaseId": self.validation_run.workflow_id,
            "databaseId": self.validation_run.run_id,
            "attempt": self.validation_run.attempt,
            "headBranch": TAG,
            "headSha": HEAD_SHA,
            "workflowName": hosted.EXPECTED_WORKFLOW_NAME,
            "event": hosted.EXPECTED_WORKFLOW_EVENT,
            "status": "completed",
            "conclusion": "success",
        }
        parsed = hosted.parse_successful_validation_run(
            json.dumps(value),
            expected_workflow_id=self.validation_run.workflow_id,
            expected_run_id=self.validation_run.run_id,
            expected_attempt=self.validation_run.attempt,
            expected_tag=TAG,
            expected_head_sha=HEAD_SHA,
        )
        self.assertEqual(parsed, self.validation_run)

        for field, bad_value in (("event", "push"), ("status", "in_progress")):
            with self.subTest(field=field):
                changed = dict(value)
                changed[field] = bad_value
                with self.assertRaises(hosted.HostedValidationError):
                    hosted.parse_successful_validation_run(
                        json.dumps(changed),
                        expected_workflow_id=self.validation_run.workflow_id,
                        expected_run_id=self.validation_run.run_id,
                        expected_attempt=self.validation_run.attempt,
                        expected_tag=TAG,
                        expected_head_sha=HEAD_SHA,
                    )

    def test_canonical_hosted_workflow_binds_id_name_path_and_active_state(self) -> None:
        value = {
            "id": self.validation_run.workflow_id,
            "name": hosted.EXPECTED_WORKFLOW_NAME,
            "path": ".github/workflows/validate-signed-draft.yml",
            "state": "active",
            "html_url": "https://github.invalid/workflow",
        }
        parsed = hosted.parse_hosted_workflow(
            json.dumps(value),
            expected_workflow_id=self.validation_run.workflow_id,
        )
        self.assertEqual(parsed.workflow_id, self.validation_run.workflow_id)

        cases = (
            ("id", self.validation_run.workflow_id + 1, "ID"),
            ("name", "Different workflow", "name"),
            ("path", ".github/workflows/replaced.yml", "path"),
            ("state", "disabled_manually", "not active"),
        )
        for field, replacement, message in cases:
            with self.subTest(field=field):
                changed = dict(value)
                changed[field] = replacement
                with self.assertRaisesRegex(hosted.HostedValidationError, message):
                    hosted.parse_hosted_workflow(
                        json.dumps(changed),
                        expected_workflow_id=self.validation_run.workflow_id,
                    )

        duplicate = json.dumps(value).replace(
            f'"id": {self.validation_run.workflow_id}',
            (
                f'"id": {self.validation_run.workflow_id}, '
                f'"id": {self.validation_run.workflow_id}'
            ),
            1,
        )
        with self.assertRaisesRegex(hosted.HostedValidationError, "repeats JSON key"):
            hosted.parse_hosted_workflow(
                duplicate,
                expected_workflow_id=self.validation_run.workflow_id,
            )

    def test_receipt_artifact_is_bound_by_numeric_id_digest_and_attempt_name(self) -> None:
        value = {
            "id": 900,
            "name": hosted.receipt_artifact_name(self.validation_run),
            "size_in_bytes": 1234,
            "digest": f"sha256:{'a' * 64}",
            "expired": False,
            "created_at": "2026-09-04T00:00:00Z",
            "updated_at": "2026-09-04T00:01:00Z",
            "workflow_run": {
                "id": self.validation_run.run_id,
                "head_sha": self.validation_run.head_sha,
                "head_branch": self.validation_run.tag,
                "repository_id": 99,
                "head_repository_id": 99,
            },
        }
        record = hosted.parse_receipt_artifact(
            json.dumps(value),
            run=self.validation_run,
            expected_artifact_id=900,
        )
        self.assertEqual(record.artifact_id, 900)
        self.assertEqual(record.archive_sha256, "a" * 64)

        value["id"] = 901
        with self.assertRaisesRegex(hosted.HostedValidationError, "bound ID"):
            hosted.parse_receipt_artifact(
                json.dumps(value),
                run=self.validation_run,
                expected_artifact_id=900,
            )


if __name__ == "__main__":
    unittest.main()
