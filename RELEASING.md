# Release process

1. Confirm `CHANGELOG.md`, plugin version, workspace version, and release tag
   agree. Validate `scripts/release-targets.json`; it is the only release target
   inventory.
2. Run `./scripts/generate-licenses.sh` and review dependency-license changes.
3. Run `./scripts/check.sh` and every locally available native validation
   profile with zero errors and zero warnings.
4. Push the release commit to `main` and require every hosted CI job to pass,
   including native ARM64, static-musl, Windows MSIX, and exact Universal2
   archive verification.
5. Create and push the annotated `v<version>` tag only from that green commit.
6. Wait for the release workflow to build the exact contract-defined archive
   set, execute package smoke tests, verify final binary architecture/runtime
   dependencies, generate SHA-256 checksums for accidental-corruption detection,
   and publish the GitHub Release.
7. Confirm the Universal2 archive from the Apple-silicon build was downloaded
   and smoke-tested on the Intel runner before publication.
8. Download each asset, verify its checksum, and confirm the release page points
   at the intended commit.

Do not recreate a tag or parallel release after an ambiguous publication
result. Reconcile the existing tag, workflow run, and GitHub Release first.
