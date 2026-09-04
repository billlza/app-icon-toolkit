from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from release_test_support import SCRIPTS_ROOT


class LocalBuildScriptTests(unittest.TestCase):
    @staticmethod
    def write_fake_cargo(fake_bin: Path) -> None:
        if os.name == "nt":
            cargo = fake_bin / "cargo.ps1"
            cargo.write_text(
                """$targetIndex = [Array]::IndexOf($args, "--target-dir")
if ($targetIndex -lt 0 -or $targetIndex + 1 -ge $args.Count) {
    throw "missing --target-dir"
}
$targetDirectory = $args[$targetIndex + 1]
[IO.File]::WriteAllText($env:FAKE_CARGO_LOG, $targetDirectory)
if (-not [string]::IsNullOrEmpty($env:FAKE_CARGO_FAIL)) {
    exit [int]$env:FAKE_CARGO_FAIL
}
$releaseDirectory = Join-Path $targetDirectory "release"
$binary = Join-Path $releaseDirectory "app-icon-toolkit-mcp.exe"
New-Item -ItemType Directory -Force -Path $releaseDirectory | Out-Null
switch ($env:FAKE_CARGO_ARTIFACT) {
    "missing" {}
    "empty" { [IO.File]::WriteAllBytes($binary, [byte[]]@()) }
    "directory" { New-Item -ItemType Directory -Path $binary | Out-Null }
    default {
        [IO.File]::WriteAllText(
            $binary,
            "fresh-build",
            [Text.UTF8Encoding]::new($false)
        )
    }
}
exit 0
""",
                encoding="utf-8",
            )
            return

        cargo = fake_bin / "cargo"
        cargo.write_text(
            """#!/bin/sh
set -eu

target_directory=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '--target-dir' ]; then
    shift
    target_directory=$1
  fi
  shift
done
test -n "$target_directory"
printf '%s' "$target_directory" > "$FAKE_CARGO_LOG"
if [ -n "${FAKE_CARGO_FAIL:-}" ]; then
  exit "$FAKE_CARGO_FAIL"
fi
release_directory="$target_directory/release"
binary="$release_directory/app-icon-toolkit-mcp"
mkdir -p "$release_directory"
case "${FAKE_CARGO_ARTIFACT:-normal}" in
  normal) printf '%s' 'fresh-build' > "$binary" ;;
  missing) ;;
  empty) : > "$binary" ;;
  directory) mkdir "$binary" ;;
  symlink)
    printf '%s' 'link-target' > "$release_directory/link-target"
    ln -s link-target "$binary"
    ;;
  *) exit 65 ;;
esac
""",
            encoding="utf-8",
        )
        cargo.chmod(0o755)

    def test_local_build_isolated_artifacts_and_fail_closed_install(self) -> None:
        cases: list[tuple[str, int | None, bool, str, bool]] = [
            ("success", None, False, "normal", True),
            ("cargo failure", 42, False, "normal", False),
            ("unexpected argument", None, True, "normal", False),
            ("missing artifact", None, False, "missing", False),
            ("empty artifact", None, False, "empty", False),
            ("directory artifact", None, False, "directory", False),
        ]
        if os.name != "nt":
            cases.append(("symlink artifact", None, False, "symlink", False))

        for label, fail_code, unexpected_argument, artifact, should_install in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(prefix="local-build-test-") as temporary:
                    plugin_root = Path(temporary) / "plugin"
                    scripts = plugin_root / "scripts"
                    fake_bin = Path(temporary) / "fake-bin"
                    scripts.mkdir(parents=True)
                    fake_bin.mkdir()
                    (plugin_root / "Cargo.toml").write_text(
                        "[workspace]\n",
                        encoding="utf-8",
                    )

                    script_name = (
                        "build-local.ps1" if os.name == "nt" else "build-local.sh"
                    )
                    build_script = scripts / script_name
                    build_script.write_bytes((SCRIPTS_ROOT / script_name).read_bytes())
                    if os.name != "nt":
                        build_script.chmod(0o755)
                    self.write_fake_cargo(fake_bin)

                    shared_release = plugin_root / "target" / "release"
                    shared_release.mkdir(parents=True)
                    binary_name = (
                        "app-icon-toolkit-mcp.exe"
                        if os.name == "nt"
                        else "app-icon-toolkit-mcp"
                    )
                    (shared_release / binary_name).write_bytes(b"stale-build")
                    cargo_log = Path(temporary) / "cargo-target.txt"
                    environment = os.environ.copy()
                    environment["PATH"] = (
                        str(fake_bin) + os.pathsep + environment.get("PATH", "")
                    )
                    environment["FAKE_CARGO_LOG"] = str(cargo_log)
                    environment["FAKE_CARGO_ARTIFACT"] = artifact
                    if fail_code is not None:
                        environment["FAKE_CARGO_FAIL"] = str(fail_code)

                    command = (
                        [
                            "pwsh",
                            "-NoProfile",
                            "-NonInteractive",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(build_script),
                        ]
                        if os.name == "nt"
                        else [str(build_script)]
                    )
                    if unexpected_argument:
                        command.append("--unexpected")
                    completed = subprocess.run(
                        command,
                        cwd=plugin_root,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    installed_binary = plugin_root / "bin" / binary_name
                    if unexpected_argument:
                        self.assertNotEqual(completed.returncode, 0)
                        self.assertFalse(cargo_log.exists())
                        self.assertFalse(installed_binary.exists())
                        continue

                    isolated_target = Path(cargo_log.read_text(encoding="utf-8"))
                    self.assertEqual(isolated_target.parent, plugin_root / "target")
                    self.assertTrue(isolated_target.name.startswith("build-local"))
                    self.assertFalse(isolated_target.exists())
                    self.assertEqual(
                        (shared_release / binary_name).read_bytes(),
                        b"stale-build",
                    )
                    if should_install:
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        self.assertEqual(installed_binary.read_bytes(), b"fresh-build")
                    else:
                        self.assertNotEqual(completed.returncode, 0)
                        self.assertFalse(installed_binary.exists())


if __name__ == "__main__":
    unittest.main()
