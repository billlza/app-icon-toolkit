# App Icon Toolkit

App Icon Toolkit is an open-source local MCP server and Codex plugin written in
Rust. It turns explicitly supplied PNG artwork into deterministic application
icon bundles for macOS, Android, Windows, and Linux.

The server generates platform resource files; it does not invent artwork or
modify an existing IDE project. One request can target all four platforms, and
the final output directory is published as one no-overwrite transaction.

## What is supported

| Output profile | Generated result | Independent validation |
| --- | --- | --- |
| `mac_os_app_icon_set` | macOS `.appiconset` with all 16–512 point 1x/2x slots | Xcode `actool` |
| `android_adaptive` | legacy density icons plus adaptive foreground, background, and optional monochrome resources | Android AAPT2 |
| `windows_ico` | one ICO with 16, 24, 32, 48, and 256 pixel frames | `icotool` plus codec readback |
| `windows_msix_assets` | 57 qualified AppList, medium tile, and Store logo PNG assets from 16 to 600 pixels | MakePri and a validating MakeAppx package build |
| `linux_xdg` | freedesktop hicolor tree and `.desktop` entry | `desktop-file-validate` |

Generation is supported when the MCP server runs on macOS, Linux, or Windows.
Windows uses a handle-relative, atomic no-replace directory rename; filesystems
or network protocols that reject that primitive fail explicitly instead of
falling back to copy/delete or overwriting an existing path.

The MSIX profile generates icon resources only. It does not create or modify an
application manifest, produce a complete `.msix`, sign a package, or certify a
Store submission. The temporary CI package exists solely to validate resource
qualifiers and manifest references.

The project does not claim support for Apple Icon Composer `.icon` authoring,
Liquid Glass annotations, SVG input, editor-native project mutation, signing,
notarization, or direct publication to an app store. Android adaptive layers
must be supplied semantically; the tool does not guess them from a flattened
image.

## Prebuilt release coverage

Release archives retain the original thin macOS, GNU Linux, and Windows x64
names while adding new targets:

| Host package | Runtime boundary |
| --- | --- |
| macOS ARM64, Intel, and Universal2 | macOS 13.0 or newer; binaries are currently unsigned and not notarized |
| Linux x86_64 GNU | glibc 2.34 or newer, mechanically checked from the final ELF |
| Linux x86_64 and ARM64 musl | native-tested static ELF with no interpreter or `NEEDED` library entries |
| Windows x64 and ARM64 MSVC | native-tested executable with static UCRT/VCRuntime and no dynamic CRT imports |

Universal2 is built and smoke-tested on Apple silicon, then the exact same
archive is downloaded and smoke-tested again on an Intel runner. Static linking
does not promise compatibility with every kernel or filesystem; unsupported
atomic rename primitives still fail explicitly.

## Install from source

For a prebuilt, checksum-verified installation on a new account or computer,
follow [INSTALL.md](INSTALL.md). Each local installation is available to new
Codex tasks and does not depend on this development task or its plugin cache.

Rust 1.88 or newer is required for a source installation.

On macOS or Linux:

```sh
git clone https://github.com/billlza/app-icon-toolkit.git
cd app-icon-toolkit
./scripts/build-local.sh
codex plugin marketplace add "$(pwd)"
codex plugin add app-icon-toolkit@app-icon-toolkit
```

On Windows PowerShell:

```powershell
git clone https://github.com/billlza/app-icon-toolkit.git
Set-Location app-icon-toolkit
./scripts/build-local.ps1
codex plugin marketplace add (Get-Location).Path
codex plugin add app-icon-toolkit@app-icon-toolkit
```

Release archives contain the same plugin layout with a prebuilt binary. Direct
Git marketplace installation is not supported because platform binaries are
not committed; build a checkout first or use a release archive. Start a new
task after installation so the host discovers the MCP tools.

## MCP workflow

The server exposes two tools:

1. `plan_icon_set` decodes and validates every source and returns the exact
   artifact plan without writing files.
2. `generate_icon_set` independently replans the job, renders into sibling
   staging, validates every generated file, and atomically publishes a new
   output directory.

Planning validates sources and the artifact plan, not filesystem publication
readiness. For an output such as `icons/generated`, create the parent `icons`
first and leave `icons/generated` absent. Generation reports
`OUTPUT_PARENT_UNAVAILABLE` if the parent cannot be accessed; its `relative_path`
continues to identify the requested output (`icons/generated`), not the parent.

