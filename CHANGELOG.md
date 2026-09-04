# Changelog

All notable user-visible changes are documented here. This project follows
Semantic Versioning.

## 0.2.4 - 2026-09-04

### Fixed

- Carry the canonical artifact name in the Universal2 verification matrix and
  validate every generated release-target field consumed by the CI and Release
  workflows.
- Preserve the failed, unpublished v0.2.3 attempt while starting v0.2.4 from a
  new commit, tag, workflow run, and artifact set.

## 0.2.3 - 2026-09-04

### Added

- Sign the ARM64, Intel, and Universal2 macOS MCP executables with Developer ID,
  submit the exact public ZIP archives for Apple notarization, and verify the
  online notarization ticket for every architecture.
- Add a fail-closed local release finalizer that binds annotated tag, commit,
  workflow run and attempt, numeric Actions artifact IDs and digests, numeric
  Draft identity, signing identity, notarization jobs, and the final asset set
  in append-only private receipts.
- Run signed macOS runtime acceptance on credential-isolated Apple silicon and
  Intel hosts before publication, then verify the immutable public release and
  every asset again through anonymous numeric GitHub endpoints.
- Validate prebuilt installation in an isolated, credential-free Codex home,
  including the host-resolved MCP command, independent cache, runtime server
  identity, and a complete protocol-and-generation smoke test.

### Changed

- Package macOS releases as ZIP instead of `tar.gz`, while retaining `tar.gz`
  for Linux and ZIP for Windows.
- Make the tag workflow retain attempt-qualified unsigned candidates and stage
  an empty Draft; signing, notarization, Draft upload, validation, and final
  publication now remain separate evidence gates.
- Require prebuilt installs and upgrades to use new versioned directories, with
  explicit local-marketplace replacement, verification, and rollback steps.

### Fixed

- Preserve Unix regular-file type and executable permission metadata so macOS
  Archive Utility and `ditto` extract a runnable MCP executable.
- Reject mixed workflow-attempt candidate sets, ambiguous mutation outcomes,
  replaced release identities, and GitHub asset uploads routed through the
  incorrect API hostname.
- Bind extraction and precise allowlist rollback to one stable output-directory
  capability, recover unsigned candidate staging without deleting signed state,
  and retain stable-file checks when a parser also fails.
- Reauthorize bounded release-package inputs at the copy boundary, stream ZIP
  members, cap final output, normalize damaged DEFLATE failures, and persist an
  intent before every Draft asset upload.
- Reject plugin packages whose runtime MCP identity differs from the manifest,
  and verify Windows' extensionless launcher contract through Rust rather than
  substituting Python process-resolution behavior.

## 0.2.2 - 2026-09-04

### Added

- Add a clean-machine installation guide that distinguishes prebuilt release,
  source-build, local marketplace, and future public HTTPS distribution paths.
- Allowlist and safely extract, byte-compare, and smoke-test every final release
  archive before it can leave its target build job.
- Validate release archives through actual Codex local-marketplace installs and
  plugin listings on clean macOS, Linux, and Windows runners.
- Extend minimum-Rust validation to macOS and smoke-test release-mode local
  plugin builds with Rust 1.88 on all three host operating systems.

### Fixed

- Clarify unavailable-output-parent diagnostics without changing the stable
  error code or the workspace-relative output path returned to clients.
- Document that generation requires an existing parent directory and that
  planning does not check filesystem publication readiness.
- Reject the Windows console device names `CONIN$` and `CONOUT$` in portable
  relative paths and in the native Windows publication adapter.
- Publish the output-parent contract in MCP server instructions, tool
  descriptions, and JSON Schema field descriptions.
- Isolate local source-install and quality-gate builds from shared Cargo output,
  refuse to copy a stale binary after a failed build, and keep host proc-macro
  dylibs loadable on macOS 27 with supported pre-1.98 Rust toolchains.

## 0.2.1 - 2026-08-30

### Fixed

- Run release metadata validation on Python 3.10 by using Cargo's authoritative
  workspace metadata instead of a newer standard-library TOML module.
- Preserve simultaneous MCP protocol and shutdown failures without relying on
  Python 3.11 exception groups.
- Lock release-contract tests to the complete eight-target public archive set.

## 0.2.0 - 2026-08-30

### Added

- Add the `windows_msix_assets` profile with 57 deterministic AppList,
  medium-tile, and Store-logo PNG resources, plus native MakePri and MakeAppx
  validation in Windows CI.
- Add native-tested Windows ARM64, static-musl Linux x86_64 and ARM64, and
  macOS Universal2 release archives while preserving the existing thin and GNU
  archive names.
- Add strict final-binary checks for PE/ELF machine type, Windows dynamic CRT
  imports, GNU glibc 2.34 ceiling, static-musl linkage, Mach-O slices, and each
  macOS slice's 13.0 deployment minimum.
- Add identity-based publication outcome reconciliation and structured MCP
  fields for publication state, retry advice, staging path, and wrapped primary
  error code.

### Changed

- Build releases with pinned Rust 1.97.1 from one validated target contract and
  publish candidates with atomic no-replace semantics.
- Smoke-test the exact Universal2 archive on both Apple silicon and Intel before
  publication.

### Fixed

- Preserve staging evidence when a native rename result cannot prove whether
  publication committed, instead of deleting by name and returning a misleading
  ordinary failure.
- Retain typed primary errors and preserve staging on every post-creation
  failure, avoiding recursive deletion through a raceable directory name.
- Keep the original staging directory handle live through reconciliation so
  inode or file-ID reuse cannot make a replacement object appear validated.

## 0.1.2 - 2026-08-30

### Fixed

- Replace the warning-producing artifact download action in the publication
  job with exact-name GitHub CLI downloads and explicit four-file validation.
- Close every packaged MCP smoke-test process stream deterministically and run
  Python release checks with warnings promoted to errors.

## 0.1.1 - 2026-08-30

### Fixed

- Resolve packaged MCP commands from the declared plugin working directory so
  Windows release archives are smoke-tested against their bundled `.exe`
  instead of the release runner's ambient checkout.
- Derive the installed executable name from the requested release target and
  reject unsupported target triples explicitly.

## 0.1.0 - 2026-08-30

### Added

- One local MCP server with exact planning and transactional generation tools.
- macOS app icon set, Android legacy/adaptive, Windows ICO, and Linux hicolor
  exporters.
- Bounded PNG inspection, premultiplied-alpha resizing, deterministic output,
  post-write validation, and structured stable error codes.
- Atomic no-replace publication on macOS, Linux, and Windows hosts, with
  explicit unsupported errors for filesystems that cannot provide the required
  primitive.
- Cross-platform CI, minimum Rust version checks, independent native format
  validators, dependency policy, and generated third-party license notices.
