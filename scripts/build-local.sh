#!/bin/sh
set -eu

plugin_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$plugin_root"

cargo build --manifest-path "$plugin_root/Cargo.toml" --release --locked \
  --package app-icon-mcp
mkdir -p "$plugin_root/bin"
install -m 0755 \
  "$plugin_root/target/release/app-icon-toolkit-mcp" \
  "$plugin_root/bin/app-icon-toolkit-mcp"
