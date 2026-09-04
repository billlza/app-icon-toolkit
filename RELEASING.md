# Release process

This is the release runbook for v0.2.6 and later. A release passes through four
separate trust domains. Do not collapse them into one “release succeeded”
claim:

1. The `Release` tag workflow builds and retains attempt-bound unsigned
   candidates, runs portable and native gates, and creates or reconciles only an
   empty Draft. It does not upload public assets and does not publish the Draft.
2. A trusted local macOS finalizer prepares the exact asset set, signs the three
   macOS binaries, submits their exact ZIP archives for notarization, and stages
   the complete asset set on that same still-unpublished Draft.
3. The credential-isolated `Validate Signed Draft` workflow reads the numeric
   Draft only in isolated fetch jobs, transfers verified bytes through
   attempt-bound Actions artifacts, and performs runtime acceptance in separate
   read-only Apple silicon and Intel jobs. It emits one receipt bound to the
   exact workflow, run, attempt, Draft identity, asset IDs, sizes, and digests.
4. The local finalizer consumes that exact hosted receipt, publishes by numeric
   release ID, anonymously downloads the immutable public release and every
   numeric asset again, and writes `public-verified.json`.

A Draft, an Apple `Accepted` response, a successful hosted validation run, or a
public release without `public-verified.json` is not a completed release.

## 0. Operator and repository prerequisites

Use a clean checkout of the exact release commit. Run the complete local gate,
review generated license changes, and require the exact commit's hosted CI to
finish with no errors or warnings:

```bash
./scripts/generate-licenses.sh
./scripts/check-licenses.sh
./scripts/check.sh
```

Confirm that `CHANGELOG.md`, `.codex-plugin/plugin.json`, every Cargo workspace
package, `Cargo.lock`, and the planned tag all identify v0.2.7. Treat
`scripts/release-targets.json` as the only release target inventory.

GitHub immutable releases must already be enabled for the repository before
local finalization begins. An authorized repository administrator enables that
policy out of band. This runbook deliberately contains no command that changes
the repository setting. The finalizer reads the policy and fails closed unless
the response says `enabled: true`; this read-only inspection is also available
to the operator:

```bash
RELEASE_REPOSITORY='<owner>/<repository>'
gh api --hostname github.com \
  -H 'X-GitHub-Api-Version:2026-03-10' \
  "repos/$RELEASE_REPOSITORY/immutable-releases"
```

Authenticate `gh` through the operator's normal credential store. Never put a
GitHub token, Apple credential, private key, certificate export, or password in
this runbook, a command argument, a receipt, or the repository. The
`--notary-profile` value below is only the name of an existing local Keychain
profile.

Set explicit release bindings. Do not derive any of these from whichever run or
release happens to be newest:

```bash
RELEASE_CHECKOUT='/absolute/path/to/the/clean/tagged/checkout'
RELEASE_REPOSITORY='<owner>/<repository>'
RELEASE_TAG='v0.2.7'
RELEASE_HEAD_SHA='<40-character-lowercase-tagged-commit-sha>'
IDENTITY_SHA1='<40-character-uppercase-developer-id-fingerprint>'
NOTARY_PROFILE='<existing-keychain-profile-name>'
RELEASE_EVIDENCE_ROOT='/absolute/private/path/release-evidence'
install -d -m 700 "$RELEASE_EVIDENCE_ROOT"
```

The signing Mac must never execute a downloaded candidate. Its finalizer uses
only static architecture, signature, package, and notarization-ticket checks.
Actual MCP execution of signed Draft binaries belongs only to the
credential-isolated hosted validation jobs.

The annotated `v0.2.3` tag records a failed, unpublished workflow attempt. Do
not delete, move, reuse, or finalize that tag or any artifact from its run.

The annotated `v0.2.4` tag records a second unpublished attempt. Its Release
workflow reached an empty Draft and local preparation completed, but the tagged
finalizer could not resume that sealed asset set for the next documented phase.
Preserve its tag, empty Draft, workflow attempts, artifacts, and local receipts.
Do not delete, move, reuse, publish, or finalize any part of that attempt.

The annotated `v0.2.5` tag records a third unpublished attempt. Its exact
signed assets reached the bound Draft, but hosted validation proved that a
read-only GitHub Actions installation token cannot inspect an unpublished
release. Preserve its tag, populated Draft, workflow runs, artifacts, local
receipts, and notarization jobs. Do not delete, move, reuse, publish, or
finalize any part of that attempt.

The annotated `v0.2.6` tag records a fourth unpublished attempt. Its signed
assets passed native Apple silicon and Intel validation, but hosted receipt
aggregation rejected the non-canonical Universal2 architecture order. Preserve
its tag, populated Draft, workflow runs, artifacts, local receipts, and
notarization jobs. Do not delete, move, reuse, publish, or finalize any part of
that attempt.

## 1. Freeze the annotated tag and source workflow attempt

