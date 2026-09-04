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
`app-icon-toolkit-vX.Y.Z-<release-target>.zip` on macOS and Windows, and
`app-icon-toolkit-vX.Y.Z-<release-target>.tar.gz` on Linux.

Verify the downloaded archive before extracting it. On Linux, run:

```sh
archive=app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.tar.gz
expected=$(awk -v name="$archive" '$2 == name { print $1 }' SHA256SUMS)
actual=$(sha256sum "$archive" | awk '{ print $1 }')
test -n "$expected" && test "$actual" = "$expected"
```

On macOS, print the archive checksum with:

```sh
archive=app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.zip
shasum -a 256 "$archive"
```

and compare it with the matching `SHA256SUMS` line. On Windows PowerShell, run:

```powershell
$Archive = ".\app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.zip"
Get-FileHash -Algorithm SHA256 $Archive
```

and compare the result with the matching `SHA256SUMS` line.

Extract the archive into a new, empty, versioned directory, then register and
install the local marketplace. Keep that directory after installation because
it is the registered marketplace source. Never extract a new release over an
older `app-icon-toolkit` directory. On macOS, run:

```sh
archive=app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.zip
install_root="$PWD/app-icon-toolkit-vX.Y.Z-RELEASE_TARGET"
test ! -e "$install_root"
mkdir "$install_root"
ditto -x -k "$archive" "$install_root"
cd "$install_root/app-icon-toolkit"
codex plugin marketplace add "$(pwd)"
codex plugin add app-icon-toolkit@app-icon-toolkit
```

On Linux, run:

```sh
archive=app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.tar.gz
install_root="$PWD/app-icon-toolkit-vX.Y.Z-RELEASE_TARGET"
test ! -e "$install_root"
mkdir "$install_root"
tar -xzf "$archive" -C "$install_root"
cd "$install_root/app-icon-toolkit"
codex plugin marketplace add "$(pwd)"
codex plugin add app-icon-toolkit@app-icon-toolkit
```

On Windows PowerShell, keep `$Extracted` in a stable location rather than a
temporary download directory:

```powershell
$Archive = ".\app-icon-toolkit-vX.Y.Z-RELEASE_TARGET.zip"
$Extracted = ".\app-icon-toolkit-vX.Y.Z-RELEASE_TARGET"
if (Test-Path -LiteralPath $Extracted) {
    throw "Refusing to merge a release into an existing directory: $Extracted"
}
New-Item -ItemType Directory -Path $Extracted | Out-Null
Expand-Archive $Archive -DestinationPath $Extracted
Set-Location (Join-Path $Extracted "app-icon-toolkit")
codex plugin marketplace add (Get-Location).Path
codex plugin add app-icon-toolkit@app-icon-toolkit
```

Verify that `codex plugin list --marketplace app-icon-toolkit --json` reports
the expected version as installed and enabled. Then run
`codex mcp get app-icon-toolkit --json` and confirm that the enabled stdio
server uses `./bin/app-icon-toolkit-mcp`, has no configured `env` or `env_vars`,
and has a `cwd` under the Codex plugin cache. Start a new Codex task and make
one read-only `plan_icon_set` call. The host loads newly installed MCP tools
when a task starts; an already-running task can continue using the older loaded
version.

Repeat this installation for a different operating-system user or computer.
Changing the ChatGPT account does not require access to the maintainer's
account or any private package source: the public release remains the shared
distribution source. An organization can still disable local plugins; the
package does not and should not bypass that administrative boundary.

## Upgrade a prebuilt local installation

`codex plugin marketplace upgrade` applies to Git marketplaces; it does not
refresh a local archive marketplace. An already-installed plugin also does not
prove that its cache was replaced. Treat an upgrade as an explicit, reversible
replacement:

1. Finish work in tasks using the old MCP and run `codex plugin list --json`.
   Continue only if the release installation is exactly
   `app-icon-toolkit@app-icon-toolkit`; `app-icon-toolkit@personal` is a
   separate source installation and must not be removed by these steps.
2. Keep the old extracted marketplace directory. Record its path and the old
   installed version so it can be re-added if the new installation fails.
3. Run `codex plugin remove app-icon-toolkit@app-icon-toolkit`, followed by
   `codex plugin marketplace remove app-icon-toolkit`.
4. Verify the new archive checksum, extract it into a different new, empty,
   stable, versioned directory using the platform instructions above, then run
   the two `marketplace add` and `plugin add` commands from that new package.
5. Verify the exact new version and cache `cwd` with
   `codex plugin list --marketplace app-icon-toolkit --json` and
   `codex mcp get app-icon-toolkit --json`. Start a new task and complete a
   read-only `plan_icon_set` call.
6. Delete the old extracted directory only after the new cached copy and new-task
   call are verified. If installation fails before then, remove the incomplete
   new registration and re-add the recorded old marketplace path.

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

macOS release binaries starting with v0.2.3 are signed with the project's
Developer ID identity and accepted by Apple's notarization service. The public
ZIP is the exact notarized submission, but a ZIP and a standalone command-line
binary cannot carry a stapled ticket like an application bundle or installer.
The first Gatekeeper verification therefore requires network access to Apple's
notarization service. Do not remove quarantine attributes or bypass Gatekeeper
to work around a failed verification; confirm the archive checksum and obtain a
fresh release download instead. v0.2.2 and earlier macOS binaries were unsigned.

Windows release binaries do not currently have an Authenticode signature, so
SmartScreen, endpoint-security software, or organization policy may still block
them. Do not weaken a managed computer's security policy; use the source-build
route or an organization-signed build when policy requires a trusted Windows
executable.

## Distribution boundary

The local plugin can be reinstalled independently on another account or
computer, but it is not an account-synchronized Universal Plugins Directory
entry. A directory-hosted version requires a public production HTTPS MCP and a
different file-transfer contract: a remote server cannot read or write paths on
the caller's computer. That future adapter must use bounded uploads and
downloadable results while preserving the existing domain and rendering core.
