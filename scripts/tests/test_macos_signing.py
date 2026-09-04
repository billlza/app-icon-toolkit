from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from release_test_support import create_symlink_or_skip, load_script


macos_signing = load_script("macos_signing", "macos_signing.py")

IDENTITY = "2DA7764ED42B213AE04925B6261238B24C758FE1"
TEAM = "YKUPL7Z869"
IDENTIFIER = "io.github.billlza.app-icon-toolkit.mcp"
LEAF = f"Developer ID Application: Release Owner ({TEAM})"
JOB_ID = "12345678-1234-4234-8234-123456789abc"
CDHASH = "0123456789abcdef0123456789abcdef01234567"


class ScriptedRunner:
    """Strict mock command boundary that also supports filesystem side effects."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []

    def run(self, argv):
        if not self.steps:
            raise AssertionError(f"unexpected command: {argv!r}")
        expected, returncode, stdout, stderr, effect = self.steps.pop(0)
        if argv != expected:
            raise AssertionError(f"command differs:\nexpected={expected!r}\nactual={argv!r}")
        self.seen.append(argv)
        if effect is not None:
            effect()
        return macos_signing.CommandResult(
            argv=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def assert_finished(self, testcase):
        testcase.assertEqual(self.steps, [])


def step(argv, *, returncode=0, stdout="", stderr="", effect=None):
    return (tuple(argv), returncode, stdout, stderr, effect)


def absolute(path: Path) -> str:
    return os.path.abspath(path)


def lipo_command(path: Path):
    return (macos_signing.XCRUN, "lipo", "-archs", absolute(path))


def display_command(path: Path, architecture: str):
    return (
        macos_signing.CODESIGN,
        "--display",
        "--verbose=4",
        "--arch",
        architecture,
        absolute(path),
    )


def requirement_command(path: Path, architecture: str):
    return (
        macos_signing.CODESIGN,
        "--display",
        "--requirements",
        "-",
        "--arch",
        architecture,
        absolute(path),
    )


def entitlement_command(path: Path, architecture: str):
    return (
        macos_signing.CODESIGN,
        "--display",
        "--entitlements",
        "-",
        "--xml",
        "--arch",
        architecture,
        absolute(path),
    )


def sign_command(path: Path):
    return (
        macos_signing.CODESIGN,
        "--force",
        "--sign",
        IDENTITY,
        "--identifier",
        IDENTIFIER,
        "--options",
        "runtime",
        "--timestamp",
        absolute(path),
    )


def strict_command(path: Path):
    return (
        macos_signing.CODESIGN,
        "--verify",
        "--strict",
        "--all-architectures",
        "--verbose=4",
        absolute(path),
    )


def leaf_certificate_command(path: Path, architecture: str):
    return (
        macos_signing.CODESIGN,
        "--verify",
        "--strict",
        "--arch",
        architecture,
        f'-R=certificate leaf = H"{IDENTITY}"',
        absolute(path),
    )


def identity_step():
    return step(
        (macos_signing.SECURITY, "find-identity", "-v", "-p", "codesigning"),
        stdout=f'  1) {IDENTITY} "{LEAF}"\n     1 valid identities found\n',
    )


def adhoc_output(path: Path, architecture: str) -> str:
    return "\n".join(
        (
            f"Executable={absolute(path)}",
            f"Identifier=linker-id-{architecture}",
            "CodeDirectory v=20400 size=100 "
            "flags=0x20002(adhoc,linker-signed) hashes=2+0 location=embedded",
            f"CDHash={CDHASH}",
            "Signature=adhoc",
            "TeamIdentifier=not set",
            "Internal requirements=none",
        )
    )


def developer_output(
    path: Path,
    architecture: str,
    *,
    leaf: str = LEAF,
    team: str = TEAM,
    identifier: str = IDENTIFIER,
    flags: str = "runtime",
    timestamp: str = "4 Sep 2026 at 08:00:00",
    cdhash: str = CDHASH,
) -> str:
    return "\n".join(
        (
            f"Executable={absolute(path)}",
            f"Identifier={identifier}",
            f"Format=Mach-O thin ({architecture})",
            f"CodeDirectory v=20500 size=100 flags=0x10000({flags}) hashes=2+0 location=embedded",
            f"CDHash={cdhash}",
            "Signature size=9000",
            f"Authority={leaf}",
            "Authority=Developer ID Certification Authority G2",
            "Authority=Apple Root CA",
            f"Timestamp={timestamp}",
            f"TeamIdentifier={team}",
        )
    )


def designated_requirement(identifier: str = IDENTIFIER, team: str = TEAM) -> str:
    return (
        f'designated => identifier "{identifier}" and anchor apple generic '
        "and certificate 1[field.1.2.840.113635.100.6.2.6] exists "
        "and certificate leaf[field.1.2.840.113635.100.6.1.13] exists "
        f'and certificate leaf[subject.OU] = "{team}"\n'
    )


def successful_sign_steps(
    path: Path,
    *,
    architectures=("arm64",),
    post_display=None,
    requirement=None,
    entitlement_stdout="",
    entitlement_stderr=None,
    strict_returncode=0,
):
    architecture_text = " ".join(architectures) + "\n"
    steps = [step(lipo_command(path), stdout=architecture_text)]
    steps.extend(
        step(display_command(path, architecture), stderr=adhoc_output(path, architecture))
        for architecture in architectures
    )
    steps.extend(
        step(
            entitlement_command(path, architecture),
            stderr=f"Executable={absolute(path)}\n",
        )
        for architecture in architectures
    )
    steps.append(identity_step())

    def apply_signature():
        with path.open("ab") as binary:
            binary.write(b"-developer-id-signature")

    steps.append(step(sign_command(path), effect=apply_signature))
    steps.append(step(lipo_command(path), stdout=architecture_text))
    steps.append(
        step(
            strict_command(path),
            returncode=strict_returncode,
            stderr="invalid" if strict_returncode else "",
        )
    )
    if strict_returncode:
        return steps
    for architecture in architectures:
        display = (
            developer_output(path, architecture)
            if post_display is None
            else post_display(path, architecture)
        )
        requirements = designated_requirement() if requirement is None else requirement
        entitlement_diagnostic = (
            f"Executable={absolute(path)}\n"
            if entitlement_stderr is None
            else entitlement_stderr
        )
        steps.extend(
            (
                step(leaf_certificate_command(path, architecture)),
                step(display_command(path, architecture), stderr=display),
                step(requirement_command(path, architecture), stderr=requirements),
                step(
                    entitlement_command(path, architecture),
                    stdout=entitlement_stdout,
                    stderr=entitlement_diagnostic,
                ),
            )
        )
    return steps


class FileBoundaryTests(unittest.TestCase):
    def test_sha256_is_streamed_and_matches_reference(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-hash-") as temporary:
            path = Path(temporary) / "archive.zip"
            content = b"0123456789abcdef"
            path.write_bytes(content)

            actual = macos_signing.sha256_file(path, chunk_size=3)

        self.assertEqual(actual, hashlib.sha256(content).hexdigest())

    def test_rejects_symlink(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-input-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"binary")
            symlink = root / "symlink"
            create_symlink_or_skip(self, symlink, source)
            with self.assertRaisesRegex(macos_signing.InputValidationError, "non-symlink"):
                macos_signing.validate_regular_single_link(symlink, label="input")

    def test_rejects_hardlink_directory_and_empty_file(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-input-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"binary")
            hardlink = root / "hardlink"
            os.link(source, hardlink)
            with self.assertRaisesRegex(
                macos_signing.InputValidationError, "exactly one hard link"
            ):
                macos_signing.validate_regular_single_link(source, label="input")

            with self.assertRaisesRegex(macos_signing.InputValidationError, "ordinary"):
                macos_signing.validate_regular_single_link(root, label="input")

            empty = root / "empty"
            empty.touch()
            with self.assertRaisesRegex(macos_signing.InputValidationError, "non-empty"):
                macos_signing.validate_regular_single_link(empty, label="input")

    def test_rejects_invalid_stream_chunk_size(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            macos_signing.sha256_file("unused", chunk_size=0)

    @unittest.skipIf(
        os.name == "nt",
        "Windows ctime does not provide Unix inode-change semantics",
    )
    def test_ctime_detects_same_size_rewrite_even_if_mtime_is_restored(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-ctime-") as temporary:
            path = Path(temporary) / "archive.zip"
            path.write_bytes(b"original")
            original = path.stat()
            real_sha256 = hashlib.sha256
            injected = False

            class MutatingDigest:
                def __init__(self):
                    self.digest = real_sha256()

                def update(self, chunk):
                    nonlocal injected
                    self.digest.update(chunk)
                    if injected:
                        return
                    injected = True
                    with path.open("r+b") as output:
                        output.write(b"modified")
                        output.flush()
                        os.fsync(output.fileno())
                    os.utime(
                        path,
                        ns=(original.st_atime_ns, original.st_mtime_ns),
                    )

                def hexdigest(self):
                    return self.digest.hexdigest()

            with mock.patch.object(
                macos_signing.release_files.hashlib,
                "sha256",
                side_effect=MutatingDigest,
            ):
                with self.assertRaisesRegex(
                    macos_signing.InputValidationError, "changed while it was being read"
                ):
                    macos_signing.sha256_file(path, chunk_size=3)


class CommandBoundaryTests(unittest.TestCase):
    def test_subprocess_runner_is_shell_free_and_captures_complete_result(self):
        completed = subprocess.CompletedProcess(
            args=("tool", "arg"), returncode=7, stdout="out", stderr="err"
        )
        with mock.patch.object(
            macos_signing.subprocess, "run", return_value=completed
        ) as run:
            result = macos_signing.SubprocessRunner(timeout_seconds=12).run(
                ("tool", "arg")
            )

        self.assertEqual(
            result,
            macos_signing.CommandResult(("tool", "arg"), 7, "out", "err"),
        )
        kwargs = run.call_args.kwargs
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "strict")
        self.assertEqual(kwargs["timeout"], 12)

    def test_runner_cannot_report_a_different_command(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-runner-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")

            class WrongRunner:
                def run(self, argv):
                    return macos_signing.CommandResult(("different",), 0, "arm64", "")

            with self.assertRaisesRegex(macos_signing.CommandExecutionError, "argv differs"):
                macos_signing.architectures(path, WrongRunner())

    def test_runner_output_has_a_hard_accepted_size_limit(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-output-limit-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            runner = ScriptedRunner(
                [
                    step(
                        lipo_command(path),
                        stdout="x" * (macos_signing.MAX_COMMAND_OUTPUT_BYTES + 1),
                    )
                ]
            )
            with self.assertRaisesRegex(macos_signing.CommandExecutionError, "exceeded"):
                macos_signing.architectures(path, runner)

    def test_runner_rejects_boolean_return_code(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-return-code-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")

            class BooleanRunner:
                def run(self, argv):
                    return macos_signing.CommandResult(argv, True, "arm64\n", "")

            with self.assertRaisesRegex(
                macos_signing.CommandExecutionError, "non-integer return code"
            ):
                macos_signing.architectures(path, BooleanRunner())


class PreSignPolicyTests(unittest.TestCase):
    def test_allows_unsigned_slice(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-unsigned-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            runner = ScriptedRunner(
                [
                    step(
                        display_command(path, "arm64"),
                        returncode=1,
                        stderr=f"{absolute(path)}: code object is not signed at all\n",
                    )
                ]
            )

            slices = macos_signing.inspect_pre_signatures(path, ("arm64",), runner)

            self.assertEqual(slices[0].kind, macos_signing.PreSignKind.UNSIGNED)
            runner.assert_finished(self)

    def test_allows_only_pure_ad_hoc_or_linker_signed(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-adhoc-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            runner = ScriptedRunner(
                [
                    step(
                        display_command(path, "arm64"),
                        stderr=adhoc_output(path, "arm64"),
                    ),
                    step(
                        entitlement_command(path, "arm64"),
                        stderr=f"Executable={absolute(path)}\n",
                    ),
                ]
            )

            slices = macos_signing.inspect_pre_signatures(path, ("arm64",), runner)

            self.assertEqual(slices[0].kind, macos_signing.PreSignKind.AD_HOC)

    def test_rejects_existing_developer_id_signature(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-existing-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            runner = ScriptedRunner(
                [step(display_command(path, "arm64"), stderr=developer_output(path, "arm64"))]
            )

            with self.assertRaisesRegex(
                macos_signing.SignatureValidationError, "existing non-ad-hoc"
            ):
                macos_signing.inspect_pre_signatures(path, ("arm64",), runner)

    def test_rejects_mixed_universal_signature_states(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-mixed-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            runner = ScriptedRunner(
                [
                    step(
                        display_command(path, "x86_64"),
                        returncode=1,
                        stderr=f"{absolute(path)}: code object is not signed at all\n",
                    ),
                    step(
                        display_command(path, "arm64"),
                        stderr=adhoc_output(path, "arm64"),
                    ),
                ]
            )

            with self.assertRaisesRegex(macos_signing.SignatureValidationError, "mixed"):
                macos_signing.inspect_pre_signatures(
                    path, ("x86_64", "arm64"), runner
                )

    def test_rejects_ad_hoc_signature_with_extra_flags(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-adhoc-flags-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            output = adhoc_output(path, "arm64").replace(
                "adhoc,linker-signed", "adhoc,linker-signed,runtime"
            )
            runner = ScriptedRunner([step(display_command(path, "arm64"), stderr=output)])

            with self.assertRaisesRegex(macos_signing.SignatureValidationError, "pure ad-hoc"):
                macos_signing.inspect_pre_signatures(path, ("arm64",), runner)

    def test_rejects_ad_hoc_internal_requirements_or_entitlements(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-adhoc-policy-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            requirements = adhoc_output(path, "arm64").replace(
                "Internal requirements=none", "Internal requirements=count=1 size=80"
            )
            runner = ScriptedRunner(
                [step(display_command(path, "arm64"), stderr=requirements)]
            )
            with self.assertRaisesRegex(
                macos_signing.SignatureValidationError, "internal requirements"
            ):
                macos_signing.inspect_pre_signatures(path, ("arm64",), runner)

            runner = ScriptedRunner(
                [
                    step(
                        display_command(path, "arm64"),
                        stderr=adhoc_output(path, "arm64"),
                    ),
                    step(
                        entitlement_command(path, "arm64"),
                        stdout="<plist><dict><key>unexpected</key><true/></dict></plist>",
                        stderr=f"Executable={absolute(path)}\n",
                    ),
                ]
            )
            with self.assertRaisesRegex(
                macos_signing.SignatureValidationError, "entitlements"
            ):
                macos_signing.inspect_pre_signatures(path, ("arm64",), runner)


class SigningTests(unittest.TestCase):
    def test_signs_with_exact_policy_and_verifies_each_slice(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-success-") as temporary:
            path = Path(temporary) / "app-icon-toolkit-mcp"
            path.write_bytes(b"universal-binary")
            runner = ScriptedRunner(
                successful_sign_steps(path, architectures=("x86_64", "arm64"))
            )

            receipt = macos_signing.sign_and_verify(
                path,
                expected_architectures=("x86_64", "arm64"),
                identity_sha1=IDENTITY,
                identifier=IDENTIFIER,
                team_id=TEAM,
                runner=runner,
            )

            self.assertEqual(receipt.identity_sha1, IDENTITY)
            self.assertEqual(receipt.identifier, IDENTIFIER)
            self.assertEqual(receipt.team_id, TEAM)
            self.assertEqual(receipt.architectures, ("x86_64", "arm64"))
            self.assertEqual(
                tuple(item.architecture for item in receipt.slices),
                receipt.architectures,
            )
            self.assertNotEqual(receipt.input_sha256, receipt.signed_sha256)
            self.assertIn(sign_command(path), runner.seen)
            sign_argv = sign_command(path)
            self.assertNotIn("--deep", sign_argv)
            self.assertNotIn("--entitlements", sign_argv)
            runner.assert_finished(self)

    def test_rejects_detected_architecture_that_differs_from_release_contract(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-architecture-") as temporary:
            path = Path(temporary) / "app-icon-toolkit-mcp"
            path.write_bytes(b"thin-arm-binary")
            runner = ScriptedRunner([step(lipo_command(path), stdout="arm64\n")])

            with self.assertRaisesRegex(
                macos_signing.SignatureValidationError,
                "expected.*x86_64",
            ):
                macos_signing.sign_and_verify(
                    path,
                    expected_architectures=("x86_64",),
                    identity_sha1=IDENTITY,
                    identifier=IDENTIFIER,
                    team_id=TEAM,
                    runner=runner,
                )

    def test_read_only_verify_signed_supports_crash_reconciliation(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-reconcile-") as temporary:
            path = Path(temporary) / "app-icon-toolkit-mcp"
            path.write_bytes(b"already-signed-binary")
            runner = ScriptedRunner(
                [
                    step(lipo_command(path), stdout="arm64\n"),
                    step(strict_command(path)),
                    step(leaf_certificate_command(path, "arm64")),
                    step(
                        display_command(path, "arm64"),
                        stderr=developer_output(path, "arm64"),
                    ),
                    step(
                        requirement_command(path, "arm64"),
                        stderr=designated_requirement(),
                    ),
                    step(
                        entitlement_command(path, "arm64"),
                        stderr=f"Executable={absolute(path)}\n",
                    ),
                ]
            )
            before = path.read_bytes()

            receipt = macos_signing.verify_signed(
                path,
                expected_architectures=("arm64",),
                identity_sha1=IDENTITY,
                identifier=IDENTIFIER,
                team_id=TEAM,
                runner=runner,
            )

            self.assertEqual(receipt.architectures, ("arm64",))
            self.assertEqual(receipt.signed_sha256, hashlib.sha256(before).hexdigest())
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn(sign_command(path), runner.seen)
            runner.assert_finished(self)

    def test_rejects_identity_that_is_not_exact_uppercase_sha1(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-identity-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            with self.assertRaisesRegex(macos_signing.InputValidationError, "uppercase SHA-1"):
                macos_signing.sign_and_verify(
                    path,
                    expected_architectures=("arm64",),
                    identity_sha1=IDENTITY.lower(),
                    identifier=IDENTIFIER,
                    team_id=TEAM,
                    runner=ScriptedRunner([]),
                )

    def test_rejects_keychain_leaf_outside_expected_team(self):
        runner = ScriptedRunner(
            [
                step(
                    (macos_signing.SECURITY, "find-identity", "-v", "-p", "codesigning"),
                    stdout=(
                        f'  1) {IDENTITY} '
                        '"Developer ID Application: Release Owner (OTHER12345)"\n'
                    ),
                )
            ]
        )
        with self.assertRaisesRegex(
            macos_signing.SignatureValidationError, "expected Developer ID"
        ):
            macos_signing.developer_id_leaf(IDENTITY, TEAM, runner)

    def test_invalid_identity_fails_before_codesign_mutates_input(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-preflight-") as temporary:
            path = Path(temporary) / "binary"
            original = b"unsigned-input"
            path.write_bytes(original)
            runner = ScriptedRunner(
                [
                    step(lipo_command(path), stdout="arm64\n"),
                    step(
                        display_command(path, "arm64"),
                        stderr=adhoc_output(path, "arm64"),
                    ),
                    step(
                        entitlement_command(path, "arm64"),
                        stderr=f"Executable={absolute(path)}\n",
                    ),
                    step(
                        (
                            macos_signing.SECURITY,
                            "find-identity",
                            "-v",
                            "-p",
                            "codesigning",
                        ),
                        stdout=(
                            f'  1) {IDENTITY} '
                            '"Developer ID Application: Release Owner (OTHER12345)"\n'
                        ),
                    ),
                ]
            )

            with self.assertRaisesRegex(
                macos_signing.SignatureValidationError, "expected Developer ID"
            ):
                macos_signing.sign_and_verify(
                    path,
                    expected_architectures=("arm64",),
                    identity_sha1=IDENTITY,
                    identifier=IDENTIFIER,
                    team_id=TEAM,
                    runner=runner,
                )

            self.assertEqual(path.read_bytes(), original)
            self.assertNotIn(sign_command(path), runner.seen)
            runner.assert_finished(self)

    def test_pre_sign_path_mutation_stops_before_codesign(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-race-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"original-input")

            def mutate_after_identity_lookup():
                path.write_bytes(b"replaced-input")

            runner = ScriptedRunner(
                [
                    step(lipo_command(path), stdout="arm64\n"),
                    step(
                        display_command(path, "arm64"),
                        stderr=adhoc_output(path, "arm64"),
                    ),
                    step(
                        entitlement_command(path, "arm64"),
                        stderr=f"Executable={absolute(path)}\n",
                    ),
                    step(
                        (
                            macos_signing.SECURITY,
                            "find-identity",
                            "-v",
                            "-p",
                            "codesigning",
                        ),
                        stdout=f'  1) {IDENTITY} "{LEAF}"\n',
                        effect=mutate_after_identity_lookup,
                    ),
                ]
            )

            with self.assertRaisesRegex(
                macos_signing.SignatureValidationError, "changed during pre-sign"
            ):
                macos_signing.sign_and_verify(
                    path,
                    expected_architectures=("arm64",),
                    identity_sha1=IDENTITY,
                    identifier=IDENTIFIER,
                    team_id=TEAM,
                    runner=runner,
                )

            self.assertNotIn(sign_command(path), runner.seen)
            runner.assert_finished(self)

    def test_rejects_slice_not_signed_by_exact_certificate_fingerprint(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-leaf-hash-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            steps = successful_sign_steps(path)
            leaf_index = next(
                index
                for index, entry in enumerate(steps)
                if entry[0] == leaf_certificate_command(path, "arm64")
            )
            steps[leaf_index] = step(
                leaf_certificate_command(path, "arm64"),
                returncode=1,
                stderr="explicit requirement failed",
            )
            runner = ScriptedRunner(steps)

            with self.assertRaisesRegex(
                macos_signing.CommandExecutionError, "Developer ID leaf verification"
            ):
                macos_signing.sign_and_verify(
                    path,
                    expected_architectures=("arm64",),
                    identity_sha1=IDENTITY,
                    identifier=IDENTIFIER,
                    team_id=TEAM,
                    runner=runner,
                )

    def test_strict_all_architectures_failure_stops_before_slice_parsing(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-strict-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            runner = ScriptedRunner(
                successful_sign_steps(path, strict_returncode=1)
            )

            with self.assertRaisesRegex(
                macos_signing.CommandExecutionError, "strict all-architectures"
            ):
                macos_signing.sign_and_verify(
                    path,
                    expected_architectures=("arm64",),
                    identity_sha1=IDENTITY,
                    identifier=IDENTIFIER,
                    team_id=TEAM,
                    runner=runner,
                )

    def test_rejects_wrong_post_sign_identity_metadata_and_cdhash(self):
        cases = (
            (
                "identifier",
                lambda p, a: developer_output(p, a, identifier="io.example.wrong"),
                "identifier",
            ),
            ("team", lambda p, a: developer_output(p, a, team="OTHER12345"), "Team ID"),
            (
                "leaf",
                lambda p, a: developer_output(
                    p, a, leaf=f"Developer ID Application: Other ({TEAM})"
                ),
                "leaf",
            ),
            ("runtime", lambda p, a: developer_output(p, a, flags="adhoc"), "runtime"),
            ("timestamp", lambda p, a: developer_output(p, a, timestamp="not set"), "timestamp"),
            ("cdhash", lambda p, a: developer_output(p, a, cdhash="bad"), "CDHash"),
        )
        for label, display, message in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(prefix=f"macos-signing-{label}-") as temporary:
                    path = Path(temporary) / "binary"
                    path.write_bytes(b"binary")
                    runner = ScriptedRunner(
                        successful_sign_steps(path, post_display=display)
                    )
                    with self.assertRaisesRegex(
                        macos_signing.SignatureValidationError, message
                    ):
                        macos_signing.sign_and_verify(
                            path,
                            expected_architectures=("arm64",),
                            identity_sha1=IDENTITY,
                            identifier=IDENTIFIER,
                            team_id=TEAM,
                            runner=runner,
                        )

    def test_rejects_wrong_designated_requirement(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-dr-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            runner = ScriptedRunner(
                successful_sign_steps(
                    path, requirement=designated_requirement(identifier="io.example.wrong")
                )
            )
            with self.assertRaisesRegex(
                macos_signing.SignatureValidationError, "designated requirement"
            ):
                macos_signing.sign_and_verify(
                    path,
                    expected_architectures=("arm64",),
                    identity_sha1=IDENTITY,
                    identifier=IDENTIFIER,
                    team_id=TEAM,
                    runner=runner,
                )

    def test_rejects_any_embedded_entitlements(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-entitlements-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            runner = ScriptedRunner(
                successful_sign_steps(
                    path,
                    entitlement_stdout=(
                        '<?xml version="1.0"?><plist><dict>'
                        "<key>com.apple.security.get-task-allow</key><true/>"
                        "</dict></plist>"
                    ),
                )
            )
            with self.assertRaisesRegex(macos_signing.SignatureValidationError, "entitlements"):
                macos_signing.sign_and_verify(
                    path,
                    expected_architectures=("arm64",),
                    identity_sha1=IDENTITY,
                    identifier=IDENTIFIER,
                    team_id=TEAM,
                    runner=runner,
                )

    def test_rejects_warning_even_when_codesign_exits_zero(self):
        with tempfile.TemporaryDirectory(prefix="macos-signing-warning-") as temporary:
            path = Path(temporary) / "binary"
            path.write_bytes(b"binary")
            steps = successful_sign_steps(path)
            sign_index = next(
                index
                for index, entry in enumerate(steps)
                if entry[0] == sign_command(path)
            )
            steps[sign_index] = step(
                sign_command(path),
                stderr="warning: timestamp server fallback\n",
                effect=steps[sign_index][4],
            )
            runner = ScriptedRunner(steps)
            with self.assertRaisesRegex(macos_signing.CommandExecutionError, "warning"):
                macos_signing.sign_and_verify(
                    path,
                    expected_architectures=("arm64",),
                    identity_sha1=IDENTITY,
                    identifier=IDENTIFIER,
                    team_id=TEAM,
                    runner=runner,
                )


class NotarizationTests(unittest.TestCase):
    def make_archive(self, root: str) -> Path:
        path = Path(root) / "app-icon-toolkit.zip"
        path.write_bytes(b"signed-release-archive")
        return path

    def submit_command(self, path: Path, profile="notary-profile"):
        return (
            macos_signing.XCRUN,
            "notarytool",
            "submit",
            absolute(path),
            "--keychain-profile",
            profile,
            "--no-wait",
            "--no-progress",
            "--output-format",
            "json",
        )

    def wait_command(self, profile="notary-profile", timeout="2h"):
        return (
            macos_signing.XCRUN,
            "notarytool",
            "wait",
            JOB_ID,
            "--keychain-profile",
            profile,
            "--timeout",
            timeout,
            "--no-progress",
            "--output-format",
            "json",
        )

    def info_command(self, profile="notary-profile"):
        return (
            macos_signing.XCRUN,
            "notarytool",
            "info",
            JOB_ID,
            "--keychain-profile",
            profile,
            "--no-progress",
            "--output-format",
            "json",
        )

    def log_command(self, profile="notary-profile"):
        return (
            macos_signing.XCRUN,
            "notarytool",
            "log",
            JOB_ID,
            "--keychain-profile",
            profile,
        )

    def submission(self, path: Path):
        return macos_signing.NotarySubmission(
            job_id=JOB_ID,
            archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def accepted_log(self, submission, **overrides):
        value = {
            "jobId": JOB_ID,
            "status": "Accepted",
            "statusCode": 0,
            "sha256": submission.archive_sha256,
            "issues": None,
        }
        value.update(overrides)
        return json.dumps(value)

    def test_submit_is_no_wait_and_records_only_job_and_archive_digest(self):
        with tempfile.TemporaryDirectory(prefix="macos-notary-submit-") as temporary:
            archive = self.make_archive(temporary)
            runner = ScriptedRunner(
                [
                    step(
                        self.submit_command(archive),
                        stdout=json.dumps(
                            {
                                "id": JOB_ID,
                                "message": "Successfully uploaded file",
                                "path": absolute(archive),
                            }
                        ),
                    )
                ]
            )

            submission = macos_signing.submit_notarization(
                archive, keychain_profile="notary-profile", runner=runner
            )

            self.assertEqual(submission.job_id, JOB_ID)
            self.assertEqual(
                submission.archive_sha256,
                hashlib.sha256(archive.read_bytes()).hexdigest(),
            )
            self.assertFalse(hasattr(submission, "keychain_profile"))
            runner.assert_finished(self)

    def test_submit_without_valid_job_id_is_outcome_unknown(self):
        with tempfile.TemporaryDirectory(prefix="macos-notary-unknown-") as temporary:
            archive = self.make_archive(temporary)
            for payload in ("not-json", json.dumps({"message": "uploaded"})):
                with self.subTest(payload=payload):
                    runner = ScriptedRunner(
                        [step(self.submit_command(archive), stdout=payload)]
                    )
                    with self.assertRaises(macos_signing.SubmissionOutcomeUnknown):
                        macos_signing.submit_notarization(
                            archive,
                            keychain_profile="notary-profile",
                            runner=runner,
                        )

    def test_submit_detects_archive_mutation(self):
        with tempfile.TemporaryDirectory(prefix="macos-notary-submit-mutation-") as temporary:
            archive = self.make_archive(temporary)

            def mutate():
                archive.write_bytes(b"changed-after-upload")

            runner = ScriptedRunner(
                [
                    step(
                        self.submit_command(archive),
                        stdout=json.dumps({"id": JOB_ID}),
                        effect=mutate,
                    )
                ]
            )
            with self.assertRaises(macos_signing.SubmissionOutcomeUnknown):
                macos_signing.submit_notarization(
                    archive, keychain_profile="notary-profile", runner=runner
                )

    def test_wait_info_and_log_must_independently_confirm_accepted(self):
        with tempfile.TemporaryDirectory(prefix="macos-notary-accepted-") as temporary:
            archive = self.make_archive(temporary)
            submission = self.submission(archive)
            runner = ScriptedRunner(
                [
                    step(
                        self.wait_command(),
                        stdout=json.dumps({"id": JOB_ID, "status": "Accepted"}),
                    ),
                    step(
                        self.info_command(),
                        stdout=json.dumps({"id": JOB_ID, "status": "Accepted"}),
                    ),
                    step(self.log_command(), stdout=self.accepted_log(submission)),
                ]
            )

            receipt = macos_signing.verify_accepted_notarization(
                archive,
                submission,
                keychain_profile="notary-profile",
                timeout="2h",
                runner=runner,
            )

            self.assertEqual(
                receipt,
                macos_signing.NotarizationReceipt(
                    JOB_ID, submission.archive_sha256, "Accepted"
                ),
            )
            self.assertFalse(hasattr(receipt, "keychain_profile"))
            runner.assert_finished(self)

    def test_wait_rejects_mismatched_job_id_and_unsupported_status(self):
        with tempfile.TemporaryDirectory(prefix="macos-notary-wait-") as temporary:
            archive = self.make_archive(temporary)
            submission = self.submission(archive)
            other = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            cases = (
                ({"id": other, "status": "Accepted"}, "different job ID"),
                ({"id": JOB_ID, "status": "Success"}, "unsupported"),
            )
            for response, message in cases:
                with self.subTest(response=response):
                    runner = ScriptedRunner(
                        [step(self.wait_command(), stdout=json.dumps(response))]
                    )
                    with self.assertRaisesRegex(
                        macos_signing.NotarizationValidationError, message
                    ):
                        macos_signing.wait_for_notarization(
                            archive,
                            submission,
                            keychain_profile="notary-profile",
                            timeout="2h",
                            runner=runner,
                        )

    def test_log_rejects_job_digest_status_code_and_issues_drift(self):
        with tempfile.TemporaryDirectory(prefix="macos-notary-log-") as temporary:
            archive = self.make_archive(temporary)
            submission = self.submission(archive)
            other = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            cases = (
                ({"jobId": other}, "different job ID"),
                ({"sha256": "f" * 64}, "differs from submitted"),
                ({"statusCode": True}, "must be an integer"),
                ({"issues": "none"}, "null or an array"),
            )
            for overrides, message in cases:
                with self.subTest(overrides=overrides):
                    runner = ScriptedRunner(
                        [
                            step(
                                self.log_command(),
                                stdout=self.accepted_log(submission, **overrides),
                            )
                        ]
                    )
                    with self.assertRaisesRegex(
                        macos_signing.NotarizationValidationError, message
                    ):
                        macos_signing.notarization_log(
                            archive,
                            submission,
                            keychain_profile="notary-profile",
                            runner=runner,
                        )

            missing_issues = {
                "jobId": JOB_ID,
                "status": "Accepted",
                "statusCode": 0,
                "sha256": submission.archive_sha256,
            }
            runner = ScriptedRunner(
                [step(self.log_command(), stdout=json.dumps(missing_issues))]
            )
            with self.assertRaisesRegex(
                macos_signing.NotarizationValidationError, "omitted field 'issues'"
            ):
                macos_signing.notarization_log(
                    archive,
                    submission,
                    keychain_profile="notary-profile",
                    runner=runner,
                )

    def test_accepted_verification_rejects_nonempty_issues(self):
        with tempfile.TemporaryDirectory(prefix="macos-notary-issues-") as temporary:
            archive = self.make_archive(temporary)
            submission = self.submission(archive)
            runner = ScriptedRunner(
                [
                    step(
                        self.wait_command(),
                        stdout=json.dumps({"id": JOB_ID, "status": "Accepted"}),
                    ),
                    step(
                        self.info_command(),
                        stdout=json.dumps({"id": JOB_ID, "status": "Accepted"}),
                    ),
                    step(
                        self.log_command(),
                        stdout=self.accepted_log(
                            submission,
                            issues=[{"severity": "warning", "message": "problem"}],
                        ),
                    ),
                ]
            )
            with self.assertRaisesRegex(macos_signing.NotarizationValidationError, "no issues"):
                macos_signing.verify_accepted_notarization(
                    archive,
                    submission,
                    keychain_profile="notary-profile",
                    timeout="2h",
                    runner=runner,
                )

    def test_acceptance_requires_all_statuses_and_zero_status_code(self):
        cases = (
            ("Invalid", "Accepted", "Accepted", 0),
            ("Accepted", "Invalid", "Accepted", 0),
            ("Accepted", "Accepted", "Invalid", 1),
            ("Accepted", "Accepted", "Accepted", 4000),
        )
        for wait_status, info_status, log_status, status_code in cases:
            with self.subTest(
                wait=wait_status,
                info=info_status,
                log=log_status,
                code=status_code,
            ):
                with tempfile.TemporaryDirectory(
                    prefix="macos-notary-status-"
                ) as temporary:
                    archive = self.make_archive(temporary)
                    submission = self.submission(archive)
                    runner = ScriptedRunner(
                        [
                            step(
                                self.wait_command(),
                                stdout=json.dumps(
                                    {"id": JOB_ID, "status": wait_status}
                                ),
                            ),
                            step(
                                self.info_command(),
                                stdout=json.dumps(
                                    {"id": JOB_ID, "status": info_status}
                                ),
                            ),
                            step(
                                self.log_command(),
                                stdout=self.accepted_log(
                                    submission,
                                    status=log_status,
                                    statusCode=status_code,
                                ),
                            ),
                        ]
                    )
                    with self.assertRaises(macos_signing.NotarizationValidationError):
                        macos_signing.verify_accepted_notarization(
                            archive,
                            submission,
                            keychain_profile="notary-profile",
                            timeout="2h",
                            runner=runner,
                        )
                    runner.assert_finished(self)

    def test_info_detects_archive_mutation_during_command(self):
        with tempfile.TemporaryDirectory(prefix="macos-notary-info-mutation-") as temporary:
            archive = self.make_archive(temporary)
            submission = self.submission(archive)

            def mutate():
                archive.write_bytes(b"mutated")

            runner = ScriptedRunner(
                [
                    step(
                        self.info_command(),
                        stdout=json.dumps({"id": JOB_ID, "status": "Accepted"}),
                        effect=mutate,
                    )
                ]
            )
            with self.assertRaisesRegex(macos_signing.NotarizationValidationError, "changed after"):
                macos_signing.notarization_info(
                    archive,
                    submission,
                    keychain_profile="notary-profile",
                    runner=runner,
                )

    def test_duplicate_json_keys_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="macos-notary-duplicate-") as temporary:
            archive = self.make_archive(temporary)
            submission = self.submission(archive)
            runner = ScriptedRunner(
                [
                    step(
                        self.info_command(),
                        stdout=(
                            f'{{"id":"{JOB_ID}","status":"Accepted",'
                            '"status":"Invalid"}'
                        ),
                    )
                ]
            )
            with self.assertRaisesRegex(macos_signing.NotarizationValidationError, "repeats"):
                macos_signing.notarization_info(
                    archive,
                    submission,
                    keychain_profile="notary-profile",
                    runner=runner,
                )

    def test_nonstandard_json_constants_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="macos-notary-nan-") as temporary:
            archive = self.make_archive(temporary)
            submission = self.submission(archive)
            runner = ScriptedRunner(
                [
                    step(
                        self.info_command(),
                        stdout=f'{{"id":"{JOB_ID}","status":NaN}}',
                    )
                ]
            )
            with self.assertRaisesRegex(
                macos_signing.NotarizationValidationError, "non-standard constant"
            ):
                macos_signing.notarization_info(
                    archive,
                    submission,
                    keychain_profile="notary-profile",
                    runner=runner,
                )


class TicketVerificationTests(unittest.TestCase):
    def test_checks_online_notarization_requirement_without_mutating_binary(self):
        with tempfile.TemporaryDirectory(prefix="macos-ticket-") as temporary:
            path = Path(temporary) / "app-icon-toolkit-mcp"
            path.write_bytes(b"signed-binary")
            command = (
                macos_signing.CODESIGN,
                "--verify",
                "--verbose=4",
                "-R=notarized",
                "--check-notarization",
                absolute(path),
            )
            runner = ScriptedRunner([step(command)])

            macos_signing.check_notarization_ticket(path, runner)

            runner.assert_finished(self)

    def test_rejects_binary_mutation_during_ticket_check(self):
        with tempfile.TemporaryDirectory(prefix="macos-ticket-mutation-") as temporary:
            path = Path(temporary) / "app-icon-toolkit-mcp"
            path.write_bytes(b"signed-binary")
            command = (
                macos_signing.CODESIGN,
                "--verify",
                "--verbose=4",
                "-R=notarized",
                "--check-notarization",
                absolute(path),
            )
            runner = ScriptedRunner(
                [step(command, effect=lambda: path.write_bytes(b"mutated-binary"))]
            )

            with self.assertRaisesRegex(macos_signing.SignatureValidationError, "changed"):
                macos_signing.check_notarization_ticket(path, runner)


if __name__ == "__main__":
    unittest.main()
