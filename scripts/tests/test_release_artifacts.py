from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_artifacts
import release_draft


RUN = release_draft.WorkflowRun(
    workflow_id=700,
    run_id=800,
    attempt=2,
    tag="v1.2.3",
    head_sha="0123456789abcdef0123456789abcdef01234567",
)
EXPECTED = (
    "app-icon-toolkit-aarch64-apple-darwin-attempt-2",
    "app-icon-toolkit-linux-attempt-2",
)


def artifact(artifact_id: int, name: str, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": artifact_id,
        "name": name,
        "size_in_bytes": 1234,
        "digest": f"sha256:{artifact_id:064x}",
        "expired": False,
        "created_at": "2026-09-04T00:00:00Z",
        "updated_at": "2026-09-04T00:00:00Z",
        "workflow_run": {
            "id": RUN.run_id,
            "head_branch": RUN.tag,
            "head_sha": RUN.head_sha,
            "repository_id": 99,
            "head_repository_id": 99,
        },
    }
    value.update(changes)
    return value


def inventory(artifacts: list[dict[str, object]]) -> str:
    return json.dumps({"total_count": len(artifacts), "artifacts": artifacts})


class ArtifactInventoryTests(unittest.TestCase):
    def test_exact_inventory_is_sorted_and_bound_to_the_run(self) -> None:
        records = release_artifacts.parse_artifact_inventory(
            inventory(
                [
                    artifact(2, EXPECTED[1]),
                    artifact(1, EXPECTED[0]),
                ]
            ),
            run=RUN,
            expected_names=EXPECTED,
        )
        self.assertEqual([record.name for record in records], list(EXPECTED))
        self.assertEqual([record.artifact_id for record in records], [1, 2])

    def test_prior_attempt_artifacts_are_validated_but_never_selected(self) -> None:
        records = release_artifacts.parse_artifact_inventory(
            inventory(
                [
                    artifact(10, "app-icon-toolkit-aarch64-apple-darwin-attempt-1"),
                    artifact(11, "app-icon-toolkit-linux-attempt-1"),
                    artifact(2, EXPECTED[1]),
                    artifact(1, EXPECTED[0]),
                ]
            ),
            run=RUN,
            expected_names=EXPECTED,
        )
        self.assertEqual([record.name for record in records], list(EXPECTED))
        self.assertEqual([record.artifact_id for record in records], [1, 2])

    def test_inventory_rejects_run_binding_or_expiry_drift(self) -> None:
        cases = (
            (
                artifact(
                    1,
                    EXPECTED[0],
                    workflow_run={
                        "id": RUN.run_id + 1,
                        "head_branch": RUN.tag,
                        "head_sha": RUN.head_sha,
                        "repository_id": 99,
                        "head_repository_id": 99,
                    },
                ),
                "run id",
            ),
            (artifact(1, EXPECTED[0], expired=True), "expired"),
            (
                artifact(
                    1,
                    EXPECTED[0],
                    workflow_run={
                        "id": RUN.run_id,
                        "head_branch": RUN.tag,
                        "head_sha": "f" * 40,
                        "repository_id": 99,
                        "head_repository_id": 99,
                    },
                ),
                "head SHA",
            ),
        )
        for changed, message in cases:
            with self.subTest(message=message):
                payload = inventory([changed, artifact(2, EXPECTED[1])])
                with self.assertRaisesRegex(
                    release_artifacts.ReleaseArtifactError,
                    message,
                ):
                    release_artifacts.parse_artifact_inventory(
                        payload,
                        run=RUN,
                        expected_names=EXPECTED,
                    )

    def test_inventory_rejects_missing_extra_and_duplicate_identity(self) -> None:
        cases = (
            ([artifact(1, EXPECTED[0])], "name mismatch"),
            (
                [
                    artifact(1, EXPECTED[0]),
                    artifact(2, "unexpected"),
                ],
                "allowlist",
            ),
            (
                [artifact(1, EXPECTED[0]), artifact(1, EXPECTED[1])],
                "duplicate",
            ),
        )
        for artifacts, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    release_artifacts.ReleaseArtifactError,
                    message,
                ):
                    release_artifacts.parse_artifact_inventory(
                        inventory(artifacts),
                        run=RUN,
                        expected_names=EXPECTED,
                    )

    def test_inventory_rejects_count_and_duplicate_json_keys(self) -> None:
        mismatch = json.dumps(
            {
                "total_count": 3,
                "artifacts": [artifact(1, EXPECTED[0]), artifact(2, EXPECTED[1])],
            }
        )
        with self.assertRaisesRegex(
            release_artifacts.ReleaseArtifactError,
            "total_count",
        ):
            release_artifacts.parse_artifact_inventory(
                mismatch,
                run=RUN,
                expected_names=EXPECTED,
            )

        with self.assertRaisesRegex(
            release_artifacts.ReleaseArtifactError,
            "repeats JSON key",
        ):
            release_artifacts.parse_artifact_inventory(
                '{"total_count":2,"total_count":2,"artifacts":[]}',
                run=RUN,
                expected_names=EXPECTED,
            )

    def test_inventory_requires_bounded_api_sha256_digest(self) -> None:
        for digest, message in (
            (None, "bounded non-empty string"),
            ("sha256:not-a-digest", "exact SHA-256"),
            (f"sha256:{'A' * 64}", "exact SHA-256"),
        ):
            with self.subTest(digest=digest):
                changed = artifact(1, EXPECTED[0], digest=digest)
                with self.assertRaisesRegex(
                    release_artifacts.ReleaseArtifactError,
                    message,
                ):
                    release_artifacts.parse_artifact_inventory(
                        inventory([changed, artifact(2, EXPECTED[1])]),
                        run=RUN,
                        expected_names=EXPECTED,
                    )

        oversized = artifact(
            1,
            EXPECTED[0],
            size_in_bytes=release_artifacts.MAX_ARTIFACT_ARCHIVE_BYTES + 1,
        )
        with self.assertRaisesRegex(
            release_artifacts.ReleaseArtifactError,
            "artifact archive limit",
        ):
            release_artifacts.parse_artifact_inventory(
                inventory([oversized, artifact(2, EXPECTED[1])]),
                run=RUN,
                expected_names=EXPECTED,
            )

    def test_inventory_rejects_future_and_malformed_attempt_names(self) -> None:
        for name in (
            "app-icon-toolkit-aarch64-apple-darwin-attempt-3",
            "app-icon-toolkit-aarch64-apple-darwin-attempt-02",
            "app-icon-toolkit-aarch64-apple-darwin-attempt-zero",
        ):
            with self.subTest(name=name):
                with self.assertRaises(release_artifacts.ReleaseArtifactError):
                    release_artifacts.parse_artifact_inventory(
                        inventory([artifact(1, name), artifact(2, EXPECTED[1])]),
                        run=RUN,
                        expected_names=EXPECTED,
                    )


if __name__ == "__main__":
    unittest.main()
