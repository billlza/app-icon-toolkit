# Release process

1. Confirm `CHANGELOG.md`, plugin version, workspace version, and release tag
   agree.
2. Run `./scripts/generate-licenses.sh` and review dependency-license changes.
3. Run `./scripts/check.sh` and every locally available native validation
   profile with zero errors and zero warnings.
4. Push the release commit to `main` and require every hosted CI job to pass.
5. Create and push the annotated `v<version>` tag only from that green commit.
6. Wait for the release workflow to build all target archives, execute package
   smoke tests, generate SHA-256 checksums for accidental-corruption detection,
   and publish the GitHub Release.
7. Download each asset, verify its checksum, and confirm the release page points
   at the intended commit.

Do not recreate a tag or parallel release after an ambiguous publication
result. Reconcile the existing tag, workflow run, and GitHub Release first.
