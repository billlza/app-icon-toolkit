from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATHS = (
    Path(".github/workflows/ci.yml"),
    Path(".github/workflows/release.yml"),
    Path(".github/workflows/validate-signed-draft.yml"),
)
JOB_HEADER = re.compile(r"^  ([a-zA-Z0-9_-]+):$", re.MULTILINE)
STEP_HEADER = re.compile(r"^      - [a-zA-Z0-9_-]+:", re.MULTILINE)
PINNED_ACTION = re.compile(r"^\s*- uses: ([^@\s]+)@([^\s]+)", re.MULTILINE)
MATRIX_OUTPUT = re.compile(
    r"matrix: \$\{\{ fromJSON\(needs\.[a-zA-Z0-9_-]+\.outputs\."
    r"([a-zA-Z0-9_]+)\) \}\}"
)
MATRIX_FIELD = re.compile(r"\bmatrix\.([a-zA-Z_][a-zA-Z0-9_]*)\b")
MATRIX_BRACKET = re.compile(r"\bmatrix\s*\[")
TARGET_OUTPUT_COMMAND = re.compile(
    r'echo "([a-zA-Z0-9_]+)=\$\(python3 scripts/release_targets\.py '
    r'([a-z0-9-]+)\)"'
)


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


def step_blocks(block: str) -> tuple[str, ...]:
    matches = tuple(STEP_HEADER.finditer(block))
    return tuple(
        block[
            match.start() : matches[index + 1].start()
            if index + 1 < len(matches)
            else len(block)
        ]
        for index, match in enumerate(matches)
    )


def step_name(step: str) -> str | None:
    match = re.search(
        r"^(?:      - name|        name): (.+)$",
        step,
        re.MULTILINE,
    )
    return match.group(1) if match else None


def named_step_blocks(block: str) -> dict[str, str]:
    named: dict[str, str] = {}
    for step in step_blocks(block):
        name = step_name(step)
        if name is None:
            continue
        if name in named:
            raise AssertionError(f"job repeats step name {name!r}")
        named[name] = step
    return named


