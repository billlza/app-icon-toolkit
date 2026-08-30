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
6. The publisher atomically renames staging to the final name with no-replace
   semantics. A collision is a stable `OUTPUT_EXISTS` failure.
7. Normal failures remove staging. If cleanup itself fails, the returned error
   preserves both the primary and cleanup failures.

## Platform boundaries

The four exporter modules own platform-specific artifact plans and encoders.
They do not write files. Transaction code owns all I/O and cleanup.

Unix-like hosts use `renameat_with(NOREPLACE)` relative to the parent
capability. Windows uses `NtSetInformationFile(FileRenameInformation)` with
`ReplaceIfExists=false` and the same parent capability handle. Unsupported
filesystems fail explicitly; there is no ambient absolute-path or copy/delete
fallback.

The Windows adapter validates the source handle as a non-reparse directory and
denies delete sharing so that link cannot move after validation. The adapter
contains the only project-authored unsafe block, and its buffer offsets are
checked against the Windows native API binding at compile time.

## Concurrency and resources

The MCP server permits bounded parallel planning and only one generation at a
time. The engine has no global mutable job state; its only process-global value
is an atomic staging-name counter. Files and directory capabilities are owned
values and close on scope exit.

Input size and decoded edge limits bound memory. Plans and artifact lists are
finite fixed profiles. There are no retry loops except the bounded 128-attempt
exclusive staging-name allocation.

## Compatibility policy

The first release has no persisted project schema and never modifies an input
project, so there is no data migration. Tool schemas, stable error codes,
artifact paths, and generated metadata are public compatibility surfaces.
Breaking changes to those surfaces require a major version change and explicit
changelog entry.
