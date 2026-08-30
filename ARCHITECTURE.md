# Architecture

## Design goals

App Icon Toolkit must produce deterministic, independently valid icon assets
without mutating project files or replacing existing output. The architecture
keeps platform policy, rendering, filesystem effects, and MCP transport in
separate modules so each boundary can be tested directly.

## Dependency direction

```text
app-icon-mcp ───────> app-icon-engine ───────> app-icon-domain
       └────────────────────────────────────> app-icon-domain
                              └─(Windows)───> app-icon-windows-fs
```

Dependencies point inward. Domain code contains no filesystem, image codec,
async runtime, or MCP types. The Windows filesystem crate is a private leaf;
it cannot call the engine or understand icon jobs.

## Request flow

1. The MCP adapter deserializes a precise schema and converts it to validated
   domain values.
2. The engine opens the caller-selected workspace root as a capability.
3. Source inspection enforces regular-file, encoded-size, format, animation,
   dimension, and opacity constraints.
4. Exporters produce one deterministic artifact plan. Rendering shares decoded
   source images and scaled-raster caches across platform targets.
5. Generation writes exclusively into a sibling staging directory,
   synchronizes each file, reads it back, and validates its native encoding
   contract.
6. The transaction records the staging directory's filesystem object identity.
7. The publisher atomically renames staging to the final name with no-replace
   semantics, then reconciles the staging and final names against that identity
   regardless of the native result.
8. A proven collision is a stable `OUTPUT_EXISTS` failure. A proven successful
   rename succeeds even if a network filesystem returned a late error. An
   unprovable result is `ATOMIC_PUBLISH_INDETERMINATE` and preserves both names.
9. Failures after staging creation preserve staging and return its path while
   retaining the typed primary error and stable code. There is no recursive
   name-based cleanup in the transaction path.

## Platform boundaries

The five profile exporters own platform-specific artifact plans and encoders.
They do not write files. The Win32 ICO and MSIX resource-matrix profiles remain
separate modules because they implement different public artifact contracts.
Transaction code owns staging, validation I/O, and publication effects.

Unix-like hosts use `renameat_with(NOREPLACE)` relative to the parent
capability. Windows uses `NtSetInformationFile(FileRenameInformation)` with
`ReplaceIfExists=false` and the same parent capability handle. Unsupported
filesystems fail explicitly; there is no ambient absolute-path or copy/delete
fallback.

The Windows adapter validates the source handle as a non-reparse directory and
denies delete sharing so that link cannot move after validation. It records
`FILE_ID_INFO` (volume serial number plus 128-bit file ID) and rechecks the
expected identity before rename. Unix reconciliation uses device and inode.
The adapter contains the only project-authored unsafe blocks, and native buffer
sizes and offsets are checked against the Windows API bindings.

Unix rechecks the staging identity immediately before the path-based
`renameat_with` call. No portable Unix API binds the rename source to an open
directory handle, so an equally privileged process can still replace the name
in that final syscall window. Post-rename reconciliation will return
indeterminate rather than accept a different final identity; such a final must
be treated as untrusted evidence, not generated output.

Directory identity does not freeze file contents. A process with the same user
permissions can mutate files inside staging after validation, or inside final
after publication, without changing the directory identity. The engine is not
a sandbox or cryptographic integrity boundary; callers must grant an exclusive
workspace against equally privileged concurrent writers while generating and
consuming output.

The reconciliation classifier is a pure state machine under
`transaction/reconcile.rs`. If the final name has the original staging
identity and the staging name no longer does, publication is proven. If an
error leaves the original object at staging and final is absent or different,
non-publication is proven. Missing, replaced, simultaneous, or unobservable
states are indeterminate and are never cleaned automatically. Proven
non-publication also preserves staging because a later recursive delete by name
would reopen a replacement race.

This is namespace-integrity logic, not a complete power-loss protocol. Files
are synchronized before rename, but the project does not claim cross-platform
directory-entry durability through controller caches, sudden power loss, NFS,
or SMB. `SIGKILL` before rename can leave an identifiable hidden staging
directory. Explicit cleanup is an operator action; automatic PID- or name-based
orphan deletion is intentionally absent because it can misidentify another
live process's directory.

## Concurrency and resources

The MCP server permits bounded parallel planning and only one generation at a
time. The engine has no global mutable job state; its only process-global value
is an atomic staging-name counter. Files and directory capabilities are owned
values and close on scope exit.

Input size and decoded edge limits bound memory. Plans and artifact lists are
finite fixed profiles. There are no retry loops except the bounded 128-attempt
exclusive staging-name allocation.

## Distribution architecture

`scripts/release-targets.json` is the single release target inventory. Its
strict loader feeds the CI and release matrices, packaging allowlist, installed
binary names, expected public assets, license targets, architecture checks, and
the Intel verifier for Universal2. Thin target identifiers must equal Rust
triples; Universal2 is an explicit synthetic identifier with two Rust slices.

Release builds use one pinned Rust toolchain in an isolated Cargo target
directory. Final binaries are checked, not inferred from build flags: PE
machine and CRT imports, ELF machine and glibc ceiling or static-musl entries,
and Mach-O slices plus per-slice deployment minimum. Candidate publication uses
a complete sibling temporary file and an atomic no-replace hard link.

## Compatibility policy

The project has no persisted project schema and never modifies an input
project, so there is no data migration. Tool schemas, stable error codes,
artifact paths, and generated metadata are public compatibility surfaces.
Breaking changes to those surfaces require a major version change and explicit
changelog entry.