def step_contracts(block: str) -> tuple[tuple[str | None, str, str], ...]:
    contracts = []
    for step in step_blocks(block):
        name = step_name(step)
        uses_match = re.search(
            r"^(?:      - uses|        uses): (.+)$",
            step,
            re.MULTILINE,
        )
        run_match = re.search(
            r"^(?:      - run|        run): (.+)$",
            step,
            re.MULTILINE,
        )
        if (uses_match is None) == (run_match is None):
            raise AssertionError("step must contain exactly one uses or run entry")
        if uses_match:
            contracts.append((name, "uses", uses_match.group(1)))
            continue
        assert run_match is not None
        run_value = run_match.group(1)
        if run_value in {"|", ">-"}:
            body_lines = []
            for line in step[run_match.end() :].splitlines():
                if not line:
                    continue
                if not line.startswith("          "):
                    raise AssertionError("run step contains an unexpected property")
                body_lines.append(line[10:])
            if not body_lines:
                raise AssertionError("run step must contain a command")
            run_value = "\n".join(body_lines)
        contracts.append((name, "run", run_value))
    return tuple(contracts)


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
            "${{ matrix.artifact_name }}-attempt-${{ github.run_attempt }}"
        )
        ci_download_name = (
            "${{ matrix.artifact_name }}-attempt-${GITHUB_RUN_ATTEMPT}"
        )
        self.assertEqual(ci_workflow.count(f"name: {ci_upload_name}"), 1)
        self.assertEqual(ci_workflow.count(f'--name "{ci_download_name}"'), 2)
        self.assertNotIn(
            "app-icon-toolkit-${{ matrix.id }}-attempt-",
            ci_workflow,
        )
        self.assertNotIn(
            '--name "app-icon-toolkit-${{ matrix.id }}"',
            ci_workflow,
        )

    def test_generated_matrices_cover_every_workflow_consumer_field(self) -> None:
        commands = {
            "matrix": "matrix",
            "codex_install_matrix": "codex-install-matrix",
            "universal_verify_matrix": "universal-verify-matrix",
        }
        expected_consumers = {
            (".github/workflows/ci.yml", "distribution", "matrix"),
            (
                ".github/workflows/ci.yml",
                "distribution-codex-install",
                "codex_install_matrix",
            ),
            (
                ".github/workflows/ci.yml",
                "distribution-universal-intel",
                "universal_verify_matrix",
            ),
            (".github/workflows/release.yml", "build-assets", "matrix"),
            (
                ".github/workflows/release.yml",
                "verify-codex-install",
                "codex_install_matrix",
            ),
            (
                ".github/workflows/release.yml",
                "verify-universal-intel",
                "universal_verify_matrix",
            ),
        }
        observed_consumers: set[tuple[str, str, str]] = set()
        generated: dict[str, list[dict[str, object]]] = {}

        for relative in (
            Path(".github/workflows/ci.yml"),
            Path(".github/workflows/release.yml"),
        ):
            workflow = workflow_text(relative)
            produced = TARGET_OUTPUT_COMMAND.findall(workflow)
            self.assertEqual(len(produced), len(dict(produced)))
            producer_commands = dict(produced)
            self.assertEqual(
                {name: producer_commands.get(name) for name in commands},
                commands,
            )
            blocks = job_blocks(workflow)
            for job_name, block in blocks.items():
                outputs = MATRIX_OUTPUT.findall(block)
                if not outputs:
                    continue
                self.assertEqual(len(outputs), 1)
                output = outputs[0]
                self.assertIn(output, producer_commands)
                observed_consumers.add((relative.as_posix(), job_name, output))
                command = producer_commands[output]

                if command not in generated:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(REPOSITORY_ROOT / "scripts" / "release_targets.py"),
                            command,
                        ],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        timeout=10,
                    )
                    self.assertEqual(completed.stderr, "")
                    value = json.loads(completed.stdout)
                    self.assertEqual(set(value), {"include"})
                    entries = value["include"]
                    self.assertIsInstance(entries, list)
                    self.assertTrue(entries)
                    self.assertTrue(all(isinstance(entry, dict) for entry in entries))
                    generated[command] = entries

                referenced_fields = set(MATRIX_FIELD.findall(block))
                self.assertTrue(referenced_fields)
                self.assertNotRegex(block, MATRIX_BRACKET)
                for index, entry in enumerate(generated[command]):
                    missing = referenced_fields - set(entry)
                    self.assertFalse(
                        missing,
                        f"{relative.as_posix()}:{job_name} <- {command} "
                        f"entry[{index}] missing {sorted(missing)}",
                    )

        self.assertEqual(observed_consumers, expected_consumers)
        self.assertEqual(set(generated), set(commands.values()))

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
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("write-all", workflow)
        self.assertEqual(workflow.count("persist-credentials: false"), 4)
        self.assertEqual(workflow.count("timeout-minutes: 10"), 4)
        self.assertEqual(workflow.count("timeout-minutes: 30"), 1)
        self.assertIn(
            "matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}",
            workflow,
        )

        blocks = job_blocks(workflow)
        expected_permissions = {
            "plan": {"actions": "read", "contents": "write"},
            "fetch": {"actions": "read", "contents": "write"},
            "validate": {"actions": "read", "contents": "read"},
            "refresh-draft": {"contents": "write"},
            "receipt": {"actions": "read", "contents": "read"},
        }
        self.assertEqual(set(blocks), set(expected_permissions))
        checkout_jobs = {"plan", "fetch", "validate", "receipt"}
        for job_name, block in blocks.items():
            with self.subTest(job=job_name):
                self.assertEqual(
                    job_permissions(block),
                    expected_permissions[job_name],
                )
                self.assertNotIn("id-token:", block)
                expected_checkout_count = 1 if job_name in checkout_jobs else 0
                self.assertEqual(
                    block.count("uses: actions/checkout@"),
                    expected_checkout_count,
                )
                self.assertEqual(
                    block.count("ref: ${{ inputs.head_sha }}"),
                    expected_checkout_count,
                )
                self.assertEqual(
                    block.count("persist-credentials: false"),
                    expected_checkout_count,
                )

        write_capable_jobs = {"plan", "fetch", "refresh-draft"}
        for job_name in write_capable_jobs:
            block = blocks[job_name]
            for forbidden in (
                "validate-target",
                "smoke-installed-plugin.py",
                "check-release-binary.py",
                "safe_extract_archive",
                "gh release",
                "--method POST",
                "--method PATCH",
                "--method PUT",
                "--method DELETE",
                "--method=",
                "-X ",
                "--field ",
                "--raw-field ",
                "--input ",
                "-f ",
                "-F ",
                "git push",
            ):
                with self.subTest(job=job_name, forbidden=forbidden):
                    self.assertNotIn(forbidden, block)

        checkout_action = (
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 "
            "# v7.0.1"
        )
        upload_action = (
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a "
            "# v7.0.1"
        )
        verify_dispatch = '''set -euo pipefail
test "$GITHUB_REF_TYPE" = "tag"
test "$GITHUB_REF_NAME" = "$EXPECTED_TAG"
test "$GITHUB_SHA" = "$EXPECTED_HEAD_SHA"
test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD_SHA"'''
        fetch_source_run = '''set -euo pipefail
gh run view "$SOURCE_RUN_ID" \\
  --repo "$GITHUB_REPOSITORY" \\
  --json databaseId,workflowDatabaseId,attempt,headBranch,headSha,workflowName,event,status,conclusion \\
  > source-run.json'''
        fetch_draft = '''set -euo pipefail
gh api --hostname github.com \\
  "repos/$GITHUB_REPOSITORY/releases/$RELEASE_DATABASE_ID" \\
  > draft-release.json'''
        fetch_workflow = '''set -euo pipefail
validation_workflow_id=$(gh api --hostname github.com \\
  "repos/$GITHUB_REPOSITORY/actions/workflows/validate-signed-draft.yml" \\
  --jq .id)
test "$validation_workflow_id" -gt 0
printf '%s\\n' "$validation_workflow_id" > validation-workflow-id.txt'''
        bind_plan = '''set -euo pipefail
validation_workflow_id=$(cat validation-workflow-id.txt)
python3 scripts/validate-signed-draft.py plan \\
  --repository "$GITHUB_REPOSITORY" \\
  --source-workflow-id "$SOURCE_WORKFLOW_ID" \\
  --source-run-id "$SOURCE_RUN_ID" \\
  --source-run-attempt "$SOURCE_RUN_ATTEMPT" \\
  --source-run-json source-run.json \\
  --validation-workflow-id "$validation_workflow_id" \\
  --validation-run-id "$GITHUB_RUN_ID" \\
  --validation-run-attempt "$GITHUB_RUN_ATTEMPT" \\
  --tag "$RELEASE_TAG" \\
  --head-sha "$RELEASE_HEAD_SHA" \\
  --release-id "$RELEASE_ID" \\
  --release-database-id "$RELEASE_DATABASE_ID" \\
  --release-json draft-release.json \\
  --notes-file CHANGELOG.md \\
  --identity-sha1 "$IDENTITY_SHA1" \\
  --output hosted-validation-plan.json \\
  --github-output "$GITHUB_OUTPUT"'''
        download_plan = '''set -euo pipefail
mkdir hosted-plan
gh run download "$GITHUB_RUN_ID" \\
  --repo "$GITHUB_REPOSITORY" \\
  --name "signed-draft-plan-run-${GITHUB_RUN_ID}-attempt-${GITHUB_RUN_ATTEMPT}" \\
  --dir hosted-plan'''
        fetch_asset = '''python3 scripts/validate-signed-draft.py download
--plan hosted-plan/hosted-validation-plan.json
--validation-id "$VALIDATION_ID"
--output-directory signed-asset'''
        refresh_draft = '''gh api --hostname github.com
"repos/$GITHUB_REPOSITORY/releases/$RELEASE_DATABASE_ID"
> fresh-draft-release.json'''
        expected_write_steps = {
            "plan": (
                (None, "uses", checkout_action),
                ("Verify tag-dispatch and checkout binding", "run", verify_dispatch),
                ("Fetch exact source workflow run", "run", fetch_source_run),
                ("Fetch exact signed Draft metadata", "run", fetch_draft),
                ("Fetch hosted validation workflow identity", "run", fetch_workflow),
                ("Bind source run and exact signed Draft assets", "run", bind_plan),
                ("Upload exact validation plan", "uses", upload_action),
            ),
            "fetch": (
                (None, "uses", checkout_action),
                ("Download this run's exact validation plan", "run", download_plan),
                (
                    "Fetch and verify exact numeric signed Draft asset",
                    "run",
                    fetch_asset,
                ),
                (
                    "Transfer verified bytes to the read-only validation boundary",
                    "uses",
                    upload_action,
                ),
            ),
            "refresh-draft": (
                (
                    "Refresh the exact unpublished release",
                    "run",
                    refresh_draft,
                ),
                (
                    "Transfer the attempt-bound Draft snapshot",
                    "uses",
                    upload_action,
                ),
            ),
        }
        for job_name, expected_steps in expected_write_steps.items():
            with self.subTest(job=job_name, contract="write-step-allowlist"):
                self.assertEqual(step_contracts(blocks[job_name]), expected_steps)

        token_steps = {
            "plan": {
                "Fetch exact source workflow run",
                "Fetch exact signed Draft metadata",
                "Fetch hosted validation workflow identity",
            },
            "fetch": {
                "Download this run's exact validation plan",
                "Fetch and verify exact numeric signed Draft asset",
            },
            "validate": {
                "Download this run's exact validation plan",
                "Download exact verified asset from the isolated fetch job",
            },
            "refresh-draft": {"Refresh the exact unpublished release"},
            "receipt": {"Download exact plan and native result artifacts"},
        }
        for job_name, expected_token_steps in token_steps.items():
            block = blocks[job_name]
            self.assertNotRegex(block, r"(?m)^    env:")
            self.assertEqual(
                block.count("GH_TOKEN: ${{ github.token }}"),
                len(expected_token_steps),
            )
            self.assertNotIn("GITHUB_TOKEN", block)
            for name, step in named_step_blocks(block).items():
                expected_count = 1 if name in expected_token_steps else 0
                with self.subTest(job=job_name, step=name, contract="token"):
                    self.assertEqual(
                        step.count("GH_TOKEN: ${{ github.token }}"),
                        expected_count,
                    )

        write_uploads = {
            ("plan", "Upload exact validation plan"): "hosted-validation-plan.json",
            (
                "fetch",
                "Transfer verified bytes to the read-only validation boundary",
            ): "signed-asset/${{ matrix.archive_name }}",
            (
                "refresh-draft",
                "Transfer the attempt-bound Draft snapshot",
            ): "fresh-draft-release.json",
        }
        for (job_name, name), expected_path in write_uploads.items():
            step = named_step_blocks(blocks[job_name])[name]
            with self.subTest(job=job_name, step=name, contract="upload"):
                self.assertEqual(step.count(f"path: {expected_path}"), 1)
                self.assertEqual(step.count("if-no-files-found: error"), 1)
                self.assertEqual(step.count("retention-days: 90"), 1)

        self.assertIn("needs: plan\n", blocks["fetch"])
        self.assertIn("needs: [plan, fetch]\n", blocks["validate"])
        self.assertIn("strategy:\n      fail-fast: false\n", blocks["validate"])
        self.assertIn("needs: [plan, validate]\n", blocks["refresh-draft"])
        self.assertIn(
            "needs: [plan, validate, refresh-draft]\n",
            blocks["receipt"],
        )

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
        asset_producer = (
            "name: signed-draft-asset-${{ matrix.validation_id }}-run-"
            "${{ github.run_id }}-attempt-${{ github.run_attempt }}"
        )
        asset_consumer = (
            '--name "signed-draft-asset-${{ matrix.validation_id }}-run-'
            '${GITHUB_RUN_ID}-attempt-${GITHUB_RUN_ATTEMPT}"'
        )
        refresh_producer = (
            "name: signed-draft-refresh-run-${{ github.run_id }}-attempt-"
            "${{ github.run_attempt }}"
        )
        refresh_consumer = (
            '--name "signed-draft-refresh-run-${GITHUB_RUN_ID}-attempt-'
            '${GITHUB_RUN_ATTEMPT}"'
        )
        self.assertEqual(workflow.count(plan_producer), 1)
        self.assertEqual(workflow.count(plan_consumer), 3)
        self.assertEqual(workflow.count(asset_producer), 1)
        self.assertEqual(workflow.count(asset_consumer), 1)
        self.assertEqual(workflow.count(result_producer), 1)
        self.assertEqual(workflow.count(result_consumer), 1)
        self.assertEqual(workflow.count(refresh_producer), 1)
        self.assertEqual(workflow.count(refresh_consumer), 1)
        self.assertEqual(workflow.count(receipt_producer), 1)
        for unbound_name in (
            "name: signed-draft-plan-run-${{ github.run_id }}\n",
            "name: signed-draft-result-${{ matrix.validation_id }}-run-"
            "${{ github.run_id }}\n",
            "name: hosted-validation-receipt-run-${{ github.run_id }}\n",
        ):
            with self.subTest(unbound_name=unbound_name):
                self.assertNotIn(unbound_name, workflow)

        self.assertEqual(
            blocks["fetch"].count("scripts/validate-signed-draft.py download"),
            1,
        )
        self.assertNotIn(
            "scripts/validate-signed-draft.py download",
            blocks["validate"],
        )
        self.assertIn(asset_consumer, blocks["validate"])
        self.assertIn(refresh_consumer, blocks["receipt"])
        self.assertNotIn("actions/checkout@", blocks["refresh-draft"])
        self.assertNotIn("scripts/", blocks["refresh-draft"])

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

        credential_free_steps = (
            (
                "      - id: plan\n",
                "      - name: Upload exact validation plan\n",
            ),
            (
                "      - name: Verify signature, ticket, architecture, and MCP runtime\n",
                "      - name: Upload native validation result\n",
            ),
            (
                "      - name: Build strict hosted validation receipt\n",
                "      - name: Upload attempt-bound validation receipt\n",
            ),
        )
        for start_marker, end_marker in credential_free_steps:
            start = workflow.index(start_marker)
            end = workflow.index(end_marker, start)
            step = workflow[start:end]
            for forbidden in (
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "github.token",
                "secrets.",
            ):
                with self.subTest(step=start_marker.strip(), forbidden=forbidden):
                    self.assertNotIn(forbidden, step)


if __name__ == "__main__":
    unittest.main()
