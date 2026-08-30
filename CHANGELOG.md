# Changelog

All notable user-visible changes are documented here. This project follows
Semantic Versioning.

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
