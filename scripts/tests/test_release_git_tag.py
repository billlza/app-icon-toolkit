from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_git_tag


TAG = "v1.2.3"
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
TAG_SHA = "89abcdef0123456789abcdef0123456789abcdef"


def ref_json(**changes: object) -> str:
    value: dict[str, object] = {
        "ref": f"refs/tags/{TAG}",
        "object": {"type": "tag", "sha": TAG_SHA},
    }
    value.update(changes)
    return json.dumps(value)


def tag_json(**changes: object) -> str:
    value: dict[str, object] = {
        "sha": TAG_SHA,
        "tag": TAG,
        "object": {"type": "commit", "sha": COMMIT_SHA},
    }
    value.update(changes)
    return json.dumps(value)


class ReleaseGitTagTests(unittest.TestCase):
    def test_exact_annotated_tag_binding_is_accepted(self) -> None:
        binding = release_git_tag.parse_remote_annotated_tag(
            ref_json(),
            tag_json(),
            expected_tag=TAG,
            expected_commit_sha=COMMIT_SHA,
            expected_local_tag_object_sha=TAG_SHA,
        )
        self.assertEqual(binding.tag_object_sha, TAG_SHA)
        self.assertEqual(binding.commit_sha, COMMIT_SHA)

    def test_lightweight_moved_or_indirect_tag_is_rejected(self) -> None:
        cases = (
            (
                ref_json(object={"type": "commit", "sha": COMMIT_SHA}),
                tag_json(),
                "annotated",
            ),
            (
                ref_json(object={"type": "tag", "sha": "f" * 40}),
                tag_json(),
                "local and remote",
            ),
            (
                ref_json(),
                tag_json(object={"type": "commit", "sha": "f" * 40}),
                "wrong release commit",
            ),
            (
                ref_json(),
                tag_json(object={"type": "tag", "sha": COMMIT_SHA}),
                "directly to a commit",
            ),
        )
        for ref_payload, tag_payload, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                release_git_tag.ReleaseTagError,
                message,
            ):
                release_git_tag.parse_remote_annotated_tag(
                    ref_payload,
                    tag_payload,
                    expected_tag=TAG,
                    expected_commit_sha=COMMIT_SHA,
                    expected_local_tag_object_sha=TAG_SHA,
                )

    def test_duplicate_json_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(release_git_tag.ReleaseTagError, "repeats"):
            release_git_tag.parse_remote_annotated_tag(
                '{"ref":"refs/tags/v1.2.3","ref":"refs/tags/v1.2.3",'
                '"object":{"type":"tag","sha":"89abcdef0123456789abcdef0123456789abcdef"}}',
                tag_json(),
                expected_tag=TAG,
                expected_commit_sha=COMMIT_SHA,
                expected_local_tag_object_sha=TAG_SHA,
            )


if __name__ == "__main__":
    unittest.main()