Generate operations are deliberately serialized. A concurrent MCP generation
request returns a structured `BUSY` error instead of joining an unbounded
queue. Separate engine callers racing for the same destination are protected
by the filesystem no-replace primitive: exactly one can publish.

The transaction pins the staging directory with a live filesystem handle.
After any native rename result it opens the staging and final names and compares
their live objects with that pin. If the result cannot be proven, the server
returns `ATOMIC_PUBLISH_INDETERMINATE`, preserves the evidence, and marks the
response `reconcile_first` instead of deleting by name or encouraging a blind
retry.

## Input and filesystem contract

- Source and output paths are UTF-8 paths relative to an explicit workspace
  root. Traversal, absolute paths, Windows device names, control characters,
  and non-portable separators are rejected.
- Inputs must be regular, single-frame PNG files no larger than 64 MiB or 4096
  pixels per edge. The flattened master must be at least 1024×1024; Android
  adaptive layers must be at least 432×432.
- The output directory must not already exist, and its parent directory must
  already exist. Generation does not create missing parents. There is no
  overwrite, merge, backup, or copy/delete fallback.
- Files use exclusive creation inside staging, are synchronized and read back,
  and are decoded again before publication.
- Any failure after staging creation preserves the hidden sibling directory and
  returns its relative path. This avoids raceable recursive deletion; callers
  may inspect and explicitly remove confirmed stale staging. `SIGKILL` before
  rename can leave the same kind of orphan without a response path.
- The caller-selected workspace root is the capability boundary. Run the MCP
  with the same filesystem privileges and workspace scope you would grant any
  local build tool.
- MCP stdout is reserved for protocol frames. Diagnostics go to stderr and do
  not include source contents, environment variables, or credentials.

This is ordinary local artifact generation (security level 0/1). It does not
add signatures, encryption, or tamper-evident metadata to icon files.

## Architecture

```text
app-icon-mcp ───────> app-icon-engine ───────> app-icon-domain
       └────────────────────────────────────> app-icon-domain
                              └─(Windows)───> app-icon-windows-fs
```

- `app-icon-domain` owns validated values, target profiles, artifact contracts,
  and deterministic plans. It has no MCP, codec, async-runtime, or filesystem
  dependency.
- `app-icon-engine` owns bounded PNG decoding, five exporter modules, shared
  render caching, validation, staging, and transaction orchestration.
- `app-icon-mcp` owns JSON Schema DTOs, domain conversion, tool annotations,
  structured failures, concurrency control, and stdio transport.
- `app-icon-windows-fs` is a private leaf adapter containing the single audited
  Windows native FFI boundary needed for handle-relative no-replace
  publication. The other crates continue to forbid unsafe code.

There are five output profiles but one MCP and one transaction. Splitting by
platform would duplicate schemas, error mapping, process management, and
concurrency logic while losing all-or-nothing multi-platform generation.

See [ARCHITECTURE.md](ARCHITECTURE.md) for dependency and error-flow details.

## Quality gates

Install `cargo-deny` and `cargo-about`, then run:

```sh
./scripts/check.sh
```

The gate checks formatting, Clippy with warnings denied, all tests, rustdoc with
warnings denied, RustSec/license/source policy, generated third-party notices,
and a locked release build. CI repeats the Rust gate on macOS, Linux, and
Windows, proves a Rust 1.88 source build and installed-plugin smoke on all three,
and builds every entry from the validated release-target contract with Rust
1.97.1. Every final archive is unpacked and smoke-tested; representative macOS,
Linux, and Windows archives are then installed and listed by a clean pinned
Codex host, and the cached installed copy is smoke-tested again.

Native format checks use disposable generated fixtures:

```sh
./scripts/check-native.sh macos
ANDROID_HOME=/path/to/android-sdk ./scripts/check-native.sh android
./scripts/check-native.sh linux windows
```

The selected profile fails if its independent validator is unavailable. CI
runs all portable profiles on suitable hosts; Windows runners additionally use
`scripts/check-msix-assets.ps1` with MakePri and MakeAppx.

## Project policies

- [CONTRIBUTING.md](CONTRIBUTING.md) describes development and review gates.
- [INSTALL.md](INSTALL.md) documents clean-machine and cross-account installation.
- [SECURITY.md](SECURITY.md) defines the threat model and reporting process.
- [CHANGELOG.md](CHANGELOG.md) records user-visible changes.
- [THIRD_PARTY_LICENSES.html](THIRD_PARTY_LICENSES.html) contains dependency
  license texts for binary distributions.

App Icon Toolkit is licensed under the [MIT License](LICENSE).
