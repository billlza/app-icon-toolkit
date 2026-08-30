# Security policy

## Supported versions

Security fixes are provided for the latest published minor release.

| Version | Supported |
| --- | --- |
| 0.2.x | Yes |
| 0.1.x | No |
| Older or unreleased snapshots | No |

## Reporting a vulnerability

Use the repository's private GitHub Security Advisory reporting flow. Do not
open a public issue for a vulnerability that could expose files outside the
selected workspace, overwrite an existing path, execute an unintended command,
or disclose source contents or credentials.

Include the affected version, host operating system and filesystem, a minimal
reproduction, expected behavior, and observed behavior. Maintainers will
acknowledge a complete report within seven days and will coordinate disclosure
after a fix is available.

## Threat model

App Icon Toolkit is a local build-style tool running with the current user's
permissions. The caller intentionally grants one workspace root per request.
Within that root, source and output paths are untrusted input.

The implementation defends against lexical path traversal, absolute inner
paths, Windows device names, symlink or non-regular source files, malformed or
oversized image inputs, destination replacement, partial output publication,
and unbounded concurrent generation. It does not sandbox the caller-selected
workspace root or protect against an operating-system administrator.

The transaction keeps the original staging directory open through validation,
rename, and reconciliation, and compares both names against that live pin after
every native result. It performs no recursive staging cleanup after generation
has started. If the namespace cannot prove whether the pinned object became
final, the tool preserves the evidence and returns
`ATOMIC_PUBLISH_INDETERMINATE` with `reconcile_first`; it never treats that
state as success, ordinary failure, or permission to retry automatically.

The publication primitive protects namespace integrity and atomic visibility;
it is not a cryptographic integrity mechanism and does not promise complete
power-loss durability. Files are synchronized, but directory entries,
controller caches, and remote-server recovery have platform-specific
durability boundaries. `SIGKILL` before rename can leave a hidden staging
directory. Automatic PID-based orphan cleanup is intentionally excluded because
PID reuse and concurrent processes can make ownership ambiguous.

Windows network filesystems that reject handle-relative rename fail explicitly
rather than using an ambient-path fallback. SMB remains unsupported. A process
with the same user identity and concurrent write access to the workspace can
force an indeterminate result or denial of service. Preserving staging on every
post-creation failure avoids deleting a replacement object through a raceable
name.

On Unix, the no-replace rename API remains path-based. The engine reopens and
compares staging with the pinned directory immediately before the syscall, but
an equally privileged process can replace the source in the remaining window.
Reconciliation will mark a different final object indeterminate; callers must
not consume that final directory as trusted output.

An open directory handle does not protect file contents from a process running
as the same user. Such a process can modify staging after readback validation
or final after publication without replacing the directory object. Run
generation in a workspace that excludes equally privileged concurrent writers;
this project does not add hashes, signatures, or encryption to ordinary local
icon assets.

Release scripts execute platform SDK validators in CI and development gates,
including MakePri and MakeAppx for the generated MSIX resource matrix. The MCP
runtime itself still invokes no subprocess and accepts no package manifest or
executable from a project.

No network service, authentication secret, telemetry, subprocess invocation,
or project-provided executable is part of the MCP runtime.
