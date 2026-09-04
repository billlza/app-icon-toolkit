"""Shared script-module loading for release test modules."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import unittest


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]


def create_symlink_or_skip(
    testcase: unittest.TestCase,
    link: Path,
    target: Path,
) -> None:
    """Create a test symlink or skip only when Windows lacks that capability."""

    try:
        link.symlink_to(target)
    except NotImplementedError as error:
        if os.name == "nt":
            testcase.skipTest(
                f"Windows symbolic-link creation is unsupported: {error}"
            )
        raise
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            testcase.skipTest(
                "Windows symbolic-link creation requires an unavailable privilege"
            )
        raise


def load_script(module_name: str, filename: str):
    specification = importlib.util.spec_from_file_location(
        module_name, SCRIPTS_ROOT / filename
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {filename}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


release_files = load_script("release_files", "release_files.py")
release_targets = load_script("release_targets", "release_targets.py")
release_zip_preflight = load_script(
    "release_zip_preflight",
    "release_zip_preflight.py",
)
release_package = load_script("release_package", "release_package.py")
prepare_release_package = load_script(
    "prepare_release_package", "prepare-release-package.py"
)
extract_release_package = load_script(
    "extract_release_package", "extract-release-package.py"
)
check_release_binary = load_script(
    "check_release_binary", "check-release-binary.py"
)
build_release_binary = load_script(
    "build_release_binary", "build-release-binary.py"
)
install_release_toolchain = load_script(
    "install_release_toolchain", "install-release-toolchain.py"
)
check_release_version = load_script(
    "check_release_version", "check-release-version.py"
)
stage_release_draft = load_script(
    "stage_release_draft", "stage-release-draft.py"
)
validate_signed_draft = load_script(
    "validate_signed_draft", "validate-signed-draft.py"
)
check_codex_plugin_install = load_script(
    "check_codex_plugin_install", "check-codex-plugin-install.py"
)
smoke_installed_plugin = load_script(
    "smoke_installed_plugin", "smoke-installed-plugin.py"
)