Before tagging, prove that the checkout is clean, that `HEAD` is the intended
commit, and that the exact commit's `CI` run is green. Then create one annotated
tag with neutral release naming and push that exact ref:

```bash
git -C "$RELEASE_CHECKOUT" status --short
git -C "$RELEASE_CHECKOUT" rev-parse HEAD
git -C "$RELEASE_CHECKOUT" tag -a "$RELEASE_TAG" "$RELEASE_HEAD_SHA" \
  -m "Release $RELEASE_TAG"
git -C "$RELEASE_CHECKOUT" push origin "refs/tags/$RELEASE_TAG"
```

Wait for every job in the resulting `Release` workflow to pass, including the
empty-Draft staging job. Record the positive numeric workflow ID, run ID, and
run attempt from that exact tag run:

```bash
SOURCE_WORKFLOW_ID='<numeric-release-workflow-id>'
SOURCE_RUN_ID='<numeric-release-run-id>'
SOURCE_RUN_ATTEMPT='<positive-run-attempt>'

gh run view "$SOURCE_RUN_ID" --repo "$RELEASE_REPOSITORY" \
  --json databaseId,workflowDatabaseId,attempt,headBranch,headSha,workflowName,event,status,conclusion
```

The returned workflow ID, run ID, attempt, tag ref, and head SHA must equal the
recorded values. Inspect the Draft and record both its GraphQL node ID and its
positive numeric database ID. At this point its `assets` array must be empty,
`isDraft` must be true, and `isPrerelease` must be false:

```bash
gh release view "$RELEASE_TAG" --repo "$RELEASE_REPOSITORY" \
  --json id,databaseId,tagName,name,body,isDraft,isPrerelease,assets

RELEASE_NODE_ID='<exact-graphql-release-node-id>'
RELEASE_DATABASE_ID='<positive-numeric-release-database-id>'
```

If the tag workflow must be rerun, only **Re-run all jobs** is allowed. With the
CLI, `gh run rerun "$SOURCE_RUN_ID" --repo "$RELEASE_REPOSITORY"` means the
whole run; never select “Re-run failed jobs”, rerun one job, or use `--failed`
or `--job`. After a rerun, record the new `SOURCE_RUN_ATTEMPT` and start a new
local attempt root. Never mix artifacts from different attempts.

## 2. Prepare, notarize, and stage on the trusted Mac

Create an attempt path outside the Git checkout. The path is bound to the exact
source run and attempt and must be preserved for reconciliation:

```bash
ATTEMPT_ROOT="$RELEASE_EVIDENCE_ROOT/app-icon-toolkit-$RELEASE_TAG-run-$SOURCE_RUN_ID-attempt-$SOURCE_RUN_ATTEMPT"

FINALIZER=(
  python3 "$RELEASE_CHECKOUT/scripts/finalize-macos-release.py"
  --plugin-root "$RELEASE_CHECKOUT"
  --repository "$RELEASE_REPOSITORY"
  --tag "$RELEASE_TAG"
  --head-sha "$RELEASE_HEAD_SHA"
  --workflow-id "$SOURCE_WORKFLOW_ID"
  --run-id "$SOURCE_RUN_ID"
  --run-attempt "$SOURCE_RUN_ATTEMPT"
  --identity-sha1 "$IDENTITY_SHA1"
  --notary-profile "$NOTARY_PROFILE"
  --attempt-root "$ATTEMPT_ROOT"
)
```

Run and inspect each phase separately. `prepare` downloads only the exact
current-attempt artifacts, signs and statically verifies macOS binaries, builds
the final archives, and produces the complete checksum set. It does not submit
anything to Apple and does not execute a candidate:

```bash
"${FINALIZER[@]}" --stop-after prepare
```

`notarize` submits the exact three prepared macOS ZIP archives, requires Apple
wait/info/log agreement with zero issues, and validates each standalone
binary's online ticket:

```bash
"${FINALIZER[@]}" --stop-after notarize
```

`stage` uploads the exact archive allowlist and `SHA256SUMS` by numeric Draft
identity. It persists one append-only intent before each asset POST, then
verifies all remote sizes and digests while leaving the release unpublished. If
the process stops after an intent is durable, resume is read-only until the
remote asset is observed or `--reconcile-github-upload` explicitly authorizes
the exact missing remainder:

```bash
"${FINALIZER[@]}" --stop-after stage
```

Re-run the read-only `gh release view` command from step 1. The GraphQL and
numeric release IDs must be unchanged, the release must still be a Draft, and
its assets must exactly match the stage result. Preserve the attempt directory;
do not edit its append-only receipts.

## 3. Dispatch and bind hosted signed-Draft validation

Dispatch `Validate Signed Draft` using the tag ref, never the default branch or
a moving branch. GitHub exposes Draft releases only to a push-capable identity,
so the workflow grants its installation token `contents: write` only in
isolated jobs that perform bound GETs and never extract or execute candidates.
The native validation and receipt jobs remain read-only, credentials are not
persisted, and candidate processes receive a minimal environment with no GitHub
token. The workflow receives no signing or notarization credential:

