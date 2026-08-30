# Changelog

All notable user-visible changes are documented here. This project follows
Semantic Versioning.

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
