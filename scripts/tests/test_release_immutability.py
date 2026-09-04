from __future__ import annotations

from pathlib import Path
import sys
import unittest


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_immutability


class ReleaseImmutabilityTests(unittest.TestCase):
    def test_enabled_policy_is_an_explicit_capability(self) -> None:
        policy = release_immutability.parse_release_immutability_policy(
            '{"enabled":true,"enforced_by_owner":false}'
        )
        self.assertTrue(policy.enabled)
        self.assertFalse(policy.enforced_by_owner)

    def test_disabled_malformed_or_ambiguous_policy_fails_closed(self) -> None:
        payloads = (
            '{"enabled":false,"enforced_by_owner":false}',
            '{"enabled":true,"enforced_by_owner":0}',
            '{"enabled":true}',
            '{"enabled":true,"enforced_by_owner":false,"extra":false}',
            '{"enabled":true,"enabled":true,"enforced_by_owner":false}',
            'null',
            '',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(
                    release_immutability.ReleaseImmutabilityError
                ):
                    release_immutability.parse_release_immutability_policy(payload)


if __name__ == "__main__":
    unittest.main()
