"""Core finalization policy and CLI-boundary tests."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
from unittest import mock

from finalization_test_support import FinalizationTestCase
import finalize_macos_release
import release_finalization_core as core


class ReleaseFinalizationCoreTests(FinalizationTestCase):
    def test_receipt_verification_mode_never_creates_missing_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-receipt-mode-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "state.json"

            with self.assertRaisesRegex(
                core.FinalizationError,
                "required existing receipt is missing",
            ):
                core.ensure_receipt(
                    root,
                    path.name,
                    {"state": "sealed"},
                    create_missing=False,
                )
            self.assertFalse(path.exists())

            core.ensure_receipt(root, path.name, {"state": "sealed"})
            before = path.read_bytes()
            core.ensure_receipt(
                root,
                path.name,
                {"state": "sealed"},
                create_missing=False,
            )
            self.assertEqual(path.read_bytes(), before)

    def test_finalization_phase_contract_rejects_unknown_values(self) -> None:
        for phase in core.FINALIZATION_PHASES:
            with self.subTest(phase=phase):
                self.assertEqual(core.validate_finalization_phase(phase), phase)

        with self.assertRaisesRegex(
            core.FinalizationError,
            "unsupported finalization stop phase",
        ):
            core.validate_finalization_phase("invalid")

    def test_attempt_root_inside_checkout_is_rejected_before_any_creation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finalizer-attempt-location-") as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            plugin.mkdir()
            nested = plugin / "release-attempt"
            alias = root / "plugin-alias"
            alias.symlink_to(plugin, target_is_directory=True)

            for candidate in (plugin, nested, alias / "nested-attempt"):
                with self.subTest(candidate=candidate):
                    options = self.options(plugin, candidate)
                    with mock.patch.object(
                        finalize_macos_release.source,
                        "validate_checkout",
                    ) as checkout, mock.patch.object(
                        finalize_macos_release,
                        "initialize_or_resume",
                    ) as initialize:
                        with self.assertRaisesRegex(
                            core.FinalizationError,
                            "outside the source checkout",
                        ):
                            finalize_macos_release.finalize(options)
                    checkout.assert_not_called()
                    initialize.assert_not_called()

            self.assertFalse(nested.exists())
            outside = root / "release-attempt"
            self.assertEqual(
                core.validate_attempt_root_location(plugin, outside),
                outside,
            )

    def test_hosted_cli_binding_is_all_or_none(self) -> None:
        empty = argparse.Namespace(
            hosted_workflow_id=None,
            hosted_run_id=None,
            hosted_run_attempt=None,
            hosted_receipt_artifact_id=None,
        )
        self.assertIsNone(finalize_macos_release._hosted_validation_input(empty))
        partial = argparse.Namespace(
            hosted_workflow_id=1,
            hosted_run_id=None,
            hosted_run_attempt=1,
            hosted_receipt_artifact_id=2,
        )
        with self.assertRaisesRegex(
            core.FinalizationError,
            "supplied together",
        ):
            finalize_macos_release._hosted_validation_input(partial)
