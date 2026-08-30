# Contributing

Contributions are welcome. Keep changes focused on a demonstrated platform
contract or user workflow, and preserve the dependency direction described in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Development setup

Install Rust 1.88 or newer, `cargo-deny`, and `cargo-about` 0.9.2. Platform
format tests additionally require the validator named by the selected profile:
Xcode `actool`, Android AAPT2, `desktop-file-validate`, `icotool`, or Windows SDK
MakePri and MakeAppx.

Run the complete portable gate before opening a change:

```sh
./scripts/check.sh
```

Run every native profile available on your host:

```sh
./scripts/check-native.sh macos
ANDROID_HOME=/path/to/android-sdk ./scripts/check-native.sh android
./scripts/check-native.sh linux windows
```

On Windows, validate the MSIX resource matrix against a built executable:

```powershell
./scripts/check-msix-assets.ps1 `
  -Target x86_64-pc-windows-msvc `
  -Toolchain 1.97.1 `
  -Binary target/release-candidates/x86_64-pc-windows-msvc/app-icon-toolkit-mcp.exe
```

When dependencies change, regenerate notices and review the diff:

```sh
./scripts/generate-licenses.sh
./scripts/check-licenses.sh
```

## Change requirements

- Add tests for the normal path, important boundaries, and explicit failures.
- Do not weaken assertions, disable lints, suppress warnings, or hide errors
  behind default values.
- Do not add overwrite, merge, copy/delete, or silent fallback behavior to the
  publication path.
- Keep MCP DTOs out of the engine and filesystem or codec types out of domain.
- Avoid new dependencies when an existing dependency or the standard library
  provides the needed contract.
- Update README, architecture, security, and changelog documents when their
  stated behavior changes.
- Keep release targets in `scripts/release-targets.json`; do not add a parallel
  target allowlist to workflows or packaging scripts.

Windows filesystem changes require a real Windows test run. Cross-compilation
is useful for conditional-compilation coverage but is not runtime evidence.
