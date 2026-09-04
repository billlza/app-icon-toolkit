from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from release_test_support import validate_signed_draft
import macos_signing
import release_draft
import release_hosted_validation as hosted
import release_hosted_validation_runner as hosted_runner
import release_targets


REPOSITORY = "example/app-icon-toolkit"
TAG = "v1.2.3"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
IDENTITY = "2DA7764ED42B213AE04925B6261238B24C758FE1"
NOTES = "# Release notes\n\nVerified changes.\n"


class ValidateSignedDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = release_targets.load_contract()
        self.source_run = release_draft.WorkflowRun(100, 101, 2, TAG, HEAD_SHA)
        self.validation_run = release_draft.WorkflowRun(200, 201, 1, TAG, HEAD_SHA)
        names = sorted(
            {
                *(target.release_filename(TAG) for target in self.contract.targets),
                hosted.CHECKSUM_ASSET_NAME,
            }
        )
        assets = []
        for index, name in enumerate(names):
            payload = name.encode("utf-8")
            assets.append(
                hosted.DraftReleaseAsset(
                    asset_id=300 + index,
                    name=name,
                    size=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
        self.release = hosted.DraftRelease(
            release_id="RE_kwDOExample",
            release_database_id=250,
            tag=TAG,
            name=f"App Icon Toolkit {TAG}",
            body=NOTES,
            is_draft=True,
            is_prerelease=False,
            assets=tuple(assets),
        )
        self.plan = hosted.create_plan(
            repository=REPOSITORY,
            source_run=self.source_run,
            validation_run=self.validation_run,
            release=self.release,
            release_notes=NOTES,
            identity_sha1=IDENTITY,
            contract=self.contract,
        )

    def release_json(self) -> str:
        return json.dumps(
            {
                "id": self.release.release_database_id,
                "node_id": self.release.release_id,
                "tag_name": TAG,
                "name": self.release.name,
                "body": NOTES,
                "draft": True,
                "prerelease": False,
                "assets": [
                    {
                        "id": asset.asset_id,
                        "name": asset.name,
                        "size": asset.size,
                        "digest": f"sha256:{asset.sha256}",
                        "state": "uploaded",
                    }
                    for asset in self.release.assets
                ],
            }
        )

    def source_run_json(self) -> str:
        return json.dumps(
            {
                "workflowDatabaseId": self.source_run.workflow_id,
                "databaseId": self.source_run.run_id,
                "attempt": self.source_run.attempt,
                "headBranch": TAG,
                "headSha": HEAD_SHA,
                "workflowName": release_draft.EXPECTED_WORKFLOW,
                "event": release_draft.EXPECTED_EVENT,
                "status": "completed",
                "conclusion": "success",
            }
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

    def test_build_plan_binds_source_run_release_and_hosted_run(self) -> None:
        plan = validate_signed_draft.build_plan(
            repository=REPOSITORY,
            source_workflow_id=self.source_run.workflow_id,
            source_run_id=self.source_run.run_id,
            source_run_attempt=self.source_run.attempt,
            source_run_json=self.source_run_json(),
            validation_workflow_id=self.validation_run.workflow_id,
            validation_run_id=self.validation_run.run_id,
            validation_run_attempt=self.validation_run.attempt,
            tag=TAG,
            head_sha=HEAD_SHA,
            release_id=self.release.release_id,
            release_database_id=self.release.release_database_id,
            release_json=self.release_json(),
            release_notes=NOTES,
            identity_sha1=IDENTITY,
            contract=self.contract,
        )
        self.assertEqual(plan, self.plan)

        with self.assertRaisesRegex(
            validate_signed_draft.HostedValidationCliError,
            "identity differs",
        ):
            validate_signed_draft.build_plan(
                repository=REPOSITORY,
                source_workflow_id=self.source_run.workflow_id,
                source_run_id=self.source_run.run_id,
                source_run_attempt=self.source_run.attempt,
                source_run_json=self.source_run_json(),
                validation_workflow_id=self.validation_run.workflow_id,
                validation_run_id=self.validation_run.run_id,
                validation_run_attempt=self.validation_run.attempt,
                tag=TAG,
                head_sha=HEAD_SHA,
                release_id="RE_replaced",
                release_database_id=self.release.release_database_id,
                release_json=self.release_json(),
                release_notes=NOTES,
                identity_sha1=IDENTITY,
                contract=self.contract,
            )

    @unittest.skipUnless(
        os.name == "posix",
        "hosted downloads require exact POSIX 0700/0600 private modes",
    )
    def test_download_uses_numeric_release_asset_id_and_verifies_digest(self) -> None:
        spec = self.plan.validations[0]
        with tempfile.TemporaryDirectory(prefix="hosted-download-") as temporary:
            output = Path(temporary) / "download"
            calls = []

            def download(command, destination, **keywords):
                calls.append((command, destination, keywords))
                destination.write_bytes(spec.archive_name.encode("utf-8"))

            with mock.patch.object(
                hosted_runner,
                "resolve_gh",
                return_value=Path("/usr/bin/gh"),
            ), mock.patch.object(
                hosted_runner.release_artifact_download,
                "download_command_to_file",
                side_effect=download,
            ):
                destination = validate_signed_draft.download_validation_asset(
                    self.plan,
                    spec,
                    output,
                )

            self.assertEqual(destination.name, spec.archive_name)
            command, called_destination, keywords = calls[0]
            self.assertEqual(called_destination, destination)
            self.assertEqual(
                command,
                (
                    "/usr/bin/gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "-H",
                    "Accept: application/octet-stream",
                    f"repos/{REPOSITORY}/releases/assets/{spec.archive_asset_id}",
                ),
            )
            self.assertEqual(keywords["expected_size"], spec.archive_size)

    def test_candidate_validation_refuses_github_tokens_before_execution(self) -> None:
        spec = self.plan.validations[0]
        for token_name in ("GH_TOKEN", "GITHUB_TOKEN"):
            with self.subTest(token_name=token_name), mock.patch.object(
                hosted_runner,
                "run_required",
            ) as run:
                with self.assertRaisesRegex(
                    validate_signed_draft.HostedValidationCliError,
                    "refuses GitHub token",
                ):
                    validate_signed_draft.validate_target(
                        plugin_root=Path("/plugin"),
                        plan=self.plan,
                        spec=spec,
                        archive=Path(spec.archive_name),
                        contract=self.contract,
                        environment={token_name: "secret"},
                    )
                run.assert_not_called()

    def test_target_validation_runs_signature_ticket_and_mcp_smoke_without_token(self) -> None:
        spec = next(
            item
            for item in self.plan.validations
            if item.target_id == "universal2-apple-darwin"
            and item.runtime_architecture == "arm64"
        )
        with tempfile.TemporaryDirectory(prefix="hosted-target-") as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            (plugin / "scripts").mkdir(parents=True)
            archive = root / spec.archive_name
            archive.write_bytes(spec.archive_name.encode("utf-8"))
            package = root / "package"
            binary = package / "bin" / spec.binary_name
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"signed binary")
            binary_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            signature = macos_signing.SignatureVerificationReceipt(
                signed_sha256=binary_digest,
                identity_sha1=IDENTITY,
                identifier=self.contract.macos_signing.code_identifier,
                team_id=self.contract.macos_signing.team_id,
                architectures=tuple(reversed(spec.expected_architectures)),
                slices=(),
            )

            with mock.patch.object(
                hosted_runner.sys,
                "platform",
                "darwin",
            ), mock.patch.object(
                hosted_runner,
                "runtime_architecture",
                return_value=spec.runtime_architecture,
            ), mock.patch.object(
                hosted_runner,
                "safe_extract_archive",
                return_value=package,
            ), mock.patch.object(
                hosted_runner,
                "run_required",
            ) as run, mock.patch.object(
                hosted_runner.macos_signing,
                "verify_signed",
                return_value=signature,
            ) as verify, mock.patch.object(
                hosted_runner.macos_signing,
                "check_notarization_ticket",
            ) as ticket:
                result = validate_signed_draft.validate_target(
                    plugin_root=plugin,
                    plan=self.plan,
                    spec=spec,
                    archive=archive,
                    contract=self.contract,
                    environment={
                        "ACTIONS_RUNTIME_TOKEN": "not-forwarded",
                        "HOME": "/untrusted",
                        "PATH": "/untrusted",
                    },
                )

            self.assertTrue(result.mcp_smoke_valid)
            self.assertEqual(result.binary_sha256, binary_digest)
            self.assertEqual(result.architectures, spec.expected_architectures)
            self.assertEqual(
                hosted.parse_validation_result(
                    hosted.canonical_json(asdict(result)),
                ),
                result,
            )
            verify.assert_called_once()
            ticket.assert_called_once()
            self.assertEqual(run.call_count, 2)
            smoke_argv = run.call_args_list[1].args[0]
            self.assertIn("smoke-installed-plugin.py", smoke_argv[1])
            self.assertEqual(Path(smoke_argv[-1]), package)
            smoke_environment = run.call_args_list[1].kwargs["environment"]
            self.assertEqual(
                smoke_environment,
                {
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                },
            )
            for name in (
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "GITHUB_ENV",
                "GITHUB_OUTPUT",
                "GITHUB_PATH",
                "ACTIONS_RUNTIME_TOKEN",
                "HOME",
            ):
                self.assertNotIn(name, smoke_environment)

    def test_result_inventory_rejects_missing_or_extra_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hosted-results-") as temporary:
            directory = Path(temporary)
            for spec in self.plan.validations:
                result = self.result(spec)
                (directory / f"{spec.validation_id}.json").write_text(
                    hosted.canonical_json(asdict(result)),
                    encoding="utf-8",
                )
            loaded = validate_signed_draft.load_exact_results(directory, self.plan)
            self.assertEqual(
                {result.validation_id for result in loaded},
                {spec.validation_id for spec in self.plan.validations},
            )

            (directory / "extra.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                validate_signed_draft.HostedValidationCliError,
                "file set is not exact",
            ):
                validate_signed_draft.load_exact_results(directory, self.plan)


if __name__ == "__main__":
    unittest.main()
