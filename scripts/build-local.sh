#!/bin/sh
set -eu

if [ "$#" -ne 0 ]; then
  echo "usage: $0" >&2
  exit 2
fi

plugin_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$plugin_root"

target_parent="$plugin_root/target"
mkdir -p "$target_parent"
build_target=$(mktemp -d "$target_parent/build-local.XXXXXXXX")

cleanup() {
  rm -rf -- "$build_target"
}
trap cleanup EXIT HUP INT TERM

cargo build --manifest-path "$plugin_root/Cargo.toml" --release --locked \
  --package app-icon-mcp --target-dir "$build_target"

release_binary="$build_target/release/app-icon-toolkit-mcp"
if [ ! -f "$release_binary" ] || [ ! -s "$release_binary" ] || \
  [ -L "$release_binary" ]; then
  echo "Cargo did not produce a regular local plugin binary: $release_binary" >&2
  exit 1
fi

mkdir -p "$plugin_root/bin"
install -m 0755 \
  "$release_binary" \
  "$plugin_root/bin/app-icon-toolkit-mcp"
