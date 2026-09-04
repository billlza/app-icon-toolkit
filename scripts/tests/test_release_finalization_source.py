"""Canonical workflow, tag, policy, and artifact-source tests."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from unittest import mock

from finalization_test_support import FinalizationTestCase
import release_artifacts
import release_draft
import release_finalization_core as core
import release_finalization_source as source
import release_hosted_validation
import release_targets


class ReleaseFinalizationSourceTests(FinalizationTestCase):
    def test_download_resume_uses_the_shared_archive_mapping(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-download-resume-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            options = self.options(root, attempt)
            run = release_draft.WorkflowRun(
                workflow_id=options.binding.workflow_database_id,
                run_id=options.binding.run_id,
                attempt=options.binding.run_attempt,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
            contract = release_targets.load_contract()
            (attempt / "download-manifest.json").write_text(
                "{}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                source,
                "read_artifact_inventory",
                return_value=(),
            ), mock.patch.object(
                source,
                "read_workflow_run",
                return_value=run,
            ), mock.patch.object(
                source,
                "verify_release_assets",
            ), mock.patch.object(
                source,
                "archive_paths",
                return_value={},
            ) as archive_mapping, mock.patch.object(
                source,
                "_download_manifest_payload",
                return_value={},
            ), mock.patch.object(source, "ensure_receipt"):
                downloads = source.download_candidates(
                    options,
                    contract,
                    attempt,
                    run,
                )

            self.assertEqual(downloads, attempt / "downloads")
            archive_mapping.assert_called_once_with(
                downloads,
                contract,
                options.binding.tag,
            )

    def test_download_candidates_detects_inventory_change_after_exact_id_download(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-inventory-change-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            attempt = root / "attempt"
            attempt.mkdir(mode=0o700)
            options = self.options(root, attempt)
            full_contract = release_targets.load_contract()
            target = full_contract.targets[0]
            contract = release_targets.ReleaseContract(
                release_toolchain=full_contract.release_toolchain,
                macos_signing=full_contract.macos_signing,
                targets=(target,),
            )
            run = release_draft.WorkflowRun(
                workflow_id=options.binding.workflow_database_id,
                run_id=options.binding.run_id,
                attempt=options.binding.run_attempt,
                tag=options.binding.tag,
                head_sha=options.binding.head_sha,
            )
            artifact_name = target.artifact_name_for_attempt(run.attempt)
            before = release_artifacts.ArtifactRecord(
                artifact_id=100,
                name=artifact_name,
                size_in_bytes=5,
                archive_sha256="0" * 64,
                created_at="2026-09-04T00:00:00Z",
                updated_at="2026-09-04T00:00:00Z",
                run_id=run.run_id,
                head_sha=run.head_sha,
                head_branch=run.tag,
                repository_id=99,
                head_repository_id=99,
            )
            after = replace(before, artifact_id=101, archive_sha256="a" * 64)

            def extract(
                _repository: str,
                _record: release_artifacts.ArtifactRecord,
                expected_name: str,
                _cache: Path,
                downloads: Path,
                _downloader: object,
            ) -> Path:
                destination = downloads / expected_name
                destination.write_bytes(b"public archive")
                return destination

            def portable_private_subdirectory(parent: Path, relative: str) -> Path:
                directory = parent / relative
                directory.mkdir(mode=0o700, exist_ok=True)
                return directory

            with mock.patch.object(
                source,
                "read_artifact_inventory",
                side_effect=((before,), (after,)),
            ), mock.patch.object(
                source.release_artifact_download,
                "GitHubArtifactZipDownloader",
                return_value=object(),
            ), mock.patch.object(
                source.release_artifact_download,
                "download_public_archive",
                side_effect=extract,
            ), mock.patch.object(
                source,
                "read_workflow_run",
                return_value=run,
            ), mock.patch.object(
                source,
                "private_subdirectory",
                side_effect=portable_private_subdirectory,
            ):
                with self.assertRaisesRegex(
                    core.FinalizationError,
                    "inventory changed during download",
                ):
                    source.download_candidates(
                        options,
                        contract,
                        attempt,
                        run,
                    )
            self.assertFalse((attempt / "download-manifest.json").exists())

    def test_source_workflow_run_is_bound_to_canonical_hostname_api(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-source-workflow-") as temporary:
            root = Path(temporary)
            options = self.options(root, root / "attempt")
            workflow_payload = json.dumps(
                {
                    "id": options.binding.workflow_database_id,
                    "name": release_draft.EXPECTED_WORKFLOW,
                    "path": release_draft.EXPECTED_WORKFLOW_PATH,
                    "state": "active",
                }
            )
            run_payload = json.dumps(
                {
                    "databaseId": options.binding.run_id,
                    "workflowDatabaseId": options.binding.workflow_database_id,
                    "attempt": options.binding.run_attempt,
                    "headBranch": options.binding.tag,
                    "headSha": options.binding.head_sha,
                    "workflowName": release_draft.EXPECTED_WORKFLOW,
                    "event": release_draft.EXPECTED_EVENT,
                    "status": "completed",
                    "conclusion": "success",
                }
            )
            with mock.patch.object(
                source,
                "required_command",
                side_effect=(workflow_payload, run_payload),
            ) as required:
                run = source.read_workflow_run(options)

            self.assertEqual(run.workflow_id, options.binding.workflow_database_id)
            self.assertEqual(
                required.call_args_list,
                [
                    mock.call(
                        (
                            "gh",
                            "api",
                            "--hostname",
                            "github.com",
                            "repos/example/app-icon-toolkit/actions/workflows/release.yml",
                        )
                    ),
                    mock.call(
                        (
                            "gh",
                            "run",
                            "view",
                            str(options.binding.run_id),
                            "--repo",
                            f"github.com/{options.repository}",
                            "--json",
                            source.WORKFLOW_RUN_FIELDS,
                        )
                    ),
                ],
            )

    def test_hosted_workflow_is_bound_to_canonical_hostname_api(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-hosted-workflow-") as temporary:
            root = Path(temporary)
            binding = core.HostedValidationInput(
                workflow_id=700,
                run_id=701,
                run_attempt=2,
                receipt_artifact_id=702,
            )
            options = replace(
                self.options(root, root / "attempt"),
                hosted_validation=binding,
            )
            payload = json.dumps(
                {
                    "id": binding.workflow_id,
                    "name": release_hosted_validation.EXPECTED_WORKFLOW_NAME,
                    "path": release_hosted_validation.EXPECTED_WORKFLOW_PATH,
                    "state": "active",
                }
            )
            with mock.patch.object(
                source,
                "required_command",
                return_value=payload,
            ) as required:
                workflow = source.read_hosted_validation_workflow(
                    options,
                    binding,
                )

            self.assertEqual(workflow.workflow_id, binding.workflow_id)
            required.assert_called_once_with(
                (
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "repos/example/app-icon-toolkit/actions/workflows/"
                    "validate-signed-draft.yml",
                )
            )

    def test_hosted_run_view_uses_host_qualified_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-hosted-run-") as temporary:
            root = Path(temporary)
            binding = core.HostedValidationInput(
                workflow_id=700,
                run_id=701,
                run_attempt=2,
                receipt_artifact_id=702,
            )
            options = replace(
                self.options(root, root / "attempt"),
                hosted_validation=binding,
            )
            payload = json.dumps(
                {
                    "databaseId": binding.run_id,
                    "workflowDatabaseId": binding.workflow_id,
                    "attempt": binding.run_attempt,
                    "headBranch": options.binding.tag,
                    "headSha": options.binding.head_sha,
                    "workflowName": release_hosted_validation.EXPECTED_WORKFLOW_NAME,
                    "event": release_hosted_validation.EXPECTED_WORKFLOW_EVENT,
                    "status": "completed",
                    "conclusion": "success",
                }
            )
            with mock.patch.object(
                source,
                "required_command",
                return_value=payload,
            ) as required:
                run = source.read_hosted_validation_run(
                    options,
                    binding,
                )

            self.assertEqual(run.workflow_id, binding.workflow_id)
            required.assert_called_once_with(
                (
                    "gh",
                    "run",
                    "view",
                    str(binding.run_id),
                    "--repo",
                    f"github.com/{options.repository}",
                    "--json",
                    source.WORKFLOW_RUN_FIELDS,
                )
            )

    def test_remote_annotated_tag_uses_hostname_bound_github_api(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-remote-tag-") as temporary:
            root = Path(temporary)
            options = self.options(root, root / "attempt")
            tag_object_sha = "89abcdef0123456789abcdef0123456789abcdef"
            ref_payload = json.dumps(
                {
                    "ref": f"refs/tags/{options.binding.tag}",
                    "object": {"type": "tag", "sha": tag_object_sha},
                }
            )
            tag_payload = json.dumps(
                {
                    "sha": tag_object_sha,
                    "tag": options.binding.tag,
                    "object": {
                        "type": "commit",
                        "sha": options.binding.head_sha,
                    },
                }
            )
            with mock.patch.object(
                source,
                "required_command",
                side_effect=(tag_object_sha, ref_payload, tag_payload),
            ) as required:
                binding = source.validate_remote_tag(options)

            self.assertEqual(binding.tag_object_sha, tag_object_sha)
            self.assertEqual(
                required.call_args_list,
                [
                    mock.call(
                        ("git", "rev-parse", f"refs/tags/{options.binding.tag}"),
                        cwd=options.plugin_root,
                    ),
                    mock.call(
                        (
                            "gh",
                            "api",
                            "--hostname",
                            "github.com",
                            "repos/example/app-icon-toolkit/git/ref/tags/v1.2.3",
                        )
                    ),
                    mock.call(
                        (
                            "gh",
                            "api",
                            "--hostname",
                            "github.com",
                            f"repos/example/app-icon-toolkit/git/tags/{tag_object_sha}",
                        )
                    ),
                ],
            )

    def test_immutability_gate_uses_exact_read_only_github_api(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-immutability-api-") as temporary:
            root = Path(temporary)
            options = self.options(root, root / "attempt")
            with mock.patch.object(
                source,
                "required_command",
                return_value='{"enabled":true,"enforced_by_owner":false}',
            ) as required:
                policy = source.read_release_immutability_policy(
                    options
                )

            self.assertTrue(policy.enabled)
            self.assertFalse(policy.enforced_by_owner)
            required.assert_called_once_with(
                (
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "-H",
                    "X-GitHub-Api-Version:2026-03-10",
                    "repos/example/app-icon-toolkit/immutable-releases",
                )
            )
