#!/usr/bin/env bash
set -euo pipefail

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

CARGO_ABOUT="$cargo_about" "$plugin_root/scripts/generate-licenses.sh" "$generated_notice"
cmp "$generated_notice" "$plugin_root/THIRD_PARTY_LICENSES.html"