```bash
gh workflow run validate-signed-draft.yml \
  --repo "$RELEASE_REPOSITORY" \
  --ref "$RELEASE_TAG" \
  -f source_workflow_id="$SOURCE_WORKFLOW_ID" \
  -f source_run_id="$SOURCE_RUN_ID" \
  -f source_run_attempt="$SOURCE_RUN_ATTEMPT" \
  -f release_id="$RELEASE_NODE_ID" \
  -f release_database_id="$RELEASE_DATABASE_ID" \
  -f tag="$RELEASE_TAG" \
  -f head_sha="$RELEASE_HEAD_SHA" \
  -f identity_sha1="$IDENTITY_SHA1"
```

Wait for all jobs in that exact dispatch to pass. Record and verify its numeric
workflow ID, run ID, and attempt rather than selecting a run by recency:

```bash
HOSTED_WORKFLOW_ID='<numeric-validate-signed-draft-workflow-id>'
HOSTED_RUN_ID='<numeric-hosted-validation-run-id>'
HOSTED_RUN_ATTEMPT='<positive-hosted-run-attempt>'

gh run view "$HOSTED_RUN_ID" --repo "$RELEASE_REPOSITORY" \
  --json databaseId,workflowDatabaseId,attempt,headBranch,headSha,workflowName,event,status,conclusion
```

List that run's artifacts through the read-only numeric API. Select exactly one
artifact named
`hosted-validation-receipt-run-<run-id>-attempt-<run-attempt>`, verify it is not
expired, and record its positive numeric ID, size, and SHA-256 digest:

```bash
gh api --hostname github.com --paginate \
  "repos/$RELEASE_REPOSITORY/actions/runs/$HOSTED_RUN_ID/artifacts?per_page=100" \
  --jq '.artifacts[] | {id,name,size_in_bytes,digest,expired}'

HOSTED_RECEIPT_ARTIFACT_ID='<positive-numeric-receipt-artifact-id>'
```

If this workflow must be rerun, only **Re-run all jobs** is allowed. Record the
new `HOSTED_RUN_ATTEMPT` and the new numeric receipt artifact ID. A receipt from
an older or partial attempt cannot authorize publication.

## 4. Publish and require anonymous public acceptance

Resume the same local attempt and provide all four exact hosted-validation
bindings. `publish` first binds and downloads the numeric receipt artifact,
verifies the complete Draft again, and only then publishes by numeric release
ID. Publication is immediately followed by credential-free public acceptance:

```bash
"${FINALIZER[@]}" \
  --hosted-workflow-id "$HOSTED_WORKFLOW_ID" \
  --hosted-run-id "$HOSTED_RUN_ID" \
  --hosted-run-attempt "$HOSTED_RUN_ATTEMPT" \
  --hosted-receipt-artifact-id "$HOSTED_RECEIPT_ARTIFACT_ID" \
  --stop-after publish
```

The command is successful only when its final phase is `public-verified`. The
anonymous acceptance stage fetches the numeric release JSON before and after
all downloads, requires `immutable: true`, compares the exact numeric asset
IDs, sizes, and SHA-256 digests, verifies the complete asset set and
`SHA256SUMS`, safely extracts every archive, and statically revalidates all
macOS signatures and online tickets. It never executes a published candidate.
The finalizer writes the typed receipt to:

```text
<attempt-root>/public-verified.json
```

Do not announce completion unless `public-verified.json` is part of the same
preserved attempt chain whose other receipts bind the exact source
workflow/run/attempt, release ID, hosted workflow/run/attempt, numeric receipt
artifact ID, tag, commit, and local attempt root.

## 5. Outcome-unknown and retry rules

Any Apple or GitHub mutation whose result cannot be proven is `UNKNOWN`, not a
failure that may be retried blindly. Preserve the tag, Draft, attempt root,
temporary evidence, intents, and receipts. The first response to every UNKNOWN
outcome is read-only reconciliation:

- Inspect Apple history/info/log for the existing archive digest. If a durable
  notarization intent exists without a job receipt, adopt only the exact
  matching UUID with `--adopt-submission TARGET=UUID`; do not submit again.
- Inspect the bound numeric Draft/release and asset IDs with `gh release view`
  and `gh api`. Do not create another release, move the tag, or replace assets.
- `--reconcile-github-upload` may authorize only the exact missing upload
  remainder after read-only reconciliation. `--reconcile-github-publish` may
  authorize one exact retry only after reconciliation proves that the same
  bound release is still a complete Draft. Neither flag is a blind retry.
- If publication is already proven but anonymous GET or public-byte validation
  failed, the release is `PUBLIC_BUT_UNVERIFIED`. Resume the same `publish`
  command and binding without a reconciliation flag; the finalizer reconciles
  the existing public release read-only and repeats only the anonymous GET
  verification. It performs no new GitHub mutation.

Never delete evidence to make a retry possible. Never reuse a local attempt
root for a different tag, commit, workflow, run, or attempt.
