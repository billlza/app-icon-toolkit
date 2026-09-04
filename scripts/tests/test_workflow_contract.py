from __future__ import annotations

from pathlib import Path
import re
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATHS = (
    Path(".github/workflows/ci.yml"),
    Path(".github/workflows/release.yml"),
    Path(".github/workflows/validate-signed-draft.yml"),
)
JOB_HEADER = re.compile(r"^  ([a-zA-Z0-9_-]+):$", re.MULTILINE)
PINNED_ACTION = re.compile(r"^\s*- uses: ([^@\s]+)@([^\s]+)", re.MULTILINE)


def workflow_text(relative: Path) -> str:
    return (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")


def job_blocks(workflow: str) -> dict[str, str]:
    try:
        jobs = workflow.split("jobs:\n", maxsplit=1)[1]
    except IndexError as error:
        raise AssertionError("workflow is missing a jobs mapping") from error
    matches = tuple(JOB_HEADER.finditer(jobs))
    return {
        match.group(1): jobs[
            match.start() : matches[index + 1].start()
            if index + 1 < len(matches)
            else len(jobs)
        ]
        for index, match in enumerate(matches)
    }


def job_permissions(block: str) -> dict[str, str]:
    matches = re.findall(
        r"^    permissions:\n((?:^      [a-z-]+: [a-z]+\n)+)",
        block,
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise AssertionError("job must contain exactly one permissions mapping")
    permissions: dict[str, str] = {}
    for line in matches[0].splitlines():
        name, value = line.strip().split(": ", maxsplit=1)
        if name in permissions:
            raise AssertionError(f"job repeats permission {name!r}")
        permissions[name] = value
    return permissions


class WorkflowContractTests(unittest.TestCase):
    def test_every_job_has_a_bounded_timeout(self) -> None:
        for relative in WORKFLOW_PATHS:
            with self.subTest(workflow=relative.as_posix()):
                blocks = job_blocks(workflow_text(relative))
                self.assertTrue(blocks)
                for job_name, block in blocks.items():
                    with self.subTest(job=job_name):
                        matches = re.findall(r"^    timeout-minutes: ([0-9]+)$", block, re.MULTILINE)
                        self.assertEqual(len(matches), 1)
                        self.assertGreater(int(matches[0]), 0)

    def test_every_checkout_disables_persisted_credentials(self) -> None:
        for relative in WORKFLOW_PATHS:
            with self.subTest(workflow=relative.as_posix()):
                workflow = workflow_text(relative)
                checkout_count = workflow.count("uses: actions/checkout@")
                self.assertGreater(checkout_count, 0)
                self.assertEqual(
                    workflow.count("persist-credentials: false"),
                    checkout_count,
                )

    def test_every_external_action_is_pinned_to_a_full_commit(self) -> None:
        for relative in WORKFLOW_PATHS:
            with self.subTest(workflow=relative.as_posix()):
                actions = PINNED_ACTION.findall(workflow_text(relative))
                self.assertTrue(actions)
                for action, revision in actions:
                    with self.subTest(action=action):
                        self.assertRegex(revision, r"^[0-9a-f]{40}$")

    def test_tag_workflow_stages_only_an_empty_draft(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("  stage-draft:\n", workflow)
        self.assertIn("python3 scripts/stage-release-draft.py", workflow)
        self.assertIn(
            "needs: [quality, policy, msrv, build-assets, verify-codex-install, verify-universal-intel]",
            workflow,
        )
        for forbidden in (
            "  publish:\n",
            "gh release create",
            "gh release upload",
            "--draft=false",
            "release-assets/*",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, workflow)

    def test_universal_archive_consumers_follow_the_matrix_contract(self) -> None:
        for relative in (
            Path(".github/workflows/ci.yml"),
            Path(".github/workflows/release.yml"),
        ):
            with self.subTest(workflow=relative.as_posix()):
                workflow = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(
                    "${{ matrix.id }}.${{ matrix.archive_format }}",
                    workflow,
                )
                self.assertIn(
                    '"extracted/app-icon-toolkit/bin/${{ matrix.binary_name }}"',
                    workflow,
                )
                self.assertNotIn("${{ matrix.id }}.tar.gz", workflow)

    def test_ci_distribution_uses_a_valid_stable_placeholder_tag(self) -> None:
        workflow = workflow_text(Path(".github/workflows/ci.yml"))
        self.assertNotIn("v0.0.0-ci", workflow)
        self.assertEqual(workflow.count("--tag v0.0.0"), 1)
        self.assertEqual(
            workflow.count(
                "app-icon-toolkit-v0.0.0-${{ matrix.id }}."
                "${{ matrix.archive_format }}"
            ),
            2,
        )

    def test_release_candidates_remain_available_for_local_finalization(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        stripped_lines = [line.strip() for line in workflow.splitlines()]
        self.assertEqual(stripped_lines.count("retention-days: 14"), 1)
        self.assertNotIn("retention-days: 1", stripped_lines)

    def test_every_artifact_producer_and_consumer_is_attempt_bound(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        attempt_bound_name = (
            "${{ matrix.artifact_name }}-attempt-${{ github.run_attempt }}"
        )
        self.assertEqual(workflow.count(attempt_bound_name), 3)
        self.assertEqual(workflow.count(f"name: {attempt_bound_name}"), 1)
        self.assertEqual(workflow.count(f'--name "{attempt_bound_name}"'), 2)
        self.assertNotIn("name: app-icon-toolkit-${{ matrix.id }}", workflow)
        self.assertNotIn('--name "app-icon-toolkit-${{ matrix.id }}"', workflow)

        ci_workflow = workflow_text(Path(".github/workflows/ci.yml"))
        ci_upload_name = (
            "app-icon-toolkit-${{ matrix.id }}-attempt-${{ github.run_attempt }}"
        )
        ci_download_name = (
            "app-icon-toolkit-${{ matrix.id }}-attempt-${GITHUB_RUN_ATTEMPT}"
        )
        self.assertEqual(ci_workflow.count(f"name: {ci_upload_name}"), 1)
        self.assertEqual(ci_workflow.count(f'--name "{ci_download_name}"'), 2)
        self.assertNotIn("name: app-icon-toolkit-${{ matrix.id }}\n", ci_workflow)
        self.assertNotIn(
            '--name "app-icon-toolkit-${{ matrix.id }}"',
            ci_workflow,
        )

    def test_signed_draft_validation_is_secret_free_and_attempt_bound(self) -> None:
        workflow = (
            REPOSITORY_ROOT
            / ".github"
            / "workflows"
            / "validate-signed-draft.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("name: Validate Signed Draft\n", workflow)
        self.assertIn("  workflow_dispatch:\n", workflow)
        self.assertNotIn("  push:\n", workflow)
        self.assertNotIn("  pull_request_target:\n", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertEqual(workflow.count("persist-credentials: false"), 3)
        self.assertEqual(workflow.count("timeout-minutes: 10"), 2)
        self.assertEqual(workflow.count("timeout-minutes: 30"), 1)
        self.assertIn(
            "matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}",
            workflow,
        )

        blocks = job_blocks(workflow)
        self.assertEqual(set(blocks), {"plan", "validate", "receipt"})
        for job_name, block in blocks.items():
            with self.subTest(job=job_name):
                self.assertEqual(
                    job_permissions(block),
                    {"actions": "read", "contents": "read"},
                )
                self.assertNotIn("id-token:", block)
                self.assertNotRegex(
                    block,
                    r"(?m)^\s+[a-z-]+:\s+(?:write|write-all)\s*$",
                )
                self.assertEqual(block.count("uses: actions/checkout@"), 1)
                self.assertEqual(block.count("ref: ${{ inputs.head_sha }}"), 1)
                self.assertEqual(block.count("persist-credentials: false"), 1)

        self.assertIn("needs: plan\n", blocks["validate"])
        self.assertIn("strategy:\n      fail-fast: false\n", blocks["validate"])
        self.assertIn("needs: [plan, validate]\n", blocks["receipt"])

        plan_producer = (
            "name: signed-draft-plan-run-${{ github.run_id }}-attempt-"
            "${{ github.run_attempt }}"
        )
        plan_consumer = (
            '--name "signed-draft-plan-run-${GITHUB_RUN_ID}-attempt-'
            '${GITHUB_RUN_ATTEMPT}"'
        )
        result_producer = (
            "name: signed-draft-result-${{ matrix.validation_id }}-run-"
            "${{ github.run_id }}-attempt-${{ github.run_attempt }}"
        )
        result_consumer = (
            '--name "signed-draft-result-${validation_id}-run-${GITHUB_RUN_ID}-'
            'attempt-${GITHUB_RUN_ATTEMPT}"'
        )
        receipt_producer = (
            "name: hosted-validation-receipt-run-${{ github.run_id }}-attempt-"
            "${{ github.run_attempt }}"
        )
        self.assertEqual(workflow.count(plan_producer), 1)
        self.assertEqual(workflow.count(plan_consumer), 2)
        self.assertEqual(workflow.count(result_producer), 1)
        self.assertEqual(workflow.count(result_consumer), 1)
        self.assertEqual(workflow.count(receipt_producer), 1)
        for unbound_name in (
            "name: signed-draft-plan-run-${{ github.run_id }}\n",
            "name: signed-draft-result-${{ matrix.validation_id }}-run-"
            "${{ github.run_id }}\n",
            "name: hosted-validation-receipt-run-${{ github.run_id }}\n",
        ):
            with self.subTest(unbound_name=unbound_name):
                self.assertNotIn(unbound_name, workflow)

        for command in (
            'test "$GITHUB_REF_TYPE" = "tag"',
            'test "$GITHUB_REF_NAME" = "$EXPECTED_TAG"',
            'test "$GITHUB_SHA" = "$EXPECTED_HEAD_SHA"',
            'test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD_SHA"',
        ):
            with self.subTest(preflight=command):
                self.assertEqual(blocks["plan"].count(command), 1)
        self.assertEqual(
            blocks["plan"].count("EXPECTED_TAG: ${{ inputs.tag }}"),
            1,
        )
        self.assertEqual(
            blocks["plan"].count("EXPECTED_HEAD_SHA: ${{ inputs.head_sha }}"),
            1,
        )

        smoke_start = workflow.index(
            "      - name: Verify signature, ticket, architecture, and MCP runtime\n"
        )
        smoke_end = workflow.index(
            "      - name: Upload native validation result\n",
            smoke_start,
        )
        smoke_step = workflow[smoke_start:smoke_end]
        self.assertNotIn("GH_TOKEN", smoke_step)
        self.assertNotIn("GITHUB_TOKEN", smoke_step)
        self.assertNotIn("github.token", smoke_step)
        self.assertNotIn("secrets.", smoke_step)


if __name__ == "__main__":
    unittest.main()
