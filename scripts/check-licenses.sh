#!/bin/sh
set -eu

plugin_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cargo_about=${CARGO_ABOUT:-cargo-about}
temporary_base=${TMPDIR:-/tmp}
if [ "$temporary_base" != "/" ]; then
  temporary_base=${temporary_base%/}
fi
generated_notice=$(mktemp "$temporary_base/app-icon-toolkit-licenses.XXXXXX")

cleanup() {
  case "$generated_notice" in
    "$temporary_base"/app-icon-toolkit-licenses.*)
      rm -f -- "$generated_notice"
      ;;
    *)
      echo "refusing to remove unexpected temporary file: $generated_notice" >&2
      return 1
      ;;
  esac
}
trap cleanup EXIT HUP INT TERM

command -v "$cargo_about" >/dev/null 2>&1 || {
  echo "cargo-about 0.9.2 is required to verify third-party notices" >&2
  exit 1
}

cd "$plugin_root"
"$cargo_about" generate --locked --workspace --all-features \
  --output-file "$generated_notice" \
  "$plugin_root/about.hbs"
cmp "$generated_notice" "$plugin_root/THIRD_PARTY_LICENSES.html"
