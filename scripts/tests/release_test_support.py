"""Shared script-module loading for release test modules."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]


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


release_targets = load_script("release_targets", "release_targets.py")
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
check_codex_plugin_install = load_script(
    "check_codex_plugin_install", "check-codex-plugin-install.py"
)
smoke_installed_plugin = load_script(
    "smoke_installed_plugin", "smoke-installed-plugin.py"
)
