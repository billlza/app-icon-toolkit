"""Bound hosted-evidence and final pre-PATCH state tests."""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
from unittest import mock

from finalization_test_support import FinalizationTestCase
import release_attempt
import release_draft
import release_draft_staging as staging
import release_finalization_core as core
import release_hosted_validation
import release_immutability
import release_publication_preflight as preflight
import release_targets


class ReleasePublicationPreflightTests(FinalizationTestCase):
    def test_prepublication_state_rebinds_all_inputs_with_policy_last(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-prepublication-") as temporary:
            root = Path(temporary)
            attempt, base_options, run, notes, assets = self.publication_fixture(root)
            hosted_binding = core.HostedValidationInput(
                workflow_id=700,
                run_id=701,
                run_attempt=2,
                receipt_artifact_id=702,
            )
            options = replace(base_options, hosted_validation=hosted_binding)
            verified = release_draft.VerifiedDraft(
                repository=options.repository,
                run=run,
                release_id="R_kgDORelease",
                release_database_id=67890,
                expected_body=notes,
                assets=assets,
            )
            hosted_run = release_draft.WorkflowRun(
                workflow_id=hosted_binding.workflow_id,
                run_id=hosted_binding.run_id,
                attempt=hosted_binding.run_attempt,
                tag=run.tag,
                head_sha=run.head_sha,
            )
            workflow = release_hosted_validation.HostedWorkflowIdentity(
                workflow_id=hosted_binding.workflow_id,
                name=release_hosted_validation.EXPECTED_WORKFLOW_NAME,
                path=release_hosted_validation.EXPECTED_WORKFLOW_PATH,
                state="active",
            )
            receipt = mock.Mock(
                release=mock.sentinel.receipt_release,
                assets=(mock.sentinel.receipt_asset,),
            )
            numeric_release = mock.sentinel.numeric_release
            events: list[str] = []

            def observed(name: str, value: object = None):
                def invoke(*_args: object, **_kwargs: object) -> object:
                    events.append(name)
                    return value

                return invoke

            with mock.patch.object(
                preflight.source,
                "read_workflow_run",
                side_effect=observed("source-run", run),
            ), mock.patch.object(
                preflight.source,
                "validate_remote_tag",
                side_effect=observed("remote-tag", mock.sentinel.remote_tag),
            ), mock.patch.object(
                preflight.source,
                "read_hosted_validation_workflow",
                side_effect=observed("hosted-workflow", workflow),
            ), mock.patch.object(
                preflight.source,
                "read_hosted_validation_run",
                side_effect=observed("hosted-run", hosted_run),
            ), mock.patch.object(
                preflight,
                "require_persisted_hosted_validation",
                side_effect=observed("persisted-receipt", receipt),
            ), mock.patch.object(
                staging,
                "read_release_json",
                side_effect=observed(
                    "graphql-draft",
                    self.publication_release_json(run, notes, assets, draft=True),
                ),
            ), mock.patch.object(
                staging,
                "read_numeric_draft_release",
                side_effect=observed("numeric-draft", numeric_release),
            ), mock.patch.object(
                release_hosted_validation,
                "require_exact_draft_release",
                side_effect=observed("numeric-binding"),
            ), mock.patch.object(
                preflight.source,
                "read_release_immutability_policy",
                side_effect=observed(
                    "immutability",
                    release_immutability.ReleaseImmutabilityPolicy(
                        enabled=True,
                        enforced_by_owner=False,
                    ),
                ),
            ):
                preflight.require_prepublication_state(
                    options,
                    release_targets.load_contract(),
                    attempt,
                    verified,
                )

            self.assertEqual(
                events,
                [
                    "source-run",
                    "remote-tag",
                    "hosted-workflow",
                    "hosted-run",
                    "persisted-receipt",
                    "graphql-draft",
                    "numeric-draft",
                    "numeric-binding",
                    "immutability",
                ],
            )

    def test_persisted_hosted_receipt_preserves_numeric_asset_id_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-persisted-hosted-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            contract = release_targets.load_contract()
            hosted_binding = core.HostedValidationInput(
                workflow_id=700,
                run_id=701,
                run_attempt=2,
                receipt_artifact_id=702,
            )
            options = replace(
                self.options(root, attempt),
                hosted_validation=hosted_binding,
            )
            source_run = release_draft.WorkflowRun(
                workflow_id=options.binding.workflow_database_id,
                run_id=options.binding.run_id,
                attempt=options.binding.run_attempt,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
            hosted_run = release_draft.WorkflowRun(
                workflow_id=hosted_binding.workflow_id,
                run_id=hosted_binding.run_id,
                attempt=hosted_binding.run_attempt,
                tag=source_run.tag,
                head_sha=source_run.head_sha,
            )
            names = tuple(
                sorted(
                    {
                        *(target.release_filename(source_run.tag) for target in contract.targets),
                        release_hosted_validation.CHECKSUM_ASSET_NAME,
                    }
                )
            )
            paths: dict[str, Path] = {}
            for index, name in enumerate(names):
                path = attempt / name
                path.write_bytes(f"signed asset {index}\n".encode("ascii"))
                paths[name] = path
            assets = release_draft.snapshot_local_assets(
                paths,
                expected_names=names,
            )
            draft = release_hosted_validation.DraftRelease(
                release_id="R_hosted",
                release_database_id=800,
                tag=source_run.tag,
                name=f"App Icon Toolkit {source_run.tag}",
                body="release notes\n",
                is_draft=True,
                is_prerelease=False,
                assets=tuple(
                    release_hosted_validation.DraftReleaseAsset(
                        asset_id=900 + index,
                        name=asset.name,
                        size=asset.size,
                        sha256=asset.sha256,
                    )
                    for index, asset in enumerate(assets)
                ),
            )
            plan = release_hosted_validation.create_plan(
                repository=options.repository,
                source_run=source_run,
                validation_run=hosted_run,
                release=draft,
                release_notes=draft.body,
                identity_sha1=options.identity_sha1,
                contract=contract,
            )
            results = tuple(
                release_hosted_validation.ValidationResult(
                    **asdict(spec),
                    binary_sha256="f" * 64,
                    identity_sha1=options.identity_sha1,
                    identifier=contract.macos_signing.code_identifier,
                    team_id=contract.macos_signing.team_id,
                    architectures=spec.expected_architectures,
                    signature_valid=True,
                    notarization_ticket_valid=True,
                    mcp_smoke_valid=True,
                )
                for spec in plan.validations
            )
            receipt = release_hosted_validation.create_bound_receipt(
                plan,
                refreshed_release=draft,
                results=results,
                contract=contract,
            )
            verified = release_draft.VerifiedDraft(
                repository=options.repository,
                run=source_run,
                release_id=draft.release_id,
                release_database_id=draft.release_database_id,
                expected_body=draft.body,
                assets=assets,
            )
            receipt_value = release_hosted_validation.receipt_payload(receipt)
            receipt_sha256 = hashlib.sha256(
                release_hosted_validation.canonical_json(receipt_value).encode("ascii")
            ).hexdigest()
            persisted = {
                "workflow_id": hosted_run.workflow_id,
                "workflow_name": release_hosted_validation.EXPECTED_WORKFLOW_NAME,
                "workflow_path": release_hosted_validation.EXPECTED_WORKFLOW_PATH,
                "workflow_state": "active",
                "run_id": hosted_run.run_id,
                "run_attempt": hosted_run.attempt,
                "receipt_artifact_id": hosted_binding.receipt_artifact_id,
                "receipt_artifact_name": release_hosted_validation.receipt_artifact_name(
                    hosted_run
                ),
                "receipt_artifact_size": 1234,
                "receipt_artifact_sha256": "a" * 64,
                "receipt_sha256": receipt_sha256,
                "release_id": verified.release_id,
                "release_database_id": verified.release_database_id,
                "assets": staging.asset_receipt_summary(assets),
                "receipt": receipt_value,
            }
            release_attempt.write_receipt_no_replace(
                attempt,
                "hosted-validation-verified.json",
                persisted,
            )

            self.assertEqual(
                preflight.require_persisted_hosted_validation(
                    options,
                    contract,
                    attempt,
                    verified,
                ),
                receipt,
            )

            tampered_attempt = root / "tampered-attempt"
            tampered_attempt.mkdir(mode=0o700)
            tampered = json.loads(json.dumps(persisted))
            tampered["receipt"]["assets"][0]["asset_id"] += 10_000
            release_attempt.write_receipt_no_replace(
                tampered_attempt,
                "hosted-validation-verified.json",
                tampered,
            )
            with self.assertRaisesRegex(
                core.FinalizationError,
                "differs from its artifact digest",
            ):
                preflight.require_persisted_hosted_validation(
                    options,
                    contract,
                    tampered_attempt,
                    verified,
                )

    def test_prepublication_tag_drift_fails_before_later_checks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-prepublication-tag-") as temporary:
            root = Path(temporary)
            attempt, base_options, run, notes, assets = self.publication_fixture(root)
            options = replace(
                base_options,
                hosted_validation=core.HostedValidationInput(
                    workflow_id=700,
                    run_id=701,
                    run_attempt=2,
                    receipt_artifact_id=702,
                ),
            )
            verified = release_draft.VerifiedDraft(
                repository=options.repository,
                run=run,
                release_id="R_kgDORelease",
                release_database_id=67890,
                expected_body=notes,
                assets=assets,
            )
            with mock.patch.object(
                preflight.source,
                "read_workflow_run",
                return_value=run,
            ), mock.patch.object(
                preflight.source,
                "validate_remote_tag",
                side_effect=core.FinalizationError(
                    "remote annotated tag moved"
                ),
            ), mock.patch.object(
                preflight.source,
                "read_hosted_validation_workflow",
            ) as hosted_workflow, mock.patch.object(
                staging,
                "read_release_json",
            ) as release_json, mock.patch.object(
                staging,
                "read_numeric_draft_release",
            ) as numeric_release, mock.patch.object(
                preflight.source,
                "read_release_immutability_policy",
            ) as immutability:
                with self.assertRaisesRegex(
                    core.FinalizationError,
                    "tag moved",
                ):
                    preflight.require_prepublication_state(
                        options,
                        release_targets.load_contract(),
                        attempt,
                        verified,
                    )

            hosted_workflow.assert_not_called()
            release_json.assert_not_called()
            numeric_release.assert_not_called()
            immutability.assert_not_called()
