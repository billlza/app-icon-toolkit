#!/usr/bin/env bash
set -euo pipefail

plugin_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cargo_about=${CARGO_ABOUT:-cargo-about}
if (( $# > 1 )); then
  echo "usage: $0 [output-file]" >&2
  exit 2
fi
output_file=${1:-"$plugin_root/THIRD_PARTY_LICENSES.html"}

command -v "$cargo_about" >/dev/null 2>&1 || {
  echo "cargo-about 0.9.2 is required to generate third-party notices" >&2
  exit 1
}

cd "$plugin_root"
release_target_lines=$(python3 "$plugin_root/scripts/release_targets.py" rust-targets)
target_arguments=()
while IFS= read -r release_target; do
  if [[ -z "$release_target" ]]; then
    echo "release target contract emitted an empty target" >&2
    exit 1
  fi
  target_arguments+=(--target "$release_target")
done <<< "$release_target_lines"

"$cargo_about" generate --locked --workspace --all-features \
  "${target_arguments[@]}" \
  --output-file "$output_file" \
  "$plugin_root/about.hbs"
