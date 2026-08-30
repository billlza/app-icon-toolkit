# Security policy

## Supported versions

Security fixes are provided for the latest published minor release.

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |
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

The publication primitive protects namespace integrity and atomic visibility;
it is not a cryptographic integrity mechanism and does not promise complete
power-loss durability. Windows network filesystems that reject handle-relative
rename fail explicitly rather than using an ambient-path fallback.

No network service, authentication secret, telemetry, subprocess invocation,
or project-provided executable is part of the MCP runtime.
