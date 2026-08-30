#!/bin/sh
set -eu

plugin_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cargo_about=${CARGO_ABOUT:-cargo-about}

command -v "$cargo_about" >/dev/null 2>&1 || {
  echo "cargo-about 0.9.2 is required to generate third-party notices" >&2
  exit 1
}

cd "$plugin_root"
"$cargo_about" generate --locked --workspace --all-features \
  --output-file "$plugin_root/THIRD_PARTY_LICENSES.html" \
  "$plugin_root/about.hbs"
