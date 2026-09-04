from __future__ import annotations

from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import release_targets


class DocumentationContractTests(unittest.TestCase):
    def test_macos_installation_matches_zip_signing_contract(self) -> None:
        contract = release_targets.load_contract()
        self.assertTrue(
            all(
                target.archive_format == "zip"
                for target in contract.targets
                if target.family in {"macos", "macos_universal2"}
            )
        )
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        install = (REPOSITORY_ROOT / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("v0.2.5 and later use Developer ID signing", readme)
        self.assertIn("ditto -x -k", install)
        self.assertIn("cannot carry a stapled ticket", install)
        self.assertNotIn(
            "Current macOS release binaries are unsigned and not notarized",
            install,
        )

    def test_release_security_boundary_is_documented(self) -> None:
        security = (REPOSITORY_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        contributing = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(
            encoding="utf-8"
        )
        normalized_security = " ".join(security.split())
        normalized_contributing = " ".join(contributing.split())
        self.assertIn(
            "the signing process never starts a candidate executable",
            normalized_security,
        )
        self.assertIn("must not execute downloaded candidates", normalized_contributing)
        self.assertIn("immutable publication", normalized_contributing)

    def test_installation_and_upgrade_are_versioned_and_fail_closed(self) -> None:
        install = (REPOSITORY_ROOT / "INSTALL.md").read_text(encoding="utf-8")
        normalized = " ".join(install.split())

        self.assertIn("test ! -e \"$install_root\"", install)
        self.assertIn("Test-Path -LiteralPath $Extracted", install)
        self.assertIn("Never extract a new release over an older", normalized)
        self.assertIn("codex mcp get app-icon-toolkit --json", install)
        self.assertIn(
            "codex plugin remove app-icon-toolkit@app-icon-toolkit", install
        )
        self.assertIn(
            "codex plugin marketplace remove app-icon-toolkit", install
        )
        self.assertIn("app-icon-toolkit@personal", install)
        self.assertIn(
            "must not be removed by these steps", normalized
        )
        self.assertIn(
            "Delete the old extracted directory only after", normalized
        )

    def test_release_runbook_matches_the_staged_release_flow(self) -> None:
        releasing = (REPOSITORY_ROOT / "RELEASING.md").read_text(encoding="utf-8")
        normalized = " ".join(releasing.split())

        self.assertIn(
            "creates or reconciles only an empty Draft",
            normalized,
        )
        self.assertIn("does not publish the Draft", normalized)
        for phase in ("prepare", "notarize", "stage", "publish"):
            with self.subTest(phase=phase):
                self.assertIn(f"--stop-after {phase}", releasing)

        self.assertIn('  --ref "$RELEASE_TAG"', releasing)
        self.assertIn("Validate Signed Draft", releasing)
        self.assertIn("HOSTED_WORKFLOW_ID", releasing)
        self.assertIn("HOSTED_RUN_ID", releasing)
        self.assertIn("HOSTED_RUN_ATTEMPT", releasing)
        self.assertIn("HOSTED_RECEIPT_ARTIFACT_ID", releasing)
        self.assertIn("positive numeric ID, size, and SHA-256 digest", normalized)
        self.assertIn("--hosted-receipt-artifact-id", releasing)

        self.assertIn("only **Re-run all jobs** is allowed", releasing)
        self.assertIn("never select “Re-run failed jobs”", normalized)
        self.assertIn("The first response to every UNKNOWN outcome is read-only", normalized)
        self.assertIn("Neither flag is a blind retry", normalized)

        self.assertIn("must already be enabled", normalized)
        self.assertIn(
            "contains no command that changes the repository setting",
            normalized,
        )
        self.assertNotIn("--method PATCH", releasing)

        self.assertIn("must never execute a downloaded candidate", normalized)
        self.assertIn("It never executes a published candidate", normalized)
        self.assertNotIn("smoke-installed-plugin.py", releasing)
        self.assertIn("PUBLIC_BUT_UNVERIFIED", releasing)
        self.assertIn("public-verified.json", releasing)
        self.assertIn("final phase is `public-verified`", normalized)
        self.assertIn("It performs no new GitHub mutation", normalized)

        self.assertNotIn("GH_TOKEN=", releasing)
        self.assertNotIn("GITHUB_TOKEN=", releasing)


if __name__ == "__main__":
    unittest.main()
