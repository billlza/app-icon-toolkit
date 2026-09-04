# Installation

App Icon Toolkit is a local stdio MCP server. Each operating-system user and
computer installs its own copy. On a Codex host where local plugins are enabled
by workspace policy, every new task can use that installed copy. Installation
does not depend on the maintainer's account, task, cache, or checkout used to
build a release.

## Install a prebuilt release

Open the [latest GitHub release](https://github.com/billlza/app-icon-toolkit/releases/latest),
download `SHA256SUMS`, and select exactly one archive:

| Computer | Release target |
| --- | --- |
| Apple silicon Mac | `aarch64-apple-darwin` or `universal2-apple-darwin` |
| Intel Mac | `x86_64-apple-darwin` or `universal2-apple-darwin` |
| x64 Linux with glibc 2.34 or newer | `x86_64-unknown-linux-gnu` |
| x64 Linux without a compatible glibc | `x86_64-unknown-linux-musl` |
| ARM64 Linux | `aarch64-unknown-linux-musl` |
| x64 Windows | `x86_64-pc-windows-msvc` |
| ARM64 Windows | `aarch64-pc-windows-msvc` |

Release CI installs the exact Codex CLI version recorded in
[`CODEX_HOST_TEST_VERSION`](CODEX_HOST_TEST_VERSION) on clean macOS, Linux, and
Windows hosts. This is a tested host version, not a claim that older or newer
Codex releases are incompatible.

The archive name is
`app-icon-toolkit-vX.Y.Z-<release-target>.tar.gz` on macOS/Linux and
`app-icon-toolkit-vX.Y.Z-<release-target>.zip` on Windows.

Verify the downloaded archive before extracting it. On Linux, run:

```sh
archive=app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.tar.gz
expected=$(awk -v name="$archive" '$2 == name { print $1 }' SHA256SUMS)
actual=$(sha256sum "$archive" | awk '{ print $1 }')
test -n "$expected" && test "$actual" = "$expected"
```

On macOS, print the archive checksum with:

```sh
archive=app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.tar.gz
shasum -a 256 "$archive"
```

and compare it with the matching `SHA256SUMS` line. On Windows PowerShell, run:

```powershell
$Archive = ".\app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.zip"
Get-FileHash -Algorithm SHA256 $Archive
```

and compare the result with the matching `SHA256SUMS` line.

Extract the archive, then register and install the local marketplace. On macOS
or Linux, first enter a stable directory that you will keep and place the
archive there, then run:

```sh
archive=app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.tar.gz
tar -xzf "$archive"
cd app-icon-toolkit
codex plugin marketplace add "$(pwd)"
codex plugin add app-icon-toolkit@app-icon-toolkit
```

On Windows PowerShell, keep `$Extracted` in a stable location rather than a
temporary download directory:

```powershell
$Archive = ".\app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.zip"
$Extracted = ".\app-icon-toolkit-vX.Y.Z-RELEASE_TARGET"
Expand-Archive $Archive -DestinationPath $Extracted
Set-Location (Join-Path $Extracted "app-icon-toolkit")
codex plugin marketplace add (Get-Location).Path
codex plugin add app-icon-toolkit@app-icon-toolkit
```

Start a new Codex task after installation. The host loads newly installed MCP
tools when a task starts; an already-running task can continue using the older
loaded version.

Repeat this installation for a different operating-system user or computer.
Changing the ChatGPT account does not require access to the maintainer's
account or any private package source: the public release remains the shared
distribution source. An organization can still disable local plugins; the
package does not and should not bypass that administrative boundary.

## Install from source

Rust 1.88 or newer is required. On macOS or Linux:

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

Directly adding the Git repository as a marketplace is not a prebuilt install
path. Platform binaries are intentionally excluded from Git, so a source
checkout must be built locally before it is registered. Prebuilt installs must
use a release archive.

## Platform trust and managed computers

Current macOS release binaries are unsigned and not notarized. Current Windows
release binaries do not have an Authenticode signature. A browser, Gatekeeper,
SmartScreen, endpoint-security product, or organization policy may therefore
block a prebuilt binary. Do not weaken a managed computer's security policy;
use the source-build route or an organization-signed build when policy requires
trusted executables.

## Distribution boundary

The local plugin can be reinstalled independently on another account or
computer, but it is not an account-synchronized Universal Plugins Directory
entry. A directory-hosted version requires a public production HTTPS MCP and a
different file-transfer contract: a remote server cannot read or write paths on
the caller's computer. That future adapter must use bounded uploads and
downloadable results while preserving the existing domain and rendering core.
