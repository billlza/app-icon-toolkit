#!/usr/bin/env python3
"""Thin CLI for hosted validation of signed draft release archives."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_hosted_validation as hosted
from release_hosted_validation_runner import (
    HostedValidationRunnerError,
    append_github_output,
    build_plan,
    download_validation_asset,
    find_spec,
    load_exact_results,
    load_plan,
    read_stable_text,
    validate_target,
    write_json_no_replace,
)
from release_notes import ReleaseNotesError, read_release_notes
from release_targets import CONTRACT_PATH, ReleaseContract, load_contract


# These aliases keep the small command surface directly testable without
# exposing the implementation module through the shell entrypoint.
HostedValidationCliError = HostedValidationRunnerError


def _plan_command(arguments: argparse.Namespace, contract: ReleaseContract) -> None:
    plan = build_plan(
        repository=arguments.repository,
        source_workflow_id=arguments.source_workflow_id,
        source_run_id=arguments.source_run_id,
        source_run_attempt=arguments.source_run_attempt,
        source_run_json=read_stable_text(
            arguments.source_run_json,
            label="source workflow run JSON",
        ),
        validation_workflow_id=arguments.validation_workflow_id,
        validation_run_id=arguments.validation_run_id,
        validation_run_attempt=arguments.validation_run_attempt,
        tag=arguments.tag,
        head_sha=arguments.head_sha,
        release_id=arguments.release_id,
        release_database_id=arguments.release_database_id,
        release_json=read_stable_text(
            arguments.release_json,
            label="draft release JSON",
        ),
        release_notes=read_release_notes(arguments.notes_file),
        identity_sha1=arguments.identity_sha1,
        contract=contract,
    )
    write_json_no_replace(arguments.output, hosted.plan_payload(plan), mode=0o644)
    matrix = json.dumps(
        {"include": [spec.matrix_entry() for spec in plan.validations]},
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    append_github_output(arguments.github_output, "matrix", matrix)


def _download_command(arguments: argparse.Namespace, contract: ReleaseContract) -> None:
    plan = load_plan(arguments.plan, contract)
    spec = find_spec(plan, arguments.validation_id)
    print(download_validation_asset(plan, spec, arguments.output_directory))


def _validate_command(arguments: argparse.Namespace, contract: ReleaseContract) -> None:
    plugin_root = arguments.plugin_root.resolve(strict=True)
    plan = load_plan(arguments.plan, contract)
    spec = find_spec(plan, arguments.validation_id)
    result = validate_target(
        plugin_root=plugin_root,
        plan=plan,
        spec=spec,
        archive=arguments.archive,
        contract=contract,
    )
    write_json_no_replace(arguments.output, asdict(result), mode=0o644)


def _list_command(arguments: argparse.Namespace, contract: ReleaseContract) -> None:
    plan = load_plan(arguments.plan, contract)
    for spec in plan.validations:
        print(spec.validation_id)


def _aggregate_command(arguments: argparse.Namespace, contract: ReleaseContract) -> None:
    plan = load_plan(arguments.plan, contract)
    refreshed_release = hosted.parse_draft_release(
        read_stable_text(
            arguments.fresh_release_json,
            label="fresh draft release JSON",
        ),
        expected_tag=plan.release.tag,
    )
    receipt = hosted.create_bound_receipt(
        plan,
        refreshed_release=refreshed_release,
        results=load_exact_results(arguments.results_directory, plan),
        contract=contract,
    )
    write_json_no_replace(
        arguments.output,
        hosted.receipt_payload(receipt),
        mode=0o644,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--repository", required=True)
    plan.add_argument("--source-workflow-id", type=int, required=True)
    plan.add_argument("--source-run-id", type=int, required=True)
    plan.add_argument("--source-run-attempt", type=int, required=True)
    plan.add_argument("--source-run-json", type=Path, required=True)
    plan.add_argument("--validation-workflow-id", type=int, required=True)
    plan.add_argument("--validation-run-id", type=int, required=True)
    plan.add_argument("--validation-run-attempt", type=int, required=True)
    plan.add_argument("--tag", required=True)
    plan.add_argument("--head-sha", required=True)
    plan.add_argument("--release-id", required=True)
    plan.add_argument("--release-database-id", type=int, required=True)
    plan.add_argument("--release-json", type=Path, required=True)
    plan.add_argument("--notes-file", type=Path, required=True)
    plan.add_argument("--identity-sha1", required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--github-output", type=Path, required=True)

    download = subparsers.add_parser("download")
    download.add_argument("--plan", type=Path, required=True)
    download.add_argument("--validation-id", required=True)
    download.add_argument("--output-directory", type=Path, required=True)

    validate = subparsers.add_parser("validate-target")
    validate.add_argument("--plugin-root", type=Path, default=Path("."))
    validate.add_argument("--plan", type=Path, required=True)
    validate.add_argument("--validation-id", required=True)
    validate.add_argument("--archive", type=Path, required=True)
    validate.add_argument("--output", type=Path, required=True)

    aggregate = subparsers.add_parser("aggregate")
    aggregate.add_argument("--plan", type=Path, required=True)
    aggregate.add_argument("--fresh-release-json", type=Path, required=True)
    aggregate.add_argument("--results-directory", type=Path, required=True)
    aggregate.add_argument("--output", type=Path, required=True)

    list_ids = subparsers.add_parser("list-validation-ids")
    list_ids.add_argument("--plan", type=Path, required=True)
    return parser


def main() -> None:
    arguments = _build_parser().parse_args()
    try:
        try:
            contract = load_contract(arguments.contract)
        except RuntimeError as error:
            raise HostedValidationRunnerError(
                f"release target contract is invalid: {error}"
            ) from error
        if arguments.command == "plan":
            _plan_command(arguments, contract)
        elif arguments.command == "download":
            _download_command(arguments, contract)
        elif arguments.command == "validate-target":
            _validate_command(arguments, contract)
        elif arguments.command == "aggregate":
            _aggregate_command(arguments, contract)
        elif arguments.command == "list-validation-ids":
            _list_command(arguments, contract)
        else:  # pragma: no cover - argparse enforces the command set
            raise AssertionError(f"unhandled command: {arguments.command}")
    except (
        HostedValidationRunnerError,
        hosted.HostedValidationError,
        ReleaseNotesError,
    ) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
