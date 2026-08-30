#!/bin/sh
set -eu

plugin_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

cargo fmt --manifest-path "$plugin_root/Cargo.toml" --all -- --check
cargo clippy --manifest-path "$plugin_root/Cargo.toml" \
  --workspace --all-targets --all-features -- -D warnings
cargo test --manifest-path "$plugin_root/Cargo.toml" \
  --workspace --all-targets --all-features
RUSTDOCFLAGS=-Dwarnings cargo doc --manifest-path "$plugin_root/Cargo.toml" \
  --workspace --all-features --no-deps
command -v cargo-deny >/dev/null 2>&1 || {
  echo "cargo-deny 0.20.2 or newer is required for dependency policy checks" >&2
  exit 1
}
cargo deny --manifest-path "$plugin_root/Cargo.toml" \
  check advisories licenses sources
"$plugin_root/scripts/check-licenses.sh"
cargo build --manifest-path "$plugin_root/Cargo.toml" \
  --release --locked --package app-icon-mcp
"$plugin_root/scripts/build-local.sh"
python3 "$plugin_root/scripts/smoke-installed-plugin.py" "$plugin_root"
